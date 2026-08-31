#!/usr/bin/env python3
#
# pyright: strict

"""Reset a MicroPython device through WebREPL."""

import argparse
import logging
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Final, cast

import websocket

if TYPE_CHECKING:
    from types import ModuleType

DEFAULT_HOST: Final[str] = "192.168.30.54"
DEFAULT_PORT: Final[int] = 8266
MIN_PORT: Final[int] = 1
MAX_PORT: Final[int] = 65535
CONNECTION_TIMEOUT_SECONDS: Final[float] = 10.0
LOGIN_TIMEOUT_SECONDS: Final[float] = 10.0
COMMAND_TIMEOUT_SECONDS: Final[float] = 5.0

logger: logging.Logger = logging.getLogger(__name__)


class WebReplError(RuntimeError):
    """Raised when WebREPL rejects a command or returns invalid data."""


@dataclass(frozen=True, slots=True)
class ResetArguments:
    """Connection settings for a reset request."""

    host: str
    port: int


def non_empty_host(value: str) -> str:
    """Validate and normalize a WebREPL host argument."""
    host: str = value.strip()
    if not host:
        raise argparse.ArgumentTypeError("host must not be empty")
    return host


def valid_port(value: str) -> int:
    """Convert and validate a TCP port argument."""
    try:
        port: int = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc

    if not MIN_PORT <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError(
            f"port must be between {MIN_PORT} and {MAX_PORT}",
        )
    return port


def parse_arguments(argv: Sequence[str] | None = None) -> ResetArguments:
    """Parse command-line arguments."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Reset a MicroPython device through WebREPL.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, type=non_empty_host)
    parser.add_argument("--port", default=DEFAULT_PORT, type=valid_port)
    namespace: argparse.Namespace = parser.parse_args(argv)

    return ResetArguments(
        host=cast("str", namespace.host),
        port=cast("int", namespace.port),
    )


def webrepl_password() -> str:
    """Load the shared WebREPL password from the device configuration."""
    try:
        config: ModuleType = import_module("webrepl_cfg")
    except ModuleNotFoundError as exc:
        if exc.name != "webrepl_cfg":
            raise
        raise WebReplError(
            "webrepl_cfg.py is required for WebREPL access",
        ) from exc

    password: object = getattr(config, "PASS", None)
    if not isinstance(password, str) or not password:
        raise WebReplError(
            "webrepl_cfg.py must define a non-empty PASS string",
        )
    return password


def recv_until(
    connection: websocket.WebSocket,
    suffix: str,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> str:
    """Receive WebSocket messages until the text ends with the expected suffix."""
    if not suffix:
        raise ValueError("suffix must not be empty")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    buffer: str = ""
    deadline: float = time.monotonic() + timeout
    while not buffer.endswith(suffix):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"did not receive suffix {suffix!r}; got {buffer!r}",
            )

        message: str | bytes = connection.recv()
        if not message:
            raise ConnectionError("WebREPL closed the connection unexpectedly")
        if isinstance(message, bytes):
            message = message.decode("utf-8", "replace")
        buffer += message

    return buffer


def connect(arguments: ResetArguments) -> websocket.WebSocket:
    """Open a WebSocket connection to a WebREPL server."""
    uri: str = f"ws://{arguments.host}:{arguments.port}/"
    connection: websocket.WebSocket = websocket.create_connection(  # pyright: ignore[reportUnknownMemberType]
        uri,
        timeout=CONNECTION_TIMEOUT_SECONDS,
    )
    return connection


def log_in(connection: websocket.WebSocket, password: str) -> None:
    """Authenticate and wait for an idle REPL prompt."""
    login_prompt: str = recv_until(connection, ": ")
    if "Password" not in login_prompt:
        raise WebReplError(f"unexpected login prompt: {login_prompt!r}")
    logger.debug("WebREPL login prompt: %r", login_prompt[-80:])

    connection.send(f"{password}\r")
    repl_prompt: str = recv_until(connection, ">>> ")
    if "WebREPL connected" not in repl_prompt and ">>>" not in repl_prompt:
        raise WebReplError(f"login did not complete: {repl_prompt!r}")
    logger.debug("WebREPL prompt after login: %r", repl_prompt[-120:])


def reset_device(connection: websocket.WebSocket) -> None:
    """Import the machine module and request a hardware reset."""
    connection.send("import machine\r")
    import_response: str = recv_until(
        connection,
        ">>> ",
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    logger.debug("WebREPL import response: %r", import_response[-120:])
    connection.send("machine.reset()\r")


def run(arguments: ResetArguments) -> None:
    """Run the reset operation."""
    password: str = webrepl_password()
    connection: websocket.WebSocket = connect(arguments)
    with closing(connection):
        log_in(connection, password)
        reset_device(connection)
    logger.info("reset_sent")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface and return its process status."""
    arguments: ResetArguments = parse_arguments(argv)
    try:
        run(arguments)
    except (OSError, ValueError, WebReplError, websocket.WebSocketException) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
