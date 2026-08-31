# AGENTS.md

## About

Keypad is a MicroPython application for a Raspberry Pi Pico W with a Pimoroni 4x4 RGB Keypad. It controls two Elgato Key Lights directly over HTTP or through Home Assistant over MQTT and is updated over Wi-Fi with WebREPL after the application enters maintenance mode.

## Tech Stack

- Pimoroni MicroPython 1.27.0
- Raspberry Pi Pico W
- Pimoroni 4x4 RGB Keypad

## Project Structure

```text
.
├── .gitignore                 # Local development files and device secrets
├── AGENTS.md                  # Agent instructions
├── README.md                  # Project overview
├── boot.py                    # Connects Wi-Fi and starts WebREPL before the app
├── examples/                  # Credential-free configuration templates
├── lib/
│   └── umqtt/
│       ├── __init__.py       # MicroPython MQTT package marker
│       └── simple.py         # MicroPython MQTT client used by the keypad
├── main.py                    # Keypad controls and maintenance/status endpoint
├── tests/                     # Host-side behavioral tests with firmware mocks
├── webrepl_reset.py           # Host-side WebREPL reset helper
└── webrepl_upload.py          # Host-side WebREPL file uploader
```

The device and OTA helpers also consume three ignored local files: `WIFI_CONFIG.py`, `MQTT_CONFIG.py`, and `webrepl_cfg.py`, which are copied to the Pico. `webrepl_cfg.py` is also the single password source for the host-side WebREPL helpers; it must define a non-empty `PASS` string. Never commit these local files or copies containing credentials. Only the empty templates in `examples/` belong in Git.

## Local Setup

Use the Pimoroni MicroPython firmware listed above. Copy each template from `examples/` to the project root without overwriting an existing configuration:

```bash
cp -n examples/WIFI_CONFIG.py WIFI_CONFIG.py
cp -n examples/MQTT_CONFIG.py MQTT_CONFIG.py
cp -n examples/webrepl_cfg.py webrepl_cfg.py
```

Fill in the Wi-Fi SSID and PSK, MQTT broker settings, and a unique WebREPL password. The blank passwords in the templates are not usable credentials. Set `LEFT_HOST` and `RIGHT_HOST` in `main.py` for your Elgato lights; the checked-in hostnames are deployment-specific defaults. MQTT commands require matching Home Assistant automations, which are not included in this repository. LED Cube control always requires MQTT.

For the host tools and tests, create `.venv` with Python 3.14 and install the dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install websocket-client pytest ruff mypy pyright mpremote
```

Provision the Pico over USB first, preserving the `lib/umqtt/` paths and copying the three completed configuration files alongside `boot.py` and `main.py`. Subsequent updates can use the OTA pipeline below. The commands and host helpers use this installation's Pico address; substitute your device address in the commands and pass `--host` to both WebREPL helpers.

### Network Security

Keep the Pico, lights, and broker on a trusted local network. WebREPL uses an unencrypted WebSocket connection, the maintenance endpoint has no authentication, and this application's MQTT connection does not use TLS. Do not expose these services through public port forwarding. A WebREPL password protects REPL access but does not encrypt traffic. Rotate any credential that has been committed to Git, even after removing it from the current files or replacing the history.

## Control Model

### Elgato light protocol

Read the relevant protocol documentation before changing Elgato request construction:

- [Elgato Key Light API](https://github.com/adamesch/elgato-key-light-api)
- [Lights resource](https://github.com/adamesch/elgato-key-light-api/blob/master/resources/lights/README.md)
- [PUT `/elgato/lights`](https://github.com/adamesch/elgato-key-light-api/blob/master/resources/lights/PUT_lights.md)

Representative requests for the two lights are:

```bash
curl -X PUT http://elgato-left.zitryss.dev:9123/elgato/lights \
  -d '{"lights":[{"on":1,"brightness":50,"temperature":200}]}'

curl -X PUT http://elgato-right.zitryss.dev:9123/elgato/lights \
  -d '{"lights":[{"on":1,"brightness":50,"temperature":200}]}'
```

HTTP/1.1 is mandatory. MicroPython's `requests` implementation sends HTTP/1.0, but the Elgato lights reject HTTP/1.0 `PUT` requests. Keep the raw-socket implementation in `main.py`, including an explicit `HTTP/1.1` request line, correct `Host` and `Content-Length` headers, and `Connection: close`.

### MQTT control and feedback

Boot in MQTT mode and keep one long-lived MQTT client connected in both HTTP and MQTT modes. HTTP mode changes only the transport used for Elgato commands; retained Home Assistant feedback must still update the keypad. In HTTP mode, brightness and temperature keys immediately update every currently-on light without changing power. Configure the retained `keypad/availability` Last Will before connecting, poll with non-blocking `check_msg()`, and reconnect with bounded `time.ticks_ms()` backoff so broker failures cannot block local HTTP control or maintenance mode.

The keypad publishes explicit, idempotent Elgato JSON commands such as `{"state":"ON","brightness":67,"temperature":210}` to the target-specific, non-retained `elgato/light/left/set` and `elgato/light/right/set` topics. There is intentionally no aggregate topic: the both-lights key always performs two operations. Command topics and `led-cube/on` are never retained; availability and feedback topics are retained.

Subscribe to `elgato/light/{left,right}/state`, `led-cube/state`, and `keypad/leds/set`. Treat inbound state as authoritative in both modes and never turn feedback into another Elgato or LED Cube command. Reject malformed or out-of-range payloads without terminating the application. An `OFF` Elgato state may use zero brightness/temperature placeholders; those zeros must not erase the keypad's selected profile.

Publish local state on `keypad/mode/state`, `keypad/profile/state`, and `keypad/leds/state` as implemented in `main.py`. Local key `16384` changes keypad illumination without an immediate network message. Combined key `32768` changes keypad illumination locally and sends exactly one explicit command to `led-cube/on`.

### Button encoding and behavior

The Pimoroni keypad reports pressed buttons as a 16-bit bitmask. Each cell below is the raw bit value for that physical key:

| Row / column | 1    | 2    | 3     | 4     |
| ------------ | ---- | ---- | ----- | ----- |
| 1            | 1    | 2    | 4     | 8     |
| 2            | 16   | 32   | 64    | 128   |
| 3            | 256  | 512  | 1024  | 2048  |
| 4            | 4096 | 8192 | 16384 | 32768 |

The physical 4x4 layout maps to these actions:

| Row / column | 1                    | 2                     | 3                    | 4                         |
| ------------ | -------------------- | --------------------- | -------------------- | ------------------------- |
| 1            | Brightness 2         | Brightness 34         | Brightness 67        | Brightness 100            |
| 2            | Temperature 143      | Temperature 210       | Temperature 277      | Temperature 344           |
| 3            | Select HTTP mode     | Left light on/off     | Right light on/off   | Both lights on/off        |
| 4            | Select MQTT mode     | LED Cube on/off       | Keypad LEDs on/off   | Keypad and LED Cube group |

Home Assistant feedback maps granular values to display ranges while keypad commands continue using the exact preset values above:

| Row | Column 1 | Column 2 | Column 3 | Column 4 |
| --- | -------- | -------- | -------- | -------- |
| Brightness | `0..25` | `26..50` | `51..75` | `76..100` |
| Temperature | `143..193` | `194..243` | `244..294` | `295..344` |

If both lights are on, highlight a row preset only when both feedback values fall in the same range. Keep the row dimmed when their ranges differ.

Preserve the row-major mapping between bit positions, button indices, LED feedback, and actions. Use the [Pimoroni RGB Keypad demo](https://github.com/pimoroni/pimoroni-pico/blob/main/micropython/examples/pico_rgb_keypad/demo.py) as the hardware reference.

Bits `256` and `4096` are HTTP and MQTT selectors, not a shared toggle. The selected mode key is bright blue and the inactive mode key is dark blue. Bits `8192`, `16384`, and `32768` remain mode-independent. The combined key uses group semantics: both turn on only when both were off; otherwise both turn off. Group buttons `2048` and `32768` use orange when exactly one member of their pair is on.

## Key Constraints

- **MicroPython, not CPython:** Files copied to the Pico must remain compatible with the device's constrained MicroPython runtime and installed Pimoroni firmware modules. Do not require type annotations or use CPython-only syntax, standard-library modules, or APIs in device code.
- **HTTP/1.1 is required:** Do not replace the raw Elgato socket requests with MicroPython `requests`; its HTTP/1.0 behavior is incompatible with the lights.
- **No device package environment:** Device dependencies are MicroPython built-ins, the firmware-provided `picokeypad` module, and the vendored `lib/umqtt/simple.py`. Host helpers may use a local virtual environment.
- **Keep WebREPL reachable:** `boot.py` must establish Wi-Fi and start WebREPL before `main.py` runs. Enter maintenance mode before OTA upload so `main.py` stops using the keypad and control socket.
- **Keep host and device code distinct:** Host-side CPython helpers may use CPython features, but those features must not leak into files deployed to the Pico.
- **Keep one WebREPL password source:** Both the Pico firmware and the host-side WebREPL helpers read `PASS` from the local `webrepl_cfg.py`.

## Development Environments and Tooling Policy

The deployment destination is the boundary between the two Python environments:

- **Pico/device code:** Every file copied to the Raspberry Pi Pico must comply with MicroPython and the device constraints above. This includes `boot.py`, `main.py`, the entire `lib/` directory, `WIFI_CONFIG.py`, `MQTT_CONFIG.py`, `webrepl_cfg.py`, and any future uploaded file. Do not add CPython-only imports, APIs, syntax, or typing requirements merely to satisfy a desktop tool.
- **Host-only code:** Files that are never copied to the Pico, including `webrepl_upload.py`, `webrepl_reset.py`, and future development or automation helpers, are normal CPython applications. Develop them with modern CPython features, type annotations, and the tools installed in the project `.venv`; MicroPython limitations do not apply to them.

The project `.venv` provides `ruff`, `mypy`, `pyright`, `mpremote`, and the `websocket-client` distribution (imported as `websocket`). Prefer the executables in `.venv/bin/` over globally installed tools.

For every new or changed host-only Python file, use the available developer tools as applicable. In particular, run Ruff, mypy, and Pyright for host helpers such as the WebREPL uploader and reset utility; resolve their findings rather than weakening checks globally. When invoking Pyright directly, point it at the virtual-environment interpreter so it can resolve packages installed in `.venv`:

```bash
.venv/bin/ruff check webrepl_upload.py webrepl_reset.py
.venv/bin/mypy webrepl_upload.py webrepl_reset.py
.venv/bin/pyright --pythonpath .venv/bin/python webrepl_upload.py webrepl_reset.py
```

Use `.venv/bin/mpremote` for supported USB/serial device inspection or file operations when appropriate. Continue to use the project WebREPL helpers for the normal Wi-Fi OTA workflow; they may and should use `websocket-client` and other host-side packages from `.venv`.

Desktop syntax checks can still catch basic mistakes in device files, but passing Ruff, mypy, Pyright, or CPython compilation does not establish MicroPython compatibility. Review device code against the firmware/runtime limitations and verify behavior on the Pico.

## Normal Over-the-Air Update Pipeline

`webrepl_upload.py` is the project-specific WebREPL PUT client. It uses `websocket-client` to send normal masked WebSocket frames while speaking MicroPython's WebREPL file-transfer protocol. Use it instead of the stock WebREPL upload client for this device.

1. Edit the device files locally.
2. Run a host-side syntax check:

   ```bash
   python3 -m py_compile main.py boot.py webrepl_upload.py webrepl_reset.py lib/umqtt/simple.py
   ```

   This verifies Python syntax but does not prove MicroPython compatibility.

3. Confirm the application is running:

   ```bash
   curl -m 5 http://192.168.30.54:8267/status
   ```

   Expected response:

   ```text
   OK status app
   ```

4. Enter maintenance mode:

   ```bash
   curl -m 5 http://192.168.30.54:8267/maintenance/on
   ```

   Expected response:

   ```text
   OK maintenance on
   ```

5. Wait for `main.py` to close TCP/8267 and return. The status endpoint becoming unreachable is expected and leaves the REPL available for WebREPL.
6. Upload the required files:

   ```bash
   . .venv/bin/activate
   python3 webrepl_upload.py --host 192.168.30.54 lib/umqtt/simple.py lib/umqtt/simple.py
   python3 webrepl_upload.py --host 192.168.30.54 MQTT_CONFIG.py MQTT_CONFIG.py
   python3 webrepl_upload.py --host 192.168.30.54 main.py main.py
   ```

   Use the same uploader for any other changed device file. A successful upload prints the remote WebREPL version bytes and uploaded byte count.

7. Reboot the Pico through WebREPL:

   ```bash
   python3 webrepl_reset.py
   ```

8. Verify that the application and WebREPL returned:

   ```bash
   curl -m 5 http://192.168.30.54:8267/status
   nc -vz -G 3 192.168.30.54 8266
   ```

   Expected results:

   ```text
   OK status app
   Connection to 192.168.30.54 port 8266 succeeded
   ```

## Maintenance Endpoint Behavior

`main.py` listens on TCP/8267 while the keypad application is running.

- `/` or `/status` returns `OK status app` in normal mode.
- `/maintenance/on`, `/maintenance`, or `/maint` returns `OK maintenance on`, changes the keypad LEDs to the maintenance color, closes the control socket, and exits `main()`.
- `/maintenance/off`, `/app`, or `/normal` is supported by the handler, although the normal OTA pipeline does not use it because entering maintenance mode exits the application.

The controlled exit prevents the foreground keypad loop from interfering with file transfer. Wi-Fi and WebREPL remain available because `boot.py` started them before the application.

## MicroPython

- Indent with 4 spaces, never tabs
- Follow PEP 8 style guide
- Prefer absolute imports over relative imports
- Avoid wildcard imports `from module import *`
- Import standard modules by their normal names, such as `time`, rather than legacy `u`-prefixed names such as `utime`
- Provide default arguments in functions where applicable
- Never use mutable objects as default function arguments
- Always use double-quoted strings (`"`) — never single-quoted (`'`)
- Always add a trailing comma after the last item in any multi-line collection, function definition, or function call
- Use list, dictionary, set, and generator comprehensions when it improves code readability
- Do not use stepped slicing; MicroPython does not support it consistently across built-in sequence types
- Use context managers `with` for file operations and resource management
- Do not rely on `__exit__()` being called for a context manager used inside a generator when the generator is abandoned before completion
- Do not rely on `__del__()` for resource cleanup; release resources explicitly
- Use plain path strings and the built-in `os` module for filesystem path-related operations
- Do not assume that either CPython's `os.path` or `pathlib` is present in the firmware
- Use `time.ticks_ms()` with `time.ticks_diff()` for elapsed time and intervals, and use `time.ticks_add()` with `time.ticks_diff()` for deadlines; use `time.time()` for absolute timestamps only when the device's RTC is set and maintained
- Never use bare `except:` statements
- Catch specific exceptions rather than broad try/except blocks
- Raise specific built-in or domain-specific exceptions rather than a generic `Exception`
- Catch `OSError` for filesystem, socket, and device I/O failures, and inspect `exc.errno` with constants from `errno`
- Do not rely on CPython `OSError` subclasses
- Catch `ValueError` when decoding malformed JSON; MicroPython does not provide `json.JSONDecodeError`
- Validate that values are JSON-compatible before serialization; MicroPython's `json.dumps()` may serialize `bytes` instead of raising `TypeError`
- Do not use `raise ... from exc`; MicroPython does not support exception chaining
- Never silently swallow a broad `except Exception`; report or recover from the failure explicitly and re-raise when appropriate
- Use `pytest` on the development computer for host-side tests
- Organize imports at the top of a file, followed by constants, then classes and functions, with `if __name__ == "__main__"` at the bottom
