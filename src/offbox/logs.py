from collections.abc import Callable, Generator
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from offbox.errors import CommandFailed, ConnectionError_, PreconditionError, UsageError
from offbox.host.ready import Clock, SystemClock
from offbox.logging import LogEntry
from offbox.paths import Paths
from offbox.runner import Runner
from offbox.runtime import Runtime
from offbox.ssh import ssh_argv


def environment_records(path: Path, env: str) -> list[tuple[int, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise PreconditionError(f"Cannot read log file {path}", "Check log permissions and encoding.") from exc
    entries: list[tuple[int, str, LogEntry]] = []
    for index, line in enumerate(lines):
        try:
            entry = LogEntry.model_validate_json(line)
        except ValidationError:
            continue
        entries.append((index, line, entry))
    owned = {entry.invocation for _index, _line, entry in entries if entry.env == env and entry.invocation}
    return [(index, line) for index, line, entry in entries if entry.env == env or (entry.env is None and entry.invocation in owned)]


def follow_lines(
    paths: Paths, env: str, *, follow: bool = False, clock: Clock | None = None, today: Callable[[], date] = date.today
) -> Generator[str]:
    timer = clock or SystemClock()
    selected: date | None = None
    emitted: set[int] = set()
    while True:
        current = today()
        if current != selected:
            selected, emitted = current, set()
        for index, line in environment_records(paths.log_file(current), env):
            if index not in emitted:
                emitted.add(index)
                yield line
        if not follow:
            return
        timer.sleep(0.2)


def stack_logs(paths: Paths, runner: Runner, *, follow: bool = False, service: str | None = None, cwd: Path | None = None) -> int:
    if service is not None and (not service or service.startswith("-") or any(ord(character) < 32 for character in service)):
        raise UsageError("Invalid Compose service", "Pass a Compose service name after --stack.")
    runtime = Runtime(paths, runner)
    work, _env, host = runtime.current(cwd)
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
