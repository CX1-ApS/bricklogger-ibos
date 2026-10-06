"""Small helpers the tests share."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    """Poll a condition until it holds, or fail after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")
