import os
from collections.abc import Callable, Generator
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from offbox.errors import CommandFailed, ConnectionError_, PreconditionError, UsageError
from offbox.host.ready import Clock, SystemClock
from offbox.logging import LogEntry, current_invocation
from offbox.naming import Worktree
from offbox.paths import Paths
from offbox.runner import Runner
from offbox.runtime import Runtime
from offbox.ssh import ssh_argv
from offbox.state import HostState

_PENDING_INVOCATIONS = 256


class LogTail:
    """Incrementally select an environment's log lines, including its invocations' lines written before the worktree was known."""

    def __init__(self, env: str, *, exclude: str = "") -> None:
        self.env = env
        self.exclude = exclude
        self.offset = 0
        self.partial = b""
        self.owned: set[str] = set()
        self.pending: dict[str, list[str]] = {}

    def read(self, path: Path) -> list[str]:
        try:
            with path.open("rb") as source:
                if os.fstat(source.fileno()).st_size < self.offset:
                    self.offset, self.partial = 0, b""
                source.seek(self.offset)
                data = source.read()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise PreconditionError(f"Cannot read log file {path}", "Check log permissions and encoding.") from exc
        self.offset += len(data)
        *complete, self.partial = (self.partial + data).split(b"\n")
        selected: list[str] = []
        for raw in complete:
            try:
                line = raw.decode("utf-8") + "\n"
            except UnicodeError as exc:
                raise PreconditionError(f"Cannot read log file {path}", "Check log permissions and encoding.") from exc
            selected.extend(self._select(line))
        return selected

    def _select(self, line: str) -> list[str]:
        try:
            entry = LogEntry.model_validate_json(line)
        except ValidationError:
            return []
        invocation = entry.invocation
        if self.exclude and invocation == self.exclude:
            return []
        if entry.env == self.env:
            earlier: list[str] = []
            if invocation and invocation not in self.owned:
                self.owned.add(invocation)
                earlier = self.pending.pop(invocation, [])
            return [*earlier, line]
        if entry.env is None and invocation:
            if invocation in self.owned:
                return [line]
            self.pending.setdefault(invocation, []).append(line)
            while len(self.pending) > _PENDING_INVOCATIONS:
                del self.pending[next(iter(self.pending))]
        return []


def environment_records(path: Path, env: str) -> list[str]:
    return LogTail(env).read(path)


def follow_lines(
    paths: Paths, env: str, *, follow: bool = False, clock: Clock | None = None, today: Callable[[], date] = date.today
) -> Generator[str]:
    timer = clock or SystemClock()
    selected: date | None = None
    tail = LogTail(env, exclude=current_invocation())
    while True:
        current = today()
        if current != selected:
            selected, tail = current, LogTail(env, exclude=current_invocation())
        yield from tail.read(paths.log_file(current))
        if not follow:
            return
        timer.sleep(0.2)


def stack_logs(paths: Paths, runner: Runner, *, follow: bool = False, service: str | None = None, cwd: Path | None = None) -> int:
    if service is not None and (not service or service.startswith("-") or any(ord(character) < 32 for character in service)):
        raise UsageError("Invalid Compose service", "Pass a Compose service name after --stack.")
    runtime = Runtime(paths, runner)
    with runtime.using("offbox logs --stack", cwd) as (work, _env, host):
        return _stack_logs(runtime, runner, work, host, follow=follow, service=service)


def _stack_logs(runtime: Runtime, runner: Runner, work: Worktree, host: HostState, *, follow: bool, service: str | None) -> int:
    container = runtime.inspect(host, work)
    if container is None or not container.state.running:
        raise PreconditionError("Environment is not running", "Run offbox up before reading stack logs.")
    labels = container.config.labels or {}
    if (
        container.name.lstrip("/") != work.container
        or labels.get("offbox.env") != work.name
        or labels.get("offbox.worktree") != str(work.root)
    ):
        raise PreconditionError("Environment ownership does not match", "Inspect offbox list and the container labels.")
    argv = [
        "docker",
        "exec",
        "-u",
        "dev",
        "-w",
        "/work",
        work.container,
        "docker",
        "compose",
        "logs",
        *(["--follow"] if follow else []),
        *([service] if service else []),
    ]
    status = runner.stream(ssh_argv(host.name, argv, options=["-n"]))
    if status == 255:
        raise ConnectionError_("SSH connection lost while streaming Compose logs", "Run offbox status and check host connectivity.")
    if status:
        raise CommandFailed(argv, status, "Compose log streaming failed; check the service name and offbox status.")
    return 0
