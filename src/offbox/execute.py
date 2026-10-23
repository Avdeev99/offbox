import fcntl
import logging
import os
import re
import shlex
import signal
import stat
import sys
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import FrameType

from pydantic import Field, SecretStr

from offbox import console
from offbox.config import InstallConfig, Model, ProjectConfig, load_config
from offbox.errors import CommandFailed, ConnectionError_, OffboxError, PreconditionError, UsageError
from offbox.host.ready import Clock, SystemClock
from offbox.logging import register_secrets
from offbox.naming import Worktree, validate_env_name
from offbox.paths import Paths
from offbox.runner import Runner, StreamProcess
from offbox.ssh import ssh_argv
from offbox.state import HostState

logger = logging.getLogger(__name__)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_CONNECTION_FIX = "offbox status"


class ExecOptions(Model):
    command: list[str]
    tty: bool | None = None
    no_sync: bool = False
    stdin: bool = False
    overrides: dict[str, SecretStr] = Field(default_factory=dict)


def parse_env(entries: Sequence[str]) -> dict[str, SecretStr]:
    result: dict[str, SecretStr] = {}
    for entry in entries:
        name, separator, value = entry.partition("=")
        if not separator or _ENV_NAME.fullmatch(name) is None or "\0" in value:
            raise UsageError("Invalid --env value", "Use --env NAME=value with a valid environment variable name.")
        register_secrets({name: value})
        result[name] = SecretStr(value)
    return result


def execution_env(
    configured: Sequence[str], overrides: Mapping[str, SecretStr], *, tty: bool, local: Mapping[str, str] | None = None
) -> dict[str, str]:
    values = os.environ if local is None else local
    names = [*configured, *(["TERM", "COLORTERM", "LANG"] if tty else [])]
    if any(_ENV_NAME.fullmatch(name) is None for name in names):
        raise PreconditionError("Invalid exec.env name", "Configure exec.env as environment variable names.")
    result = {name: values[name] for name in names if name in values}
    result.update({name: value.get_secret_value() for name, value in overrides.items()})
    register_secrets(result)
    return result


def command_argv(
    work: Worktree,
    host: HostState,
    command: Sequence[str],
    *,
    run_id: str,
    directory: str = "/work",
    env: Mapping[str, str] | None = None,
    tty: bool = False,
    force_pty: bool = False,
    stdin: bool = False,
    diagnostics: bool = False,
) -> list[str]:
    if not command or not command[0] or any("\0" in part for part in command):
        raise UsageError("A command is required", "Run offbox exec -- COMMAND [ARGS...].")
    if re.fullmatch(r"[a-f0-9]{32}", run_id) is None:
        raise PreconditionError("Invalid execution identifier", "Retry the command.")
    relative = PurePosixPath(directory)
    if not relative.is_relative_to("/work") or ".." in relative.parts or "\0" in directory:
        raise PreconditionError("Command directory is outside /work", "Run the command inside this worktree.")
    remote = ["docker", "exec", *(["-i"] if tty or stdin else []), *(["-t"] if tty else []), "-u", "dev", "-w", directory]
    for name, value in sorted((env or {}).items()):
        if _ENV_NAME.fullmatch(name) is None or "\0" in value:
            raise UsageError("Invalid execution environment", "Use valid environment variable names and values.")
        remote.extend(["-e", f"{name}={value}"])
    remote.extend([work.container, "offbox-run", *(["--tty"] if tty else []), run_id, directory, "--", *command])
    if diagnostics:
        remote = ["sh", "-c", "exec " + shlex.join(remote) + " >&2"]
    options = ["-tt" if force_pty else "-t"] if tty else ([] if stdin else ["-n"])
    return ssh_argv(host.name, remote, options=options)


class SignalState:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.number: int | None = None
        self.received_at: float | None = None

    def receive(self, number: int, frame: FrameType | None) -> None:
        if self.number is None:
            self.number = number
            self.received_at = self.clock.now()


@contextmanager
def capture_signals(state: SignalState) -> Generator[None]:
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        for number in previous:
            signal.signal(number, state.receive)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


_HASH = r"""set -eu
export LC_ALL=C
root=$1
shift
[ ! -L /srv/offbox/envs ] && [ ! -L "${root%/work}" ] && [ ! -L "$root" ] || exit 3
cd -- "$root"
umask 077
tmp=$(mktemp)
trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
for file do
    parent=$file
    while [ "$parent" != . ]; do
        [ ! -L "$parent" ] || { printf '%s\n' 'offbox: install.when contains a symlink' >&2; exit 3; }
        case "$parent" in */*) parent=${parent%/*} ;; *) parent=. ;; esac
    done
    if kind=$(stat --format=%F -- "$file" 2>&1); then
        case "$kind" in 'regular file'|'regular empty file') ;; *) printf '%s\n' 'offbox: install.when must name regular files' >&2; exit 3 ;; esac
        digest=$(sha256sum < "$file")
    else
        case "$kind" in *': No such file or directory'|*': Not a directory') digest=$(printf '' | sha256sum) ;;
            *) printf '%s\n' "$kind" >&2; exit 3 ;; esac
    fi
    digest=${digest%% *}
    printf '%s  %s\n' "$digest" "$file" >> "$tmp"
done
sha256sum < "$tmp"
"""
_MARKER_READ = r"""set -eu
export LC_ALL=C
path=$1/.offbox
[ ! -L /srv/offbox/envs ] && [ ! -L "$1" ] || exit 3
[ ! -L "$path" ] && [ ! -L "$path/install.sha256" ] || exit 3
if kind=$(stat --format=%F -- "$path/install.sha256" 2>&1); then
    case "$kind" in 'regular file'|'regular empty file') ;; *) exit 3 ;; esac
    cat -- "$path/install.sha256"
else
    case "$kind" in *': No such file or directory'|*': Not a directory')
        if parent=$(stat --format=%F -- "$path" 2>&1); then
            [ "$parent" = directory ] && [ -r "$path" ] && [ -x "$path" ] || exit 3
        else
            case "$parent" in *': No such file or directory') ;; *) printf '%s\n' "$parent" >&2; exit 3 ;; esac
        fi
        printf 'missing\n' ;;
        *) printf '%s\n' "$kind" >&2; exit 3 ;; esac
fi
"""
_MARKER_WRITE = r"""set -eu
umask 077
path=$1/.offbox
[ ! -L /srv/offbox/envs ] && [ ! -L "$1" ] || exit 3
[ ! -L "$path" ] && [ ! -L "$path/install.sha256" ] || exit 3
mkdir -p -- "$path"
tmp=$(mktemp "$path/install.XXXXXXXXXX")
trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
printf '%s\n' "$2" > "$tmp"
mv -f -- "$tmp" "$path/install.sha256"
"""


def install_paths(names: Sequence[str]) -> list[str]:
    for name in names:
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))
            or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise PreconditionError("Unsafe install.when path", "Use relative file paths inside the worktree without dot components.")
    return list(names)


class Executor:
    def __init__(self, paths: Paths, runner: Runner, *, clock: Clock | None = None, id_factory: Callable[[], str] | None = None) -> None:
        self.paths = paths
        self.runner = runner
        self.clock = clock or SystemClock()
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def _status(self, host: HostState, work: Worktree, run_id: str, *, timeout: float = 2) -> int | None:
        result = self.runner.run(
            ssh_argv(host.name, ["docker", "exec", work.container, "cat", f"/run/offbox/exec/{run_id}.code"]),
            check=False,
            timeout=timeout,
        )
        if result.returncode == 0 and re.fullmatch(r"(?:0|[1-9][0-9]{0,2})\n?", result.stdout):
            status = int(result.stdout)
            if status <= 255:
                return status
        return None

    def interrupt(self, process: StreamProcess, state: SignalState, host: HostState, work: Worktree, run_id: str) -> int:
        """Relay a recorded signal within the ten-second cleanup budget."""
        number = state.number or signal.SIGINT
        deadline = (state.received_at if state.received_at is not None else self.clock.now()) + 9
        accepted = False
        confirmed = False
        while self.clock.now() < deadline:
            local_status = process.poll()
            if local_status is not None and (0 <= local_status < 255 or confirmed):
                return 128 + number
            try:
                if not confirmed and not accepted:
                    result = self.runner.run(
                        ssh_argv(
                            host.name, ["docker", "exec", work.container, "offbox-run", "--signal", signal.Signals(number).name[3:], run_id]
                        ),
                        check=False,
                        timeout=min(1, deadline - self.clock.now()),
                    )
                    accepted = result.returncode == 0
                remaining = deadline - self.clock.now()
                if not confirmed and remaining > 0:
                    confirmed = self._status(host, work, run_id, timeout=min(1, remaining)) is not None
            except OffboxError:
                logger.debug("Signal completion not yet available", exc_info=True)
            self.clock.sleep(min(0.1, max(0, deadline - self.clock.now())))
        if process.poll() is None:
            try:
                process.kill()
            except OffboxError:
                logger.exception("Cannot reap interrupted local SSH process")
                console.warn("Local SSH cleanup failed. Fix: inspect and stop the local SSH process.")
        if not confirmed:
            cleanup = shlex.join(
                ["offbox", "host", "ssh", host.name, "--", "docker", "exec", work.container, "offbox-run", "--signal", "KILL", run_id]
            )
            console.warn(f"Remote command cleanup could not be confirmed. Fix: {cleanup}")
        return 128 + number

    def run(
        self,
        work: Worktree,
        host: HostState,
        command: Sequence[str],
        *,
        directory: str = "/work",
        env: Mapping[str, str] | None = None,
        tty: bool = False,
        force_pty: bool = False,
        stdin: bool = False,
        diagnostics: bool = False,
    ) -> int:
        run_id = self.id_factory()
        register_secrets(env or {})
        argv = command_argv(
            work,
            host,
            command,
            run_id=run_id,
            directory=directory,
            env=env,
            tty=tty,
            force_pty=force_pty,
            stdin=stdin,
            diagnostics=diagnostics,
        )
        if tty:
            status = self.runner.stream(argv, tty=True, stdin=True)
        else:
            state = SignalState(self.clock)
            with capture_signals(state):
                process = self.runner.start_stream(argv, stdin=stdin)
                try:
                    while True:
                        if state.number is not None:
                            return self.interrupt(process, state, host, work, run_id)
                        observed = process.poll()
                        if observed is not None:
                            status = observed
                            break
                        self.clock.sleep(0.05)
                finally:
                    if state.number is None and process.poll() is None:
                        process.kill()
        if status == 255:
            try:
                master = self.runner.run(["ssh", "-O", "check", f"offbox-{host.name}"], check=False, timeout=2)
                if master.returncode == 0:
                    confirmed_status = self._status(host, work, run_id)
                    if confirmed_status is not None:
                        return confirmed_status
            except OffboxError as exc:
                raise ConnectionError_("SSH connection lost before command completion was confirmed", _CONNECTION_FIX) from exc
            raise ConnectionError_("SSH connection lost before command completion was confirmed", _CONNECTION_FIX)
        return status if status >= 0 else 128 - status

    def _remote(self, host: HostState, argv: Sequence[str], *, input: str | None = None) -> str:
        result = self.runner.run(ssh_argv(host.name, argv), check=False, timeout=30, input=input)
        if result.returncode == 255:
            raise ConnectionError_("SSH connection lost during installation setup", _CONNECTION_FIX)
        if result.returncode:
            raise CommandFailed(argv, result.returncode, result.stderr)
        return result.stdout.strip()

    @contextmanager
    def installation_lock(self, work: Worktree) -> Generator[None]:
        validate_env_name(work.name)
        directory = self.paths.state / "install-locks"
        try:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            metadata = directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise PreconditionError("Unsafe install lock directory", f"Restore an owned directory at {directory}.")
            descriptor = os.open(directory / work.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise PreconditionError("Unsafe install lock", "Remove the conflicting install lock and retry.")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise PreconditionError(
                        "Installation is already running for this environment", "Retry after the current installation finishes."
                    ) from exc
                yield
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise PreconditionError("Cannot acquire installation lock", "Check local state directory permissions and retry.") from exc

    def install_hash(self, work: Worktree, host: HostState, names: Sequence[str]) -> str:
        result = self._remote(host, ["sh", "-s", "--", work.work, *install_paths(names)], input=_HASH)
        match = re.fullmatch(r"([a-f0-9]{64})  -", result)
        if match is None:
            raise PreconditionError("Invalid remote install hash", "Check the host sha256sum command and retry.")
        return match[1]

    def _stage(self, work: Worktree, host: HostState, command: str, *, env: Mapping[str, str] | None = None) -> None:
        status = self.run(work, host, ["sh", "-c", command], env=env, diagnostics=True)
        if status:
            if status in (130, 143):
                raise OffboxError("Setup command interrupted", "Retry offbox up or the requested command when ready.", status)
            raise CommandFailed(["sh", "-c", command], status, "Installation or stack startup failed; the environment is retained.")

    def install(
        self, work: Worktree, host: HostState, config: InstallConfig, *, always: bool = False, env: Mapping[str, str] | None = None
    ) -> None:
        if config.command is None:
            return
        install_paths(config.when)
        with self.installation_lock(work):
            digest = self.install_hash(work, host, config.when)
            marker = self._remote(host, ["sh", "-s", "--", work.directory], input=_MARKER_READ)
            if marker != "missing" and _DIGEST.fullmatch(marker) is None:
                raise PreconditionError("Invalid installation marker", "Remove the host .offbox/install.sha256 marker and retry.")
            if not always and marker == digest:
                return
            console.step("Installing dependencies" if always else "lockfile changed, running install")
            self._stage(work, host, config.command, env=env)
            digest = self.install_hash(work, host, config.when)
            self._remote(host, ["sh", "-s", "--", work.directory, digest], input=_MARKER_WRITE)

    def up_stages(self, work: Worktree, host: HostState, project: ProjectConfig, *, no_stack: bool = False) -> None:
        env = execution_env(project.exec.env, {}, tty=False)
        if any((work.root / name).is_file() for name in (".tool-versions", "mise.toml")):
            console.step("Installing project tools with mise")
            self._stage(work, host, "mise install", env={**env, "MISE_YES": "1"})
        self.install(work, host, project.install, always=True, env=env)
        stack, _ = project.stack.commands(work.root)
        if stack and not no_stack:
            console.step("Starting project stack")
            self._stage(work, host, stack, env=env)

    def execute(self, options: ExecOptions, *, cwd: Path | None = None) -> int:
        from offbox.runtime import Runtime

        runtime = Runtime(self.paths, self.runner, clock=self.clock)
        work, _, host = runtime.current(cwd)
        container = runtime.inspect(host, work)
        if container is None or not container.state.running:
            raise PreconditionError("Environment is not running", "offbox up")
        labels = container.config.labels or {}
        if (
            container.name.lstrip("/") != work.container
            or labels.get("offbox.env") != work.name
            or labels.get("offbox.worktree") != str(work.root)
        ):
            raise PreconditionError("Environment ownership labels do not match", "Run offbox list and resolve conflicting resources.")
        project = load_config(work.root / "offbox.toml", ProjectConfig)
        install_paths(project.install.when)
        local = (cwd or Path.cwd()).resolve()
        try:
            relative = local.relative_to(work.root)
        except ValueError as exc:
            raise PreconditionError("Current directory is outside the worktree", "Run offbox exec inside the worktree.") from exc
        directory = "/work" + ("/" + relative.as_posix() if relative.parts else "")
        tty = options.tty if options.tty is not None else sys.stdin.isatty() and sys.stdout.isatty()
        env = execution_env(project.exec.env, options.overrides, tty=tty)
        command_argv(work, host, options.command, run_id=self.id_factory(), directory=directory, env=env)
        if not options.no_sync:
            runtime.sync.flush(work, host)
        self.install(work, host, project.install, env=env)
        return self.run(
            work,
            host,
            options.command,
            directory=directory,
            env=env,
            tty=tty,
            force_pty=options.tty is True and not (sys.stdin.isatty() and sys.stdout.isatty()),
            stdin=options.stdin,
        )
