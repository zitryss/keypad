"""Dual-mode keypad controller for Elgato lights and an LED Cube."""

import binascii
import errno
import json
import socket
import time

import machine
import network
import picokeypad
from umqtt.simple import MQTTClient, MQTTException

import boot

try:
    import MQTT_CONFIG
except (ImportError, SyntaxError) as exc:
    print("MQTT_CONFIG.py could not be loaded:", exc)
    MQTT_CONFIG = None


LEFT_HOST = "elgato-left.zitryss.dev"
RIGHT_HOST = "elgato-right.zitryss.dev"
ELGATO_PORT = 9123

MQTT_SERVER = "ha.zitryss.dev"
MQTT_PORT = 1883
MQTT_USER = "hansolo"
MQTT_CLIENT_ID_PREFIX = b"pico-keypad-"
MQTT_SOCKET_TIMEOUT_S = 2
MQTT_KEEPALIVE_S = 30
MQTT_PING_INTERVAL_MS = 20_000
MQTT_RETRY_DELAYS_MS = (1_000, 2_000, 5_000, 10_000, 30_000)

ELGATO_SET_TOPICS = (
    b"elgato/light/left/set",
    b"elgato/light/right/set",
)
ELGATO_STATE_TOPICS = (
    b"elgato/light/left/state",
    b"elgato/light/right/state",
)
LED_CUBE_COMMAND_TOPIC = b"led-cube/on"
LED_CUBE_STATE_TOPIC = b"led-cube/state"
KEYPAD_LEDS_SET_TOPIC = b"keypad/leds/set"
KEYPAD_LEDS_STATE_TOPIC = b"keypad/leds/state"
KEYPAD_MODE_STATE_TOPIC = b"keypad/mode/state"
KEYPAD_PROFILE_STATE_TOPIC = b"keypad/profile/state"
KEYPAD_AVAILABILITY_TOPIC = b"keypad/availability"

MQTT_SUBSCRIPTIONS = (
    ELGATO_STATE_TOPICS[0],
    ELGATO_STATE_TOPICS[1],
    LED_CUBE_STATE_TOPIC,
    KEYPAD_LEDS_SET_TOPIC,
)

CONTROL_PORT = 8267
LOOP_DELAY_MS = 50

LEFT_LIGHT = 0
RIGHT_LIGHT = 1
BOTH_LIGHTS = 2
LIGHTS = (LEFT_LIGHT, RIGHT_LIGHT)

HTTP_MODE = "HTTP"
MQTT_MODE = "MQTT"
MQTT_CONNECTION_ERRORS = (
    OSError,
    MQTTException,
    AssertionError,
    IndexError,
    TypeError,
)


def report_os_error(message, exc):
    print(message, "(errno", exc.errno, ")")


def send_all(sock, data):
    """Send all bytes because MicroPython sockets may perform partial writes."""
    total_sent = 0
    while total_sent < len(data):
        sent = sock.send(data[total_sent:])
        if not sent:
            raise OSError(errno.EIO, "socket send returned 0")
        total_sent += sent


def validate_json_compatible(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return

    if isinstance(value, list):
        for item in value:
            validate_json_compatible(item)
        return

    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            validate_json_compatible(item)
        return

    raise ValueError("value is not JSON-compatible")


def encode_json(value):
    validate_json_compatible(value)
    return json.dumps(value).encode()


def is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


class SocketContext:
    """Close a MicroPython socket when its resource scope ends."""

    def __init__(self, sock):
        self._socket = sock

    def __enter__(self):
        return self._socket

    def __exit__(self, exception_type, _exception, _traceback):
        try:
            self._socket.close()
        except OSError as exc:
            report_os_error("Socket close failed", exc)
            if exception_type is None:
                raise
        return False


class Key:
    """One physical key with its current color and bound press action."""

    def __init__(self, index, color):
        self.index = index
        self._color = color
        self._action = None
        self._argument = None

    @property
    def color(self):
        return self._color

    def set_color(self, color):
        if len(color) != 3:
            raise ValueError("key color must contain red, green, and blue")
        self._color = color

    def bind(self, action, argument=None):
        if action is not None and not callable(action):
            raise ValueError("key action must be callable")
        self._action = action
        self._argument = argument

    def press(self):
        if self._action is None:
            return False
        self._action(self._argument)
        return True

    def illuminate(self, hardware):
        hardware.illuminate(
            self.index,
            self._color[0],
            self._color[1],
            self._color[2],
        )


class Keypad:
    """The keypad aggregate and its locally rendered state."""

    NUM_KEYS = 16
    BRIGHTNESS_VALUES = (2, 34, 67, 100)
    TEMPERATURE_VALUES = (143, 210, 277, 344)
    BRIGHTNESS_RANGES = (
        (0, 25),
        (26, 50),
        (51, 75),
        (76, 100),
    )
    TEMPERATURE_RANGES = (
        (143, 193),
        (194, 243),
        (244, 294),
        (295, 344),
    )

    OFF = (0x00, 0x00, 0x00)
    NETWORK_OK = (0x00, 0x10, 0x00)
    NETWORK_ERROR = (0x10, 0x00, 0x00)
    MAINTENANCE = (0x10, 0x00, 0x20)
    SELECTED_BRIGHTNESS = (0x28, 0x28, 0x28)
    AVAILABLE_BRIGHTNESS = (0x08, 0x08, 0x08)
    SELECTED_TEMPERATURE = (0x20, 0x10, 0x00)
    AVAILABLE_TEMPERATURE = (0x05, 0x02, 0x00)
    SELECTED_MODE = (0x00, 0x00, 0x28)
    AVAILABLE_MODE = (0x00, 0x00, 0x08)
    ON = (0x00, 0x15, 0x00)
    OFF_STATE = (0x15, 0x00, 0x00)
    PARTIAL_ORANGE = (0x20, 0x08, 0x00)

    def __init__(self, hardware):
        self._hardware = hardware
        self._keys = [Key(index, self.OFF) for index in range(self.NUM_KEYS)]
        self._last_button_states = 0
        self._is_on = True
        self._mode = MQTT_MODE
        self._brightness_index = 0
        self._temperature_index = 3
        self._light_power = [False, False]
        self._light_brightness = [self.brightness, self.brightness]
        self._light_temperature = [self.temperature, self.temperature]
        self._led_cube_on = True
        self._led_cube_confirmed = False
        self._hardware.set_brightness(1.0)

    @property
    def is_on(self):
        return self._is_on

    @property
    def mode(self):
        return self._mode

    @property
    def brightness(self):
        return self.BRIGHTNESS_VALUES[self._brightness_index]

    @property
    def temperature(self):
        return self.TEMPERATURE_VALUES[self._temperature_index]

    @property
    def led_cube_on(self):
        return self._led_cube_on

    @property
    def led_cube_confirmed(self):
        return self._led_cube_confirmed

    def key_at(self, index):
        if index < 0 or index >= len(self._keys):
            raise ValueError("invalid key index")
        return self._keys[index]

    def bind(self, index, action, argument=None):
        self.key_at(index).bind(action, argument)

    def select_brightness(self, index):
        if index < 0 or index >= len(self.BRIGHTNESS_VALUES):
            raise ValueError("invalid brightness index")
        self._brightness_index = index
        self.render()

    def select_temperature(self, index):
        if index < 0 or index >= len(self.TEMPERATURE_VALUES):
            raise ValueError("invalid temperature index")
        self._temperature_index = index
        self.render()

    def set_mode(self, mode):
        if mode not in (HTTP_MODE, MQTT_MODE):
            raise ValueError("invalid control mode")
        self._mode = mode
        self.render()

    def set_illumination(self, on):
        if not isinstance(on, bool):
            raise ValueError("keypad illumination state must be boolean")
        self._is_on = on
        self.render()

    def toggle_illumination(self):
        self.set_illumination(not self._is_on)
        return self._is_on

    def is_light_on(self, light):
        self._validate_light(light)
        return self._light_power[light]

    def set_light_desired(self, light, on):
        self._validate_light(light)
        if not isinstance(on, bool):
            raise ValueError("light power state must be boolean")
        self._light_power[light] = on
        self._light_brightness[light] = self.brightness
        self._light_temperature[light] = self.temperature

    def apply_selected_profile(self, light):
        self._validate_light(light)
        self._light_brightness[light] = self.brightness
        self._light_temperature[light] = self.temperature

    def update_light_feedback(self, light, on, brightness, temperature):
        self._validate_light(light)
        self._light_power[light] = on
        if on or brightness > 0:
            self._light_brightness[light] = brightness
        if on or temperature > 0:
            self._light_temperature[light] = temperature
        self.render()

    def light_brightness(self, light):
        self._validate_light(light)
        return self._light_brightness[light]

    def light_temperature(self, light):
        self._validate_light(light)
        return self._light_temperature[light]

    def set_led_cube(self, on, confirmed):
        if not isinstance(on, bool) or not isinstance(confirmed, bool):
            raise ValueError("LED Cube state and confirmation must be boolean")
        self._led_cube_on = on
        self._led_cube_confirmed = confirmed
        self.render()

    def show_network_status(self, connected):
        # A brief full-keypad flash makes boot status visible before controls appear.
        self._fill(self.NETWORK_OK if connected else self.NETWORK_ERROR)
        self._refresh()
        time.sleep_ms(500)

    def show_maintenance(self):
        self._fill(self.MAINTENANCE)
        self._refresh()

    def read_key_press(self):
        button_states = self._hardware.get_button_states()
        changed = button_states != self._last_button_states
        self._last_button_states = button_states

        if not changed or button_states == 0:
            return None

        for key in self._keys:
            if button_states & (1 << key.index):
                return key
        return None

    def render(self):
        if not self._is_on:
            self._fill(self.OFF)
            self._refresh()
            return

        self._render_selection_row(
            0,
            self._display_profile_index(
                self._light_brightness,
                self.BRIGHTNESS_RANGES,
                self._brightness_index,
            ),
            self.SELECTED_BRIGHTNESS,
            self.AVAILABLE_BRIGHTNESS,
        )
        self._render_selection_row(
            4,
            self._display_profile_index(
                self._light_temperature,
                self.TEMPERATURE_RANGES,
                self._temperature_index,
            ),
            self.SELECTED_TEMPERATURE,
            self.AVAILABLE_TEMPERATURE,
        )

        self.key_at(8).set_color(
            self.SELECTED_MODE if self._mode == HTTP_MODE else self.AVAILABLE_MODE,
        )
        self.key_at(12).set_color(
            self.SELECTED_MODE if self._mode == MQTT_MODE else self.AVAILABLE_MODE,
        )

        left_on = self.is_light_on(LEFT_LIGHT)
        right_on = self.is_light_on(RIGHT_LIGHT)
        self.key_at(9).set_color(self.ON if left_on else self.OFF_STATE)
        self.key_at(10).set_color(self.ON if right_on else self.OFF_STATE)
        self.key_at(11).set_color(self._group_color(left_on, right_on))

        cube_color = self.ON if self._led_cube_on else self.OFF_STATE
        self.key_at(13).set_color(cube_color)
        self.key_at(14).set_color(self.ON)
        combined_color = self.ON if self._led_cube_on else self.PARTIAL_ORANGE
        self.key_at(15).set_color(combined_color)
        self._refresh()

    def _display_profile_index(self, values, ranges, local_index):
        active_values = [
            values[light]
            for light in LIGHTS
            if self._light_power[light]
        ]
        if not active_values:
            return local_index

        selected_index = self._range_index(active_values[0], ranges)
        if selected_index is None:
            return None

        for value in active_values[1:]:
            if self._range_index(value, ranges) != selected_index:
                return None
        return selected_index

    def _range_index(self, value, ranges):
        for index, boundaries in enumerate(ranges):
            if boundaries[0] <= value <= boundaries[1]:
                return index
        return None

    def _render_selection_row(self, offset, selected, active_color, idle_color):
        for column in range(4):
            color = active_color if column == selected else idle_color
            self.key_at(offset + column).set_color(color)

    def _group_color(self, first_on, second_on):
        if first_on and second_on:
            return self.ON
        if first_on or second_on:
            return self.PARTIAL_ORANGE
        return self.OFF_STATE

    def _fill(self, color):
        for key in self._keys:
            key.set_color(color)

    def _refresh(self):
        for key in self._keys:
            key.illuminate(self._hardware)
        self._hardware.update()

    def _validate_light(self, light):
        if light not in LIGHTS:
            raise ValueError("invalid light")


class ElgatoLights:
    """Facade for the two lights and their required raw HTTP/1.1 protocol."""

    def __init__(self, left_host, right_host, port):
        self._hosts = (left_host, right_host)
        self._port = port

    def send_state(self, light, keypad):
        if light not in LIGHTS:
            raise ValueError("invalid light")

        request = {
            "lights": [
                {
                    "on": 1 if keypad.is_light_on(light) else 0,
                    "brightness": keypad.brightness,
                    "temperature": keypad.temperature,
                },
            ],
        }
        payload = encode_json(request)
        self._put(self._hosts[light], payload)

    def _put(self, host, payload):
        try:
            address = socket.getaddrinfo(host, self._port)[0][-1]
            with SocketContext(socket.socket()) as sock:
                sock.settimeout(5)
                sock.connect(address)

                headers = (
                    "PUT /elgato/lights HTTP/1.1\r\n"
                    "Host: " + host + ":" + str(self._port) + "\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: " + str(len(payload)) + "\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                )
                send_all(sock, headers.encode() + payload)
                sock.recv(1024)
        except OSError as exc:
            report_os_error("Elgato request failed for " + host, exc)


class MqttGateway:
    """Maintain one bounded, reconnecting MQTT session for commands and state."""

    def __init__(self, mqtt_config, keypad):
        self._keypad = keypad
        self._message_handler = None
        self._client = None
        self._retry_index = 0
        self._next_retry_at = time.ticks_ms()
        self._next_ping_at = self._next_retry_at
        self._server = MQTT_SERVER
        self._port = MQTT_PORT
        self._user = MQTT_USER.encode()
        self._password = None
        self._configure(mqtt_config)

    @property
    def connected(self):
        return self._client is not None

    def set_message_handler(self, handler):
        if handler is not None and not callable(handler):
            raise ValueError("MQTT message handler must be callable")
        self._message_handler = handler

    def poll(self):
        if self._password is None:
            return

        if self._client is None:
            if time.ticks_diff(time.ticks_ms(), self._next_retry_at) >= 0:
                self._connect()
            return

        client = self._client
        try:
            client.check_msg()
            if self._client is not client:
                return
            if time.ticks_diff(time.ticks_ms(), self._next_ping_at) >= 0:
                client.ping()
                self._next_ping_at = time.ticks_add(
                    time.ticks_ms(),
                    MQTT_PING_INTERVAL_MS,
                )
        except MQTT_CONNECTION_ERRORS as exc:
            self._handle_failure("MQTT polling failed", exc)

    def close(self):
        client = self._client
        self._client = None
        if client is None:
            return

        try:
            client.publish(
                KEYPAD_AVAILABILITY_TOPIC,
                b"offline",
                retain=True,
            )
            client.disconnect()
        except MQTT_CONNECTION_ERRORS as exc:
            self._report_mqtt_error("MQTT shutdown failed", exc)
            self._close_socket(client)

    def publish_light(self, light, keypad):
        if light not in LIGHTS:
            raise ValueError("invalid light")
        payload = encode_json(
            {
                "state": "ON" if keypad.is_light_on(light) else "OFF",
                "brightness": keypad.brightness,
                "temperature": keypad.temperature,
            },
        )
        self._publish(ELGATO_SET_TOPICS[light], payload)

    def publish_led_cube(self):
        payload = b"1" if self._keypad.led_cube_on else b"0"
        self._publish(LED_CUBE_COMMAND_TOPIC, payload)

    def publish_keypad_state(self):
        payload = b"ON" if self._keypad.is_on else b"OFF"
        self._publish(KEYPAD_LEDS_STATE_TOPIC, payload, retain=True)

    def publish_mode(self):
        self._publish(
            KEYPAD_MODE_STATE_TOPIC,
            self._keypad.mode.encode(),
            retain=True,
        )

    def publish_profile(self):
        payload = encode_json(
            {
                "brightness": self._keypad.brightness,
                "temperature": self._keypad.temperature,
            },
        )
        self._publish(KEYPAD_PROFILE_STATE_TOPIC, payload, retain=True)

    def _configure(self, mqtt_config):
        if mqtt_config is None:
            print("MQTT is disabled because MQTT_CONFIG.py is unavailable")
            return

        server = getattr(mqtt_config, "MQTT_SERVER", MQTT_SERVER)
        port = getattr(mqtt_config, "MQTT_PORT", MQTT_PORT)
        user = getattr(mqtt_config, "MQTT_USER", MQTT_USER)
        password = getattr(mqtt_config, "MQTT_PASSWORD", None)
        if (
            not isinstance(server, str)
            or not server
            or not is_integer(port)
            or port < 1
            or port > 65_535
            or not isinstance(user, str)
            or not user
            or not isinstance(password, str)
            or not password
        ):
            print("MQTT_CONFIG.py contains invalid broker settings")
            return

        self._server = server
        self._port = port
        self._user = user.encode()
        self._password = password.encode()

    def _connect(self):
        client_id = MQTT_CLIENT_ID_PREFIX + binascii.hexlify(machine.unique_id())
        client = MQTTClient(
            client_id,
            self._server,
            port=self._port,
            user=self._user,
            password=self._password,
            keepalive=MQTT_KEEPALIVE_S,
            socket_timeout=MQTT_SOCKET_TIMEOUT_S,
        )
        client.set_callback(self._dispatch_message)
        client.set_last_will(
            KEYPAD_AVAILABILITY_TOPIC,
            b"offline",
            retain=True,
        )

        try:
            client.connect()
            self._client = client
            for topic in MQTT_SUBSCRIPTIONS:
                client.subscribe(topic)
            self._publish_connection_state(client)
        except MQTT_CONNECTION_ERRORS as exc:
            self._client = None
            self._close_socket(client)
            self._report_mqtt_error("MQTT connection failed", exc)
            self._schedule_retry()
            return False

        self._retry_index = 0
        now = time.ticks_ms()
        self._next_retry_at = now
        self._next_ping_at = time.ticks_add(now, MQTT_PING_INTERVAL_MS)
        return True

    def _publish_connection_state(self, client):
        client.publish(KEYPAD_AVAILABILITY_TOPIC, b"online", retain=True)
        client.publish(
            KEYPAD_LEDS_STATE_TOPIC,
            b"ON" if self._keypad.is_on else b"OFF",
            retain=True,
        )
        client.publish(
            KEYPAD_MODE_STATE_TOPIC,
            self._keypad.mode.encode(),
            retain=True,
        )
        if self._keypad.mode == MQTT_MODE:
            client.publish(
                KEYPAD_PROFILE_STATE_TOPIC,
                encode_json(
                    {
                        "brightness": self._keypad.brightness,
                        "temperature": self._keypad.temperature,
                    },
                ),
                retain=True,
            )

    def _publish(self, topic, payload, retain=False):
        if self._password is None:
            print("MQTT publish skipped because broker settings are unavailable")
            return False

        if self._client is None and not self._connect():
            return False

        try:
            self._client.publish(topic, payload, retain=retain)
            return True
        except MQTT_CONNECTION_ERRORS as exc:
            self._handle_failure("MQTT publish failed", exc, schedule_retry=False)

        if not self._connect():
            return False

        try:
            self._client.publish(topic, payload, retain=retain)
            return True
        except MQTT_CONNECTION_ERRORS as exc:
            self._handle_failure("MQTT publish retry failed", exc)
            return False

    def _dispatch_message(self, topic, payload):
        if self._message_handler is None:
            return
        self._message_handler(topic, payload)

    def _handle_failure(self, message, exc, schedule_retry=True):
        client = self._client
        self._client = None
        if client is not None:
            self._close_socket(client)
        self._report_mqtt_error(message, exc)
        if schedule_retry:
            self._schedule_retry()

    def _schedule_retry(self):
        delay = MQTT_RETRY_DELAYS_MS[self._retry_index]
        if self._retry_index < len(MQTT_RETRY_DELAYS_MS) - 1:
            self._retry_index += 1
        self._next_retry_at = time.ticks_add(time.ticks_ms(), delay)

    def _close_socket(self, client):
        client_socket = getattr(client, "sock", None)
        if client_socket is None:
            return
        try:
            client_socket.close()
        except OSError as exc:
            report_os_error("MQTT socket close failed", exc)
        client.sock = None

    def _report_mqtt_error(self, message, exc):
        if isinstance(exc, OSError):
            report_os_error(message, exc)
            return
        print(message, "(", exc.__class__.__name__, ")")


class HttpLightStrategy:
    """Route Elgato power operations directly over raw HTTP/1.1."""

    def __init__(self, lights):
        self._lights = lights

    def profile_selected(self, keypad):
        active_lights = [light for light in LIGHTS if keypad.is_light_on(light)]
        for light in active_lights:
            keypad.apply_selected_profile(light)
        keypad.render()

        for light in active_lights:
            self._lights.send_state(light, keypad)

    def set_power(self, targets, keypad):
        for light in targets:
            self._lights.send_state(light, keypad)


class MqttLightStrategy:
    """Route Elgato profile and power operations through Home Assistant."""

    def __init__(self, mqtt):
        self._mqtt = mqtt

    def profile_selected(self, keypad):
        active_lights = [light for light in LIGHTS if keypad.is_light_on(light)]
        for light in active_lights:
            keypad.apply_selected_profile(light)
        keypad.render()

        self._mqtt.publish_profile()
        for light in active_lights:
            self._mqtt.publish_light(light, keypad)

    def set_power(self, targets, keypad):
        for light in targets:
            self._mqtt.publish_light(light, keypad)


class ControlServer:
    """Non-blocking HTTP control endpoint for status and OTA maintenance."""

    MAX_REQUEST_BYTES = 512

    def __init__(self, port):
        self._port = port
        self._socket = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        self.close()
        return False

    def open(self):
        if self._socket is not None:
            return

        control = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            control.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            control.bind(("0.0.0.0", self._port))
            control.listen(1)
            control.setblocking(False)
            self._socket = control
        except OSError as exc:
            report_os_error("Control server could not be opened", exc)
            try:
                control.close()
            except OSError as close_exc:
                report_os_error("Control socket cleanup failed", close_exc)
            raise

    def close(self):
        if self._socket is None:
            return
        control = self._socket
        self._socket = None
        try:
            control.close()
        except OSError as exc:
            report_os_error("Control server close failed", exc)

    def poll_for_maintenance(self):
        if self._socket is None:
            raise RuntimeError("control server is not open")

        try:
            client, _address = self._socket.accept()
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                return False
            report_os_error("Control connection accept failed", exc)
            return False

        try:
            with SocketContext(client):
                client.settimeout(1)
                data = client.recv(self.MAX_REQUEST_BYTES)
                path = self._request_path(data)
                maintenance, status, body = self._route(path)
                send_all(client, self._http_response(status, body))
                return maintenance
        except OSError as exc:
            report_os_error("Control request failed", exc)
            return False

    def _request_path(self, data):
        try:
            request = data.decode()
        except UnicodeError:
            return ""

        first_line = request.split("\r\n", 1)[0]
        parts = first_line.split(" ")
        if len(parts) < 2 or not parts[1].startswith("/"):
            return ""
        return parts[1].lower()

    def _route(self, path):
        if path in ("/", "/status"):
            return (False, "200 OK", "OK status app\n")

        if path in ("/maintenance/on", "/maint", "/maintenance"):
            return (True, "200 OK", "OK maintenance on\n")

        if path in ("/maintenance/off", "/app", "/normal"):
            return (False, "200 OK", "OK maintenance off\n")

        return (False, "404 Not Found", "ERR unknown path\n")

    def _http_response(self, status, body):
        payload = body.encode()
        headers = (
            "HTTP/1.1 " + status + "\r\n"
            "Content-Type: text/plain\r\n"
            "Content-Length: " + str(len(payload)) + "\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        return headers.encode() + payload


class KeypadApplication:
    """Coordinate keys, transport strategies, MQTT feedback, and maintenance."""

    def __init__(
        self,
        keypad,
        http_strategy,
        mqtt_strategy,
        mqtt,
        control_server,
    ):
        self._keypad = keypad
        self._strategies = {
            HTTP_MODE: http_strategy,
            MQTT_MODE: mqtt_strategy,
        }
        self._mqtt = mqtt
        self._control_server = control_server
        self._mqtt.set_message_handler(self.handle_mqtt_message)
        self._bind_key_actions()

    def run(self):
        connected = self._ensure_wifi_connected()
        self._keypad.show_network_status(connected)

        with self._control_server:
            self._keypad.render()
            try:
                while True:
                    # Maintenance is always checked before a potentially slow MQTT poll.
                    if self._control_server.poll_for_maintenance():
                        self._keypad.show_maintenance()
                        return

                    self._mqtt.poll()
                    key = self._keypad.read_key_press()
                    if key is not None:
                        key.press()
                    time.sleep_ms(LOOP_DELAY_MS)
            finally:
                self._mqtt.close()

    def handle_mqtt_message(self, topic, payload):
        if topic in ELGATO_STATE_TOPICS:
            light = ELGATO_STATE_TOPICS.index(topic)
            state = self._parse_light_state(payload)
            if state is None:
                print("Ignored malformed Elgato state for light", light)
                return
            self._keypad.update_light_feedback(
                light,
                state[0],
                state[1],
                state[2],
            )
            return

        if topic == LED_CUBE_STATE_TOPIC:
            if payload not in (b"0", b"1"):
                print("Ignored malformed LED Cube state")
                return
            self._keypad.set_led_cube(payload == b"1", True)
            return

        if topic == KEYPAD_LEDS_SET_TOPIC:
            if payload not in (b"ON", b"OFF"):
                print("Ignored malformed keypad illumination command")
                return
            self._keypad.set_illumination(payload == b"ON")
            self._mqtt.publish_keypad_state()

    def _bind_key_actions(self):
        for index in range(4):
            self._keypad.bind(index, self._select_brightness, index)
            self._keypad.bind(index + 4, self._select_temperature, index)

        self._keypad.bind(8, self._select_mode, HTTP_MODE)
        self._keypad.bind(9, self._toggle_light, LEFT_LIGHT)
        self._keypad.bind(10, self._toggle_light, RIGHT_LIGHT)
        self._keypad.bind(11, self._toggle_light, BOTH_LIGHTS)
        self._keypad.bind(12, self._select_mode, MQTT_MODE)
        self._keypad.bind(13, self._toggle_led_cube)
        self._keypad.bind(14, self._toggle_keypad)
        self._keypad.bind(15, self._toggle_combined_power)

    def _select_brightness(self, index):
        self._keypad.select_brightness(index)
        self._active_strategy().profile_selected(self._keypad)

    def _select_temperature(self, index):
        self._keypad.select_temperature(index)
        self._active_strategy().profile_selected(self._keypad)

    def _select_mode(self, mode):
        self._keypad.set_mode(mode)
        self._mqtt.publish_mode()
        if mode == MQTT_MODE:
            self._mqtt.publish_profile()

    def _toggle_light(self, target):
        targets = LIGHTS if target == BOTH_LIGHTS else (target,)
        if target == BOTH_LIGHTS:
            desired_on = not any(
                self._keypad.is_light_on(light) for light in LIGHTS
            )
        else:
            desired_on = not self._keypad.is_light_on(target)

        for light in targets:
            self._keypad.set_light_desired(light, desired_on)
        self._keypad.render()
        self._active_strategy().set_power(targets, self._keypad)

    def _toggle_led_cube(self, _unused):
        self._keypad.set_led_cube(not self._keypad.led_cube_on, False)
        self._mqtt.publish_led_cube()

    def _toggle_keypad(self, _unused):
        self._keypad.toggle_illumination()

    def _toggle_combined_power(self, _unused):
        desired_on = not (self._keypad.is_on or self._keypad.led_cube_on)
        self._keypad.set_led_cube(desired_on, False)
        self._keypad.set_illumination(desired_on)
        self._mqtt.publish_led_cube()

    def _active_strategy(self):
        return self._strategies[self._keypad.mode]

    def _parse_light_state(self, payload):
        try:
            decoded = payload.decode()
            state = json.loads(decoded)
        except (UnicodeError, ValueError):
            return None

        if not isinstance(state, dict) or len(state) != 3:
            return None
        for key in state:
            if key not in ("state", "brightness", "temperature"):
                return None
        if (
            "state" not in state
            or "brightness" not in state
            or "temperature" not in state
        ):
            return None

        power = state["state"]
        brightness = state["brightness"]
        temperature = state["temperature"]
        if power not in ("ON", "OFF"):
            return None
        if not is_integer(brightness) or brightness < 0 or brightness > 100:
            return None
        if not is_integer(temperature):
            return None
        if temperature < 143 or temperature > 344:
            if power != "OFF" or temperature != 0:
                return None
        return (power == "ON", brightness, temperature)

    def _ensure_wifi_connected(self):
        try:
            wlan = network.WLAN(network.STA_IF)
            if wlan.active() and wlan.isconnected():
                return True

            wlan = boot.connect_wifi()
            return wlan.active() and wlan.isconnected()
        except OSError as exc:
            report_os_error("WiFi connection failed", exc)
            return False


def main():
    keypad = Keypad(picokeypad.PicoKeypad())
    lights = ElgatoLights(LEFT_HOST, RIGHT_HOST, ELGATO_PORT)
    mqtt = MqttGateway(MQTT_CONFIG, keypad)
    http_strategy = HttpLightStrategy(lights)
    mqtt_strategy = MqttLightStrategy(mqtt)
    control_server = ControlServer(CONTROL_PORT)
    KeypadApplication(
        keypad,
        http_strategy,
        mqtt_strategy,
        mqtt,
        control_server,
    ).run()


if __name__ == "__main__":
    main()
