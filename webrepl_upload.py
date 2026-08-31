#!/usr/bin/env python3
# Protocol reference: MicroPython WebREPL client.
# Source: https://github.com/micropython/webrepl/blob/master/webrepl_cli.py
# The upstream license notice below covers that reference, not the entire project.
#
# The MIT License (MIT)
#
# Copyright (c) 2016 Damien P. George
# Copyright (c) 2016 Paul Sokolovsky
# Copyright (c) 2022 Jim Mussared
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

#
# pyright: strict

"""Upload a file through WebREPL using standards-compliant WebSocket frames.

The official MicroPython WebREPL client uses a small unmasked WebSocket
implementation. This helper uses websocket-client because this Pico W
firmware requires normal masked WebSocket frames.
"""

import argparse
import logging
import struct
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import websocket

if TYPE_CHECKING:
    from types import ModuleType

DEFAULT_HOST: Final[str] = "192.168.30.54"
DEFAULT_PORT: Final[int] = 8266
MIN_PORT: Final[int] = 1
MAX_PORT: Final[int] = 65535
CONNECTION_TIMEOUT_SECONDS: Final[float] = 10.0
LOGIN_TIMEOUT_SECONDS: Final[float] = 5.0
INITIAL_REPL_TIMEOUT_SECONDS: Final[float] = 3.0
INTERRUPTED_REPL_TIMEOUT_SECONDS: Final[float] = 8.0
TRANSFER_CHUNK_SIZE: Final[int] = 1024
MAX_REMOTE_FILENAME_BYTES: Final[int] = 64
WEBREPL_REQUEST: Final[struct.Struct] = struct.Struct("<2sBBQLH64s")
WEBREPL_RESPONSE: Final[struct.Struct] = struct.Struct("<2sH")
WEBREPL_PUT_FILE: Final[int] = 1
WEBREPL_GET_VERSION: Final[int] = 3

logger: logging.Logger = logging.getLogger(__name__)


class WebReplError(RuntimeError):
    """Raised when WebREPL rejects an operation or returns invalid data."""


@dataclass(frozen=True, slots=True)
class UploadArguments:
    """Validated inputs for a WebREPL upload."""

    host: str
    port: int
    local_path: Path
    remote_path: str


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


def local_file(value: str) -> Path:
    """Validate that a local upload source is a regular file."""
    path: Path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"local file does not exist: {path}")
    return path


def remote_file(value: str) -> str:
    """Validate a destination against the WebREPL filename field."""
    encoded_value: bytes = value.encode()
    if not encoded_value:
        raise argparse.ArgumentTypeError("remote filename must not be empty")
    if len(encoded_value) > MAX_REMOTE_FILENAME_BYTES:
        raise argparse.ArgumentTypeError(
            "remote filename is longer than the WebREPL 64-byte limit",
        )
    return value


def parse_arguments(argv: Sequence[str] | None = None) -> UploadArguments:
    """Parse command-line arguments."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Upload a file to a MicroPython device through WebREPL.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, type=non_empty_host)
    parser.add_argument("--port", default=DEFAULT_PORT, type=valid_port)
    parser.add_argument("local", type=local_file)
    parser.add_argument("remote", type=remote_file)
    namespace: argparse.Namespace = parser.parse_args(argv)

    return UploadArguments(
        host=cast("str", namespace.host),
        port=cast("int", namespace.port),
        local_path=cast("Path", namespace.local),
        remote_path=cast("str", namespace.remote),
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


def send_request(
    connection: websocket.WebSocket,
    operation: int,
    size: int = 0,
    filename: str | bytes = b"",
) -> None:
    """Send a WebREPL protocol request."""
    encoded_filename: bytes = (
        filename.encode() if isinstance(filename, str) else filename
    )
    if len(encoded_filename) > MAX_REMOTE_FILENAME_BYTES:
        raise ValueError("remote filename is too long for the WebREPL protocol")
    if size < 0:
        raise ValueError("file size must not be negative")

    request: bytes = WEBREPL_REQUEST.pack(
        b"WA",
        operation,
        0,
        0,
        size,
        len(encoded_filename),
        encoded_filename,
    )
    connection.send_binary(request)


def read_binary(connection: websocket.WebSocket) -> bytes:
    """Read the next binary message, ignoring REPL text echoed before it."""
    while True:
        message: str | bytes = connection.recv()
        if not message:
            raise ConnectionError("WebREPL closed the connection unexpectedly")
        if isinstance(message, bytes):
            return message


def read_response(connection: websocket.WebSocket) -> tuple[int, bytes]:
    """Read and validate a WebREPL protocol response."""
    data: bytes = read_binary(connection)
    if len(data) < WEBREPL_RESPONSE.size:
        raise WebReplError(f"short WebREPL response: {data!r}")

    unpacked: tuple[bytes, int] = cast(
        "tuple[bytes, int]",
        WEBREPL_RESPONSE.unpack(data[: WEBREPL_RESPONSE.size]),
    )
    signature: bytes = unpacked[0]
    response_code: int = unpacked[1]
    if signature != b"WB":
        raise WebReplError(f"bad WebREPL response signature: {data!r}")
    return response_code, data[WEBREPL_RESPONSE.size :]


def connect(arguments: UploadArguments) -> websocket.WebSocket:
    """Open a WebSocket connection to a WebREPL server."""
    uri: str = f"ws://{arguments.host}:{arguments.port}/"
    connection: websocket.WebSocket = websocket.create_connection(  # pyright: ignore[reportUnknownMemberType]
        uri,
        timeout=CONNECTION_TIMEOUT_SECONDS,
    )
    return connection


def log_in(connection: websocket.WebSocket, password: str) -> None:
    """Authenticate and ensure that the device is at a REPL prompt."""
    login_prompt: str = recv_until(connection, ": ")
    if "Password" not in login_prompt:
        raise WebReplError(f"unexpected login prompt: {login_prompt!r}")

    connection.send(f"{password}\r")
    try:
        repl_prompt: str = recv_until(
            connection,
            ">>> ",
            timeout=INITIAL_REPL_TIMEOUT_SECONDS,
        )
    except (TimeoutError, websocket.WebSocketTimeoutException):
        # The foreground application may need an interrupt before file transfer.
        connection.send("\x03")
        repl_prompt = recv_until(
            connection,
            ">>> ",
            timeout=INTERRUPTED_REPL_TIMEOUT_SECONDS,
        )

    if "WebREPL connected" not in repl_prompt and ">>>" not in repl_prompt:
        raise WebReplError(f"login did not complete: {repl_prompt!r}")


def upload_file(arguments: UploadArguments) -> None:
    """Upload one local file to the requested remote path."""
    data: bytes = arguments.local_path.read_bytes()
    password: str = webrepl_password()
    connection: websocket.WebSocket = connect(arguments)

    with closing(connection):
        log_in(connection, password)

        send_request(connection, WEBREPL_GET_VERSION)
        version: bytes = read_binary(connection)
        logger.info("remote version bytes: %s", version.hex())

        send_request(
            connection,
            WEBREPL_PUT_FILE,
            len(data),
            arguments.remote_path,
        )
        initial_response: tuple[int, bytes] = read_response(connection)
        initial_code: int = initial_response[0]
        if initial_code != 0:
            raise WebReplError(f"put init failed with code {initial_code}")

        offset: int
        for offset in range(0, len(data), TRANSFER_CHUNK_SIZE):
            chunk: bytes = data[offset : offset + TRANSFER_CHUNK_SIZE]
            connection.send_binary(chunk)

        final_response: tuple[int, bytes] = read_response(connection)
        final_code: int = final_response[0]
        if final_code != 0:
            raise WebReplError(f"put finish failed with code {final_code}")

    logger.info(
        "uploaded %d bytes to %s:%s",
        len(data),
        arguments.host,
        arguments.remote_path,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface and return its process status."""
    arguments: UploadArguments = parse_arguments(argv)
    try:
        upload_file(arguments)
    except (OSError, ValueError, WebReplError, websocket.WebSocketException) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
