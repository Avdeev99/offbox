import fcntl
import os
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from offbox.config import Model
from offbox.errors import PreconditionError


class HostState(Model):
    control_master_pid: int | None = Field(default=None, gt=0)
    phase: Literal["creating", "initializing", "ready"] = "ready"
    name: str
    provider: str
    address: str
    ssh_user: str
    ssh_port: int = Field(default=22, ge=1, le=65535)
    created_at: datetime
    provider_dir: Path | None = None
    provider_files_hash: str | None = None
    known_hosts_path: Path
    ssh_key: Path | None = None


class EnvState(Model):
    forward_ip: IPv4Address | None = None
    name: str
    host: str
    worktree_path: Path
    created_at: datetime
    local_ports: dict[int, int] = Field(default_factory=dict[int, int])
    active_forwards: list[int] = Field(default_factory=list[int])
    image_digest: str | None = None


class State(Model):
    version: Literal[1] = 1
    default_host: str | None = None
    hosts: dict[str, HostState] = Field(default_factory=dict)
    envs: dict[str, EnvState] = Field(default_factory=dict)


class StateStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "state.json"
        self.lock_path = directory / "state.lock"

    @contextmanager
    def _lock(self) -> Generator[None]:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.lock_path.open("a", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise PreconditionError(f"Cannot lock state {self.lock_path}", "Check the state directory permissions.") from exc

    def _read(self) -> State:
        try:
            return State.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return State()
        except (OSError, UnicodeError, ValidationError) as exc:
            raise PreconditionError(f"Cannot read state {self.path}", "Repair or restore the state file before continuing.") from exc

    def _write(self, state: State) -> None:
        validated = State.model_validate_json(state.model_dump_json())
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, prefix=".state-", delete=False) as target:
                temporary = Path(target.name)
                target.write(validated.model_dump_json(indent=2) + "\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise PreconditionError(f"Cannot write state {self.path}", "Check free space and state directory permissions.") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self) -> State:
        with self._lock():
            return self._read()

    def write(self, state: State) -> None:
        with self._lock():
            self._write(state)

    @contextmanager
    def transaction(self) -> Generator[State]:
        """Hold the exclusive lock across a read-modify-write operation."""
        with self._lock():
            state = self._read()
            yield state
            self._write(state)

    def update(self, change: Callable[[State], None]) -> State:
        with self.transaction() as state:
            change(state)
        return state
