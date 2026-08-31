# Keypad

A programmable desk keypad for my two Elgato Key Lights and LED cube. Turn the lights on or off, adjust brightness and colour temperature, and see their state on the keys.

Built with a Raspberry Pi Pico W, a Pimoroni 4×4 RGB Keypad, and MicroPython.

- **Updates over Wi-Fi.** A maintenance endpoint pauses the app for uploads through MicroPython’s WebREPL server. A custom upload client implements the file-transfer protocol used by the [official WebREPL client](https://github.com/micropython/webrepl).
- **Two ways to control the lights.** Use Home Assistant over MQTT, or switch to direct HTTP control if the broker is down. The LED cube uses MQTT in either mode.

<img src="https://github.com/user-attachments/assets/4ca4bbe6-e3e0-4ca7-9d9d-3c152d940c7b" alt="Keypad controlling Elgato Key Lights and the LED cube" width="360" height="640">
