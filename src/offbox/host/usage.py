import logging
import os
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from offbox.config import Model
from offbox.errors import PreconditionError
from offbox.host.ready import Clock, SystemClock
from offbox.locking import acquire_lock
from offbox.paths import Paths
from offbox.providers import validate_name

logger = logging.getLogger(__name__)
USAGE_WAIT_SECONDS = 600
type WallClock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class UsageHolder(Model):
    pid: int
    command: str
    started_at: datetime


def _locations(paths: Paths, name: str) -> tuple[Path, Path]:
    validate_name(name)
    directory = paths.state / "host-locks"
    return directory / f"{name}.usage", directory / f"{name}.users"


def _open(path: Path) -> int:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise PreconditionError("Cannot open host usage lock", "Check state directory permissions.") from exc


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def holders(paths: Paths, name: str) -> list[UsageHolder]:
    """Live commands that hold the shared usage lock, pruning entries of processes that have exited."""
    _, directory = _locations(paths, name)
    found: list[UsageHolder] = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return found
    for entry in entries:
        try:
            holder = UsageHolder.model_validate_json(entry.read_text())
        except (OSError, ValidationError):
            logger.info("ignoring unreadable usage holder %s", entry)
            continue
        if _alive(holder.pid):
            found.append(holder)
        else:
            entry.unlink(missing_ok=True)
    return found


def describe(found: list[UsageHolder]) -> str:
    return ", ".join(f"{holder.command} (pid {holder.pid})" for holder in found) or "an offbox command"


@contextmanager
def shared_usage(
    paths: Paths, name: str, command: str, *, wait: float = USAGE_WAIT_SECONDS, clock: Clock | None = None, now: WallClock = utc_now
) -> Generator[None]:
    """Mark the host as in use for the duration of the block; destroy paths skip a host while any holder remains."""
    lock, directory = _locations(paths, name)
    descriptor = _open(lock)
    entry = directory / f"{os.getpid()}-{uuid.uuid4().hex}.json"
    try:
        acquire_lock(
            descriptor,
            timeout=wait,
            clock=clock or SystemClock(),
            message=f"Host {name} is being destroyed",
            fix="Retry the command once the destroy finishes; it recreates an auto-managed host.",
            shared=True,
        )
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            entry.write_text(UsageHolder(pid=os.getpid(), command=command, started_at=now()).model_dump_json())
        except OSError:
            logger.warning("cannot record usage holder for %s", name, exc_info=True)
        yield
    finally:
        entry.unlink(missing_ok=True)
        os.close(descriptor)


@contextmanager
def exclusive_usage(paths: Paths, name: str, *, wait: float = 0, clock: Clock | None = None) -> Generator[bool]:
    """Yield True with the host reserved for destruction, or False when commands still use it after waiting."""
    lock, _ = _locations(paths, name)
    descriptor = _open(lock)
    try:
        try:
            acquire_lock(descriptor, timeout=wait, clock=clock or SystemClock(), message="busy", fix="busy")
        except PreconditionError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)
