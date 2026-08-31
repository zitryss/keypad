# boot.py -- Wi-Fi + WebREPL startup (Pico W / MicroPython).

import errno
import time

import network
import webrepl
import WIFI_CONFIG

CONNECT_TIMEOUT_MS = 20_000
WIFI_POLL_INTERVAL_MS = 250


def wifi_status():
    wlan = network.WLAN(network.STA_IF)
    if not wlan.active():
        wlan.active(True)
    return wlan


def connect_wifi(timeout_ms=CONNECT_TIMEOUT_MS):
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int)
        or timeout_ms < 0
    ):
        raise ValueError("WiFi connection timeout must be a non-negative integer")

    wlan = wifi_status()
    if not wlan.isconnected():
        wlan.connect(WIFI_CONFIG.SSID, WIFI_CONFIG.PSK)
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while not wlan.isconnected() and time.ticks_diff(deadline, time.ticks_ms()) > 0:
            time.sleep_ms(WIFI_POLL_INTERVAL_MS)
    return wlan


def network_config(wlan):
    if hasattr(wlan, "ifconfig"):
        return wlan.ifconfig()
    return wlan.ipconfig("addr4")


def start_webrepl():
    wlan = connect_wifi()
    if not wlan.isconnected():
        print("WiFi not connected; WebREPL not started")
        return wlan

    try:
        ap = network.WLAN(network.AP_IF)
        if ap.active():
            ap.active(False)
    except OSError as exc:
        if exc.errno == errno.EIO:
            print("Access point could not be disabled due to an I/O error:", exc)
        else:
            print("Access point could not be disabled; errno", exc.errno, exc)

    webrepl.start()
    print("WebREPL started:", network_config(wlan))
    return wlan


WLAN = start_webrepl()
