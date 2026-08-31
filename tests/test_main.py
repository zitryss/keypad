"""Host-side behavioral tests for the MicroPython keypad application."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest


class FakeHardware:
    """Record keypad output without Pimoroni hardware."""

    def __init__(self):
        self.colors = [(0, 0, 0)] * 16
        self.button_states = 0
        self.update_count = 0

    def set_brightness(self, _brightness):
        return

    def illuminate(self, index, red, green, blue):
        self.colors[index] = (red, green, blue)

    def update(self):
        self.update_count += 1

    def get_button_states(self):
        return self.button_states


class FakeLights:
    """Record direct Elgato operations."""

    def __init__(self):
        self.states = []
        self.profile_colors_at_send = []

    def send_state(self, light, keypad):
        self.profile_colors_at_send.append(
            tuple(keypad.key_at(index).color for index in range(8)),
        )
        self.states.append(
            (
                light,
                keypad.is_light_on(light),
                keypad.brightness,
                keypad.temperature,
            ),
        )


class FakeMqtt:
    """Record MQTT-facing application operations."""

    def __init__(self, firmware):
        self.firmware = firmware
        self.events = []
        self.handler = None
        self.poll_count = 0
        self.closed = False

    def set_message_handler(self, handler):
        self.handler = handler

    def publish_light(self, light, keypad):
        self.events.append(
            (
                "light",
                self.firmware.ELGATO_SET_TOPICS[light],
                "ON" if keypad.is_light_on(light) else "OFF",
                keypad.brightness,
                keypad.temperature,
            ),
        )

    def publish_led_cube(self):
        self.events.append(("cube",))

    def publish_keypad_state(self):
        self.events.append(("keypad_state",))

    def publish_mode(self):
        self.events.append(("mode",))

    def publish_profile(self):
        self.events.append(("profile",))

    def poll(self):
        self.poll_count += 1

    def close(self):
        self.closed = True


class FakeControlServer:
    """Small context manager used to construct the application."""

    def __init__(self, maintenance=False):
        self.maintenance = maintenance

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        return False

    def poll_for_maintenance(self):
        return self.maintenance


@pytest.fixture(scope="module")
def firmware():
    """Import device code with its firmware-only modules mocked."""
    fake_network = ModuleType("network")
    dynamic_network = cast("Any", fake_network)
    dynamic_network.STA_IF = 0
    dynamic_network.WLAN = lambda _interface: SimpleNamespace(
        active=lambda: True,
        isconnected=lambda: True,
    )

    fake_picokeypad = ModuleType("picokeypad")
    cast("Any", fake_picokeypad).PicoKeypad = FakeHardware

    fake_machine = ModuleType("machine")
    cast("Any", fake_machine).unique_id = lambda: b"test-device"

    fake_boot = ModuleType("boot")
    cast("Any", fake_boot).connect_wifi = lambda: SimpleNamespace(
        active=lambda: True,
        isconnected=lambda: True,
    )

    fake_simple = ModuleType("umqtt.simple")

    class ImportMqttClient:
        pass

    class ImportMqttError(Exception):
        pass

    cast("Any", fake_simple).MQTTClient = ImportMqttClient
    cast("Any", fake_simple).MQTTException = ImportMqttError
    fake_umqtt = ModuleType("umqtt")
    cast("Any", fake_umqtt).simple = fake_simple

    modules = {
        "network": fake_network,
        "picokeypad": fake_picokeypad,
        "machine": fake_machine,
        "boot": fake_boot,
        "umqtt": fake_umqtt,
        "umqtt.simple": fake_simple,
    }
    previous_modules = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    sys.modules.pop("main", None)
    module_path = Path(__file__).parent.parent / "main.py"
    specification = importlib.util.spec_from_file_location("main", module_path)
    assert specification is not None
    assert specification.loader is not None
    imported = importlib.util.module_from_spec(specification)
    sys.modules["main"] = imported
    specification.loader.exec_module(imported)
    imported.time.sleep_ms = lambda _milliseconds: None
    imported.time.ticks_ms = lambda: 0
    imported.time.ticks_add = lambda ticks, delta: ticks + delta
    imported.time.ticks_diff = lambda first, second: first - second
    yield imported
    sys.modules.pop("main", None)
    for name, previous in previous_modules.items():
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


@pytest.fixture
def rig(firmware):
    """Build an isolated keypad application and its recording collaborators."""
    hardware = FakeHardware()
    keypad = firmware.Keypad(hardware)
    lights = FakeLights()
    mqtt = FakeMqtt(firmware)
    control = FakeControlServer()
    app = firmware.KeypadApplication(
        keypad,
        firmware.HttpLightStrategy(lights),
        firmware.MqttLightStrategy(mqtt),
        mqtt,
        control,
    )
    keypad.render()
    return SimpleNamespace(
        app=app,
        control=control,
        hardware=hardware,
        keypad=keypad,
        lights=lights,
        mqtt=mqtt,
    )


def press_bit(rig, bit):
    """Press the key represented by a power-of-two hardware bit."""
    index = bit.bit_length() - 1
    assert rig.keypad.key_at(index).press()


def select_mqtt(rig, firmware):
    """Select MQTT mode and discard state-feedback events from selection."""
    press_bit(rig, 4096)
    assert rig.keypad.mode == firmware.MQTT_MODE
    rig.mqtt.events.clear()


def select_http(rig, firmware):
    """Select HTTP mode and discard mode-feedback events from selection."""
    press_bit(rig, 256)
    assert rig.keypad.mode == firmware.HTTP_MODE
    rig.mqtt.events.clear()


def light_payload(state, brightness=67, temperature=210):
    """Encode a representative Home Assistant state message."""
    return json.dumps(
        {
            "state": state,
            "brightness": brightness,
            "temperature": temperature,
        },
    ).encode()


def test_default_mode_and_blue_selectors(rig, firmware):
    assert rig.keypad.mode == firmware.MQTT_MODE
    assert rig.keypad.key_at(8).color == rig.keypad.AVAILABLE_MODE
    assert rig.keypad.key_at(12).color == rig.keypad.SELECTED_MODE

    press_bit(rig, 256)

    assert rig.keypad.mode == firmware.HTTP_MODE
    assert rig.keypad.key_at(8).color == rig.keypad.SELECTED_MODE
    assert rig.keypad.key_at(12).color == rig.keypad.AVAILABLE_MODE
    assert rig.mqtt.events == [("mode",)]

    rig.mqtt.events.clear()
    press_bit(rig, 4096)
    assert rig.keypad.mode == firmware.MQTT_MODE
    assert [event[0] for event in rig.mqtt.events] == ["mode", "profile"]


@pytest.mark.parametrize("bit", [1, 2, 4, 8, 16, 32, 64, 128])
def test_http_profile_buttons_update_powered_lights(rig, firmware, bit):
    select_http(rig, firmware)
    rig.keypad.set_light_desired(firmware.LEFT_LIGHT, True)
    rig.keypad.set_light_desired(firmware.RIGHT_LIGHT, True)

    press_bit(rig, bit)

    assert [state[0] for state in rig.lights.states] == [0, 1]
    assert all(state[1] for state in rig.lights.states)
    selected_index = bit.bit_length() - 1
    selected_color = (
        rig.keypad.SELECTED_BRIGHTNESS
        if selected_index < 4
        else rig.keypad.SELECTED_TEMPERATURE
    )
    assert all(
        colors[selected_index] == selected_color
        for colors in rig.lights.profile_colors_at_send
    )
    assert rig.mqtt.events == []


def test_http_profile_change_leaves_off_light_untouched(rig, firmware):
    select_http(rig, firmware)
    rig.keypad.set_light_desired(firmware.LEFT_LIGHT, True)

    press_bit(rig, 4)

    assert [state[0] for state in rig.lights.states] == [firmware.LEFT_LIGHT]
    assert not rig.keypad.is_light_on(firmware.RIGHT_LIGHT)


@pytest.mark.parametrize(
    ("bit", "expected_lights"),
    [
        (512, [0]),
        (1024, [1]),
        (2048, [0, 1]),
    ],
)
def test_http_power_routing(rig, firmware, bit, expected_lights):
    select_http(rig, firmware)

    press_bit(rig, bit)

    assert [state[0] for state in rig.lights.states] == expected_lights
    assert rig.mqtt.events == []


@pytest.mark.parametrize(
    ("bit", "expected_topics"),
    [
        (512, [b"elgato/light/left/set"]),
        (1024, [b"elgato/light/right/set"]),
        (
            2048,
            [b"elgato/light/left/set", b"elgato/light/right/set"],
        ),
    ],
)
def test_mqtt_power_routing_uses_only_target_topics(
    rig,
    firmware,
    bit,
    expected_topics,
):
    select_mqtt(rig, firmware)

    press_bit(rig, bit)

    assert [event[1] for event in rig.mqtt.events] == expected_topics
    assert rig.lights.states == []
    assert all(b"/all/" not in topic for topic in firmware.ELGATO_SET_TOPICS)


def test_mqtt_profile_fans_out_only_to_powered_lights(rig, firmware):
    rig.keypad.set_light_desired(firmware.LEFT_LIGHT, True)
    select_mqtt(rig, firmware)

    press_bit(rig, 4)

    assert [event[0] for event in rig.mqtt.events] == ["profile", "light"]
    assert rig.mqtt.events[1][1] == firmware.ELGATO_SET_TOPICS[0]

    rig.keypad.set_light_desired(firmware.RIGHT_LIGHT, True)
    rig.mqtt.events.clear()
    press_bit(rig, 32)

    assert [event[0] for event in rig.mqtt.events] == [
        "profile",
        "light",
        "light",
    ]


def test_feedback_updates_independent_caches_and_mixed_display(rig, firmware):
    rig.app.handle_mqtt_message(
        firmware.ELGATO_STATE_TOPICS[0],
        light_payload("ON", 67, 210),
    )

    assert rig.keypad.is_light_on(firmware.LEFT_LIGHT)
    assert rig.keypad.key_at(2).color == rig.keypad.SELECTED_BRIGHTNESS
    assert rig.keypad.key_at(5).color == rig.keypad.SELECTED_TEMPERATURE
    assert rig.keypad.key_at(11).color == rig.keypad.PARTIAL_ORANGE
    assert rig.mqtt.events == []

    rig.app.handle_mqtt_message(
        firmware.ELGATO_STATE_TOPICS[1],
        light_payload("ON", 50, 250),
    )

    assert all(
        rig.keypad.key_at(index).color == rig.keypad.AVAILABLE_BRIGHTNESS
        for index in range(4)
    )
    assert all(
        rig.keypad.key_at(index).color == rig.keypad.AVAILABLE_TEMPERATURE
        for index in range(4, 8)
    )


@pytest.mark.parametrize(
    ("brightness", "expected_index"),
    [
        (0, 0),
        (25, 0),
        (26, 1),
        (50, 1),
        (51, 2),
        (75, 2),
        (76, 3),
        (100, 3),
    ],
)
def test_granular_brightness_feedback_uses_ranges(
    rig,
    firmware,
    brightness,
    expected_index,
):
    rig.app.handle_mqtt_message(
        firmware.ELGATO_STATE_TOPICS[0],
        light_payload("ON", brightness, 210),
    )

    assert rig.keypad.key_at(expected_index).color == (
        rig.keypad.SELECTED_BRIGHTNESS
    )


@pytest.mark.parametrize(
    ("temperature", "expected_index"),
    [
        (143, 4),
        (193, 4),
        (194, 5),
        (243, 5),
        (244, 6),
        (294, 6),
        (295, 7),
        (344, 7),
    ],
)
def test_granular_temperature_feedback_uses_ranges(
    rig,
    firmware,
    temperature,
    expected_index,
):
    rig.app.handle_mqtt_message(
        firmware.ELGATO_STATE_TOPICS[0],
        light_payload("ON", 67, temperature),
    )

    assert rig.keypad.key_at(expected_index).color == (
        rig.keypad.SELECTED_TEMPERATURE
    )


def test_two_active_lights_highlight_their_shared_ranges(rig, firmware):
    rig.app.handle_mqtt_message(
        firmware.ELGATO_STATE_TOPICS[0],
        light_payload("ON", 35, 200),
    )
    rig.app.handle_mqtt_message(
        firmware.ELGATO_STATE_TOPICS[1],
        light_payload("ON", 50, 240),
    )

    assert rig.keypad.key_at(1).color == rig.keypad.SELECTED_BRIGHTNESS
    assert rig.keypad.key_at(5).color == rig.keypad.SELECTED_TEMPERATURE


def test_off_zero_placeholders_preserve_last_known_profile(rig, firmware):
    rig.app.handle_mqtt_message(
        firmware.ELGATO_STATE_TOPICS[0],
        light_payload("ON", 67, 210),
    )
    rig.app.handle_mqtt_message(
        firmware.ELGATO_STATE_TOPICS[0],
        light_payload("OFF", 0, 0),
    )

    assert rig.keypad.light_brightness(firmware.LEFT_LIGHT) == 67
    assert rig.keypad.light_temperature(firmware.LEFT_LIGHT) == 210


def test_led_cube_key_changes_only_led_cube(rig, firmware):
    initial_keypad_state = rig.keypad.is_on

    press_bit(rig, 8192)

    assert rig.keypad.is_on == initial_keypad_state
    assert not rig.keypad.led_cube_on
    assert not rig.keypad.led_cube_confirmed
    assert rig.keypad.key_at(13).color == rig.keypad.OFF_STATE
    assert rig.lights.states == []
    assert rig.mqtt.events == [("cube",)]

    rig.app.handle_mqtt_message(firmware.LED_CUBE_STATE_TOPIC, b"1")

    assert rig.keypad.led_cube_confirmed
    assert rig.keypad.led_cube_on
    assert rig.keypad.key_at(13).color == rig.keypad.ON
    assert rig.mqtt.events == [("cube",)]


def test_led_cube_feedback_is_authoritative(rig, firmware):
    assert not rig.keypad.led_cube_confirmed

    rig.app.handle_mqtt_message(firmware.LED_CUBE_STATE_TOPIC, b"0")

    assert rig.keypad.led_cube_confirmed
    assert not rig.keypad.led_cube_on
    assert rig.keypad.key_at(13).color == rig.keypad.OFF_STATE
    assert rig.keypad.key_at(15).color == rig.keypad.PARTIAL_ORANGE
    assert rig.mqtt.events == []

    rig.app.handle_mqtt_message(firmware.LED_CUBE_STATE_TOPIC, b"invalid")
    assert not rig.keypad.led_cube_on


def test_keypad_key_is_strictly_local(rig):
    initial_cube_state = rig.keypad.led_cube_on

    press_bit(rig, 16384)

    assert not rig.keypad.is_on
    assert rig.keypad.led_cube_on == initial_cube_state
    assert rig.hardware.colors == [rig.keypad.OFF] * 16
    assert rig.lights.states == []
    assert rig.mqtt.events == []


@pytest.mark.parametrize(
    ("keypad_on", "cube_on", "expected"),
    [
        (False, False, True),
        (False, True, False),
        (True, False, False),
        (True, True, False),
    ],
)
def test_combined_key_truth_table(rig, keypad_on, cube_on, expected):
    rig.keypad.set_led_cube(cube_on, True)
    rig.keypad.set_illumination(keypad_on)
    rig.mqtt.events.clear()

    press_bit(rig, 32768)

    assert rig.keypad.is_on == expected
    assert rig.keypad.led_cube_on == expected
    assert rig.lights.states == []
    assert rig.mqtt.events == [("cube",)]


def test_remote_keypad_command_does_not_control_other_devices(rig, firmware):
    initial_cube_state = rig.keypad.led_cube_on

    rig.app.handle_mqtt_message(firmware.KEYPAD_LEDS_SET_TOPIC, b"OFF")

    assert not rig.keypad.is_on
    assert rig.keypad.led_cube_on == initial_cube_state
    assert rig.lights.states == []
    assert rig.mqtt.events == [("keypad_state",)]


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"state":"on","brightness":67,"temperature":210}',
        b'{"state":"ON","brightness":true,"temperature":210}',
        b'{"state":"ON","brightness":101,"temperature":210}',
        b'{"state":"ON","brightness":67,"temperature":0}',
        b'{"state":"ON","brightness":67,"temperature":210,"extra":1}',
    ],
)
def test_malformed_light_feedback_is_ignored(rig, firmware, payload):
    rig.app.handle_mqtt_message(firmware.ELGATO_STATE_TOPICS[0], payload)

    assert not rig.keypad.is_light_on(firmware.LEFT_LIGHT)
    assert rig.mqtt.events == []


def test_mqtt_gateway_reconnects_and_republishes_state(
    firmware,
    monkeypatch,
):
    now = [0]
    clients = []

    class FakeSocket:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeBrokerClient:
        def __init__(self, *_args, **_kwargs):
            self.arguments = _args
            self.callback = None
            self.last_will = None
            self.subscriptions = []
            self.published = []
            self.sock = FakeSocket()
            self.fail_check = False
            clients.append(self)

        def set_callback(self, callback):
            self.callback = callback

        def set_last_will(self, topic, payload, retain=False, qos=0):
            self.last_will = (topic, payload, retain, qos)

        def connect(self):
            return 0

        def subscribe(self, topic):
            self.subscriptions.append(topic)

        def publish(self, topic, payload, retain=False):
            self.published.append((topic, payload, retain))

        def check_msg(self):
            if self.fail_check:
                raise OSError(5, "simulated I/O failure")

        def ping(self):
            return

        def disconnect(self):
            return

    monkeypatch.setattr(firmware, "MQTTClient", FakeBrokerClient)
    monkeypatch.setattr(firmware.time, "ticks_ms", lambda: now[0])
    keypad = firmware.Keypad(FakeHardware())
    config = SimpleNamespace(MQTT_PASSWORD="secret")
    gateway = firmware.MqttGateway(config, keypad)

    gateway.poll()

    assert gateway.connected
    assert clients[0].arguments[0] == b"pico-keypad-746573742d646576696365"
    assert clients[0].subscriptions == list(firmware.MQTT_SUBSCRIPTIONS)
    assert clients[0].last_will == (
        firmware.KEYPAD_AVAILABILITY_TOPIC,
        b"offline",
        True,
        0,
    )
    retained_topics = {
        topic for topic, _payload, retain in clients[0].published if retain
    }
    assert retained_topics == {
        firmware.KEYPAD_AVAILABILITY_TOPIC,
        firmware.KEYPAD_LEDS_STATE_TOPIC,
        firmware.KEYPAD_MODE_STATE_TOPIC,
        firmware.KEYPAD_PROFILE_STATE_TOPIC,
    }

    clients[0].fail_check = True
    failed_socket = clients[0].sock
    gateway.poll()
    assert not gateway.connected
    assert failed_socket.closed

    now[0] = 999
    gateway.poll()
    assert len(clients) == 1

    now[0] = 1000
    gateway.poll()
    assert gateway.connected
    assert len(clients) == 2
    assert clients[1].subscriptions == list(firmware.MQTT_SUBSCRIPTIONS)


def test_maintenance_is_checked_before_mqtt_poll(firmware):
    hardware = FakeHardware()
    keypad = firmware.Keypad(hardware)
    lights = FakeLights()
    mqtt = FakeMqtt(firmware)
    control = FakeControlServer(maintenance=True)
    app = firmware.KeypadApplication(
        keypad,
        firmware.HttpLightStrategy(lights),
        firmware.MqttLightStrategy(mqtt),
        mqtt,
        control,
    )

    app.run()

    assert mqtt.poll_count == 0
    assert mqtt.closed
    assert hardware.colors == [keypad.MAINTENANCE] * 16
