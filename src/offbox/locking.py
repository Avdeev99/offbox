from __future__ import annotations

import fcntl
from typing import TYPE_CHECKING

from offbox.errors import PreconditionError

if TYPE_CHECKING:
    from offbox.host.ready import Clock


def acquire_lock(descriptor: int, *, timeout: float = 0, clock: Clock, message: str, fix: str) -> None:
    timer = clock
    deadline: float | None = None
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            if timeout <= 0:
                raise PreconditionError(message, fix) from exc
            if deadline is None:
                deadline = timer.now() + timeout
            remaining = deadline - timer.now()
            if remaining <= 0:
                raise PreconditionError(f"{message} after waiting {timeout:g}s", fix) from exc
            timer.sleep(min(0.05, remaining))
