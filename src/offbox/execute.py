import contextvars
import fcntl
import hashlib
import logging
import os
import re
import shlex
import signal
import stat
import sys
import threading
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path, PurePosixPath
from types import FrameType

from pydantic import Field, SecretStr, ValidationError

from offbox import console
from offbox.config import InstallConfig, Model, PortsConfig, ProjectConfig, load_config
from offbox.errors import CommandFailed, ConnectionError_, OffboxError, PreconditionError, UsageError
from offbox.host import host_operation
from offbox.host.ready import Clock, SystemClock
from offbox.locking import acquire_lock
from offbox.logging import register_env_values, register_secrets
from offbox.naming import Worktree, validate_env_name
from offbox.paths import Paths
from offbox.ports import PortContainer, PortManager, PortSnapshot, compose_discovery
from offbox.providers import load_provider
from offbox.runner import Runner, StreamProcess
from offbox.share import Sharing, check_sharing
from offbox.ssh import ssh_argv
from offbox.state import HostState
from offbox.timings import measured, stage

logger = logging.getLogger(__name__)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CONNECTION_FIX = "offbox status"
_CREDENTIAL_ENV = frozenset(
    {"HCLOUD_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_OAUTH_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN", "SSH_AUTH_SOCK"}
)
_PUBLIC_PROVIDER_ENV = frozenset({"GOOGLE_PROJECT"})
INSTALL_WAIT_SECONDS = 600
PORT_RESTORE_JOIN_SECONDS = 10
INTERRUPTED_PORT_RESTORE_JOIN_SECONDS = 1
_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class ExecOptions(Model):
    command: list[str]
    tty: bool | None = None
    no_sync: bool = False
    stdin: bool = False
    overrides: dict[str, SecretStr] = Field(default_factory=dict)


class InstallProbe(Model):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    marker: str = Field(pattern=r"^(?:missing|[a-f0-9]{64})$")


class Preflight(Model):
    containers: list[PortContainer]
    install: str
    compose: str
    compose_failed: bool


def parse_env(entries: Sequence[str]) -> dict[str, SecretStr]:
    result: dict[str, SecretStr] = {}
    for entry in entries:
        name, separator, value = entry.partition("=")
        if not separator or _ENV_NAME.fullmatch(name) is None or "\0" in value:
            raise UsageError("Invalid --env value", "Use --env NAME=value with a valid environment variable name.")
        register_env_values({name: value})
        result[name] = SecretStr(value)
    return result


def credential_env_names(paths: Paths) -> frozenset[str]:
    """Variables that provider manifests read as credentials; exec.env must never forward them from the laptop."""
    directories = [Path(str(entry)) for entry in files("offbox").joinpath("_data", "providers").iterdir() if entry.is_dir()]
    custom = paths.config / "providers"
    if custom.is_dir():
        directories.extend(sorted(path for path in custom.iterdir() if path.is_dir()))
    names = set(_CREDENTIAL_ENV)
    for directory in directories:
        if not (directory / "provider.toml").is_file():
            continue
        try:
            names.update(load_provider(directory).manifest.credentials_env)
        except PreconditionError:
            logger.warning("Ignoring unreadable provider manifest %s", directory, exc_info=True)
    return frozenset(names - _PUBLIC_PROVIDER_ENV)


def check_exec_env(paths: Paths, configured: Sequence[str]) -> None:
    blocked = sorted(set(configured) & credential_env_names(paths))
    if blocked:
        raise PreconditionError(
            "exec.env must not forward provider credentials: " + ", ".join(blocked),
            "Remove them from exec.env in offbox.toml; pass a value for one command with offbox exec --env NAME=value.",
        )


def unapproved_exec_env(configured: Sequence[str], approved: Sequence[str] | None) -> list[str]:
    return sorted(set(configured) - set(approved or ()))


def execution_env(
    configured: Sequence[str], overrides: Mapping[str, SecretStr], *, tty: bool, local: Mapping[str, str] | None = None
) -> dict[str, str]:
    values = os.environ if local is None else local
    names = [*configured, *(["TERM", "COLORTERM", "LANG"] if tty else [])]
    if any(_ENV_NAME.fullmatch(name) is None for name in names):
        raise PreconditionError("Invalid exec.env name", "Configure exec.env as environment variable names.")
    result = {name: values[name] for name in names if name in values}
    result.update({name: value.get_secret_value() for name, value in overrides.items()})
    register_env_values({name: value for name, value in result.items() if name in configured or name in overrides})
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
    remote.extend(["-e", f"OFFBOX_ENV={work.name}"])
    remote.extend([work.container, "offbox-run", *(["--tty"] if tty else []), run_id, directory, "--", *command])
    if tty:
        return ssh_argv(host.name, remote, options=["-tt" if force_pty else "-t"])
    if stdin:
        if diagnostics:
            remote = ["sh", "-c", "exec " + shlex.join(remote) + " >&2"]
        return ssh_argv(host.name, remote)
    return ssh_argv(host.name, ["bash", "-c", WATCH_SCRIPT, "offbox-watch", work.container, run_id, "1" if diagnostics else "0", *remote])


class SignalState:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.number: int | None = None
        self.received_at: float | None = None

    def receive(self, number: int, frame: FrameType | None) -> None:
        if self.number is None:
            self.number = number
            self.received_at = self.clock.now()


WATCH_SCRIPT = r"""set -u
container=$1
run_id=$2
diagnostics=$3
shift 3
stop() {
    sent=
    attempt=0
    while [ "$attempt" -lt 60 ]; do
        state=$(docker exec "$container" sh -c 'if [ -f "$1.code" ]; then echo done; elif [ -f "$1.pid" ]; then echo running; else echo pending; fi' \
            sh "/run/offbox/exec/$run_id" 2>/dev/null) || return 0
        case "$state" in
            done) return 0 ;;
            pending) kill -0 "$child" 2>/dev/null || return 0 ;;
            *)
                if [ -z "$sent" ]; then
                    docker exec "$container" offbox-run --signal TERM "$run_id" >/dev/null 2>&1 && sent=TERM
                elif [ "$sent" = TERM ] && [ "$attempt" -ge 50 ]; then
                    docker exec "$container" offbox-run --signal KILL "$run_id" >/dev/null 2>&1 && sent=KILL
                fi
                ;;
        esac
        sleep 0.2
        attempt=$((attempt + 1))
    done
}
exec 3<&0
if [ "$diagnostics" = 1 ]; then
    "$@" </dev/null 3<&- >&2 &
else
    "$@" </dev/null 3<&- &
fi
child=$!
cat <&3 >/dev/null 2>&1 3<&- &
reader=$!
exec 3<&-
done_pid=
wait -n -p done_pid "$child" "$reader"
status=$?
if [ "$done_pid" = "$child" ]; then
    kill "$reader" 2>/dev/null
    wait "$reader" 2>/dev/null
    [ "$status" = 0 ] || stop
    exit "$status"
fi
stop
wait "$child"
"""
"""Run the docker exec client on the host and stop the remote run once the local client's stdin pipe closes."""


@contextmanager
def capture_signals(state: SignalState) -> Generator[None]:
    previous = {number: signal.getsignal(number) for number in _SIGNALS}
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
cd -- "$root" || exit 3
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

_PROBE_FUNCTIONS = "offbox_hash() (\n" + _HASH + "\n)\noffbox_marker() (\n" + _MARKER_READ + "\n)\n"
INSTALL_PROBE_SCRIPT = (
    "set -eu\n"
    + _PROBE_FUNCTIONS
    + "offbox_work=$1\noffbox_directory=$2\nshift 2\n"
    + 'offbox_hash "$offbox_work" "$@"\noffbox_marker "$offbox_directory"\n'
)
PREFLIGHT_SCRIPT = (
    "set -eu\n"
    + _PROBE_FUNCTIONS
    + r"""name=$1
worktree=$2
container=$3
probe=$4
discover=$5
work=$6
directory=$7
shift 7
failure=$(mktemp)
trap 'rm -f -- "$failure"' EXIT HUP INT TERM
if ! inspection=$(docker container inspect "$container" 2>"$failure"); then
    if grep -qi -e 'no such container' -e 'no such object' "$failure"; then
        inspection='[]'
    else
        cat -- "$failure" >&2
        exit 1
    fi
fi
install=
compose=
compose_failed=false
if printf '%s\n' "$inspection" | jq -e --arg name "$name" --arg worktree "$worktree" --arg container "$container" '
    length == 1 and .[0].Name == ("/" + $container)
    and .[0].Config.Labels["offbox.env"] == $name
    and .[0].Config.Labels["offbox.worktree"] == $worktree
    and .[0].State.Running == true
' >/dev/null; then
    if [ "$probe" = 1 ]; then
        hashed=$(offbox_hash "$work" "$@")
        marker=$(offbox_marker "$directory")
        install="$hashed
$marker"
    fi
    if [ "$discover" = 1 ]; then
        if ! compose=$(docker exec -u dev -w /work "$container" docker compose ps --format json 2>"$failure"); then
            cat -- "$failure" >&2
            compose=
            compose_failed=true
        fi
    fi
fi
printf '%s\n' "$inspection" | jq -c --arg install "$install" --arg compose "$compose" --argjson compose_failed "$compose_failed" \
    '{containers: ., install: $install, compose: $compose, compose_failed: $compose_failed}'
"""
)


def parse_install_probe(text: str) -> InstallProbe:
    hash_line, separator, marker = text.partition("\n")
    match = re.fullmatch(r"([a-f0-9]{64})  -", hash_line)
    if not separator or match is None:
        raise PreconditionError("Invalid remote install hash", "Check the host sha256sum command and retry.")
    try:
        return InstallProbe(digest=match[1], marker=marker.rstrip())
    except ValidationError as exc:
        raise PreconditionError("Invalid installation marker", "Remove the host .offbox/install.sha256 marker and retry.") from exc


def install_marker(files_digest: str, command: str) -> str:
    """The marker value for an install: the install.when digest bound to the command that produced it."""
    return hashlib.sha256(f"{files_digest}\n{command}\n".encode()).hexdigest()


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
        self.interrupted = False

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
        relayed = "TERM" if number == signal.SIGHUP else signal.Signals(number).name[3:]
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
                        ssh_argv(host.name, ["docker", "exec", work.container, "offbox-run", "--signal", relayed, run_id]),
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
        started: Callable[[], None] | None = None,
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
            if started is not None:
                started()
            status = self.runner.stream(argv, tty=True, stdin=True)
        else:
            state = SignalState(self.clock)
            with capture_signals(state):
                process = self.runner.start_stream(argv, stdin=stdin, keepalive=not stdin)
                try:
                    if started is not None:
                        started()
                    while True:
                        if state.number is not None:
                            self.interrupted = True
                            return self.interrupt(process, state, host, work, run_id)
                        observed = process.wait(timeout=0.1)
                        if observed is not None:
                            status = observed
                            break
                finally:
                    if state.number is None and process.poll() is None:
                        process.kill()
        if status == 255:
            cleanup = shlex.join(
                ["offbox", "host", "ssh", host.name, "--", "docker", "exec", work.container, "offbox-run", "--signal", "TERM", run_id]
            )
            fix = f"Completion is unknown; do not retry blindly. Check offbox status; stop this run if needed: {cleanup}"
            try:
                master = self.runner.run(["ssh", "-O", "check", f"offbox-{host.name}"], check=False, timeout=2)
                if master.returncode == 0:
                    confirmed_status = self._status(host, work, run_id)
                    if confirmed_status is not None:
                        return confirmed_status
            except OffboxError as exc:
                raise ConnectionError_("SSH connection lost before command completion was confirmed", fix) from exc
            raise ConnectionError_("SSH connection lost before command completion was confirmed", fix)
        return status if status >= 0 else 128 - status

    def _remote(self, host: HostState, argv: Sequence[str], *, input: str | None = None, timeout: float = 30) -> str:
        result = self.runner.run(ssh_argv(host.name, argv), check=False, timeout=timeout, input=input)
        if result.returncode == 255:
            raise ConnectionError_("SSH connection lost during installation setup", _CONNECTION_FIX)
        if result.returncode:
            raise CommandFailed(argv, result.returncode, result.stderr)
        return result.stdout

    @contextmanager
    def installation_lock(self, work: Worktree, *, wait: float = 0) -> Generator[bool]:
        """Yield whether the lock was contended, so callers re-check work another process may have finished."""
        validate_env_name(work.name)
        directory = self.paths.state / "install-locks"
        failure = PreconditionError("Cannot acquire installation lock", "Check local state directory permissions and retry.")
        try:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            metadata = directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise PreconditionError("Unsafe install lock directory", f"Restore an owned directory at {directory}.")
            descriptor = os.open(directory / work.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise failure from exc
        try:
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise PreconditionError("Unsafe install lock", "Remove the conflicting install lock and retry.")
                waited = False
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if wait <= 0:
                        raise PreconditionError(
                            "Installation is already running for this environment", "Retry after the current installation finishes."
                        ) from None
                    console.step("Waiting for another installation in this environment to finish")
                    acquire_lock(
                        descriptor,
                        timeout=wait,
                        clock=self.clock,
                        message="Installation is still running for this environment",
                        fix="Retry after the current installation finishes.",
                    )
                    waited = True
            except OSError as exc:
                raise failure from exc
            yield waited
        finally:
            os.close(descriptor)

    def install_hash(self, work: Worktree, host: HostState, names: Sequence[str]) -> str:
        result = self._remote(host, ["sh", "-s", "--", work.work, *install_paths(names)], input=_HASH).strip()
        match = re.fullmatch(r"([a-f0-9]{64})  -", result)
        if match is None:
            raise PreconditionError("Invalid remote install hash", "Check the host sha256sum command and retry.")
        return match[1]

    def install_probe(self, work: Worktree, host: HostState, names: Sequence[str]) -> InstallProbe:
        result = self._remote(
            host, ["sh", "-s", "--", work.work, work.directory, *install_paths(names)], input=INSTALL_PROBE_SCRIPT, timeout=60
        )
        return parse_install_probe(result)

    def _stage(self, work: Worktree, host: HostState, command: str, *, env: Mapping[str, str] | None = None) -> None:
        status = self.run(work, host, ["sh", "-c", command], env=env, diagnostics=True)
        if status:
            if status in (130, 143):
                raise OffboxError("Setup command interrupted", "Retry offbox up or the requested command when ready.", status)
            raise CommandFailed(["sh", "-c", command], status, "Installation or stack startup failed; the environment is retained.")

    @measured("Dependencies")
    def install(
        self,
        work: Worktree,
        host: HostState,
        config: InstallConfig,
        *,
        always: bool = False,
        env: Mapping[str, str] | None = None,
        probe: InstallProbe | None = None,
        wait: float = INSTALL_WAIT_SECONDS,
    ) -> None:
        if config.command is None:
            return
        install_paths(config.when)
        command = config.command
        if not always:
            current = probe or self.install_probe(work, host, config.when)
            if current.marker == install_marker(current.digest, command):
                return
        with self.installation_lock(work, wait=wait) as waited:
            if waited and not always:
                current = self.install_probe(work, host, config.when)
                if current.marker == install_marker(current.digest, command):
                    return
            console.step("Installing dependencies" if always else "install inputs changed, running install")
            self._stage(work, host, command, env=env)
            digest = install_marker(self.install_hash(work, host, config.when), command)
            self._remote(host, ["sh", "-s", "--", work.directory, digest], input=_MARKER_WRITE)

    def up_stages(self, work: Worktree, host: HostState, project: ProjectConfig, *, no_stack: bool = False) -> None:
        env = execution_env(project.exec.env, {}, tty=False)
        if any((work.root / name).is_file() for name in (".tool-versions", "mise.toml")):
            console.step("Installing project tools with mise")
            with stage("Project tools"):
                self._stage(work, host, "mise install", env={**env, "MISE_YES": "1"})
        self.install(work, host, project.install, always=True, env=env, wait=0)
        stack, _ = project.stack.commands(work.root)
        if stack and not no_stack:
            console.step("Starting project stack")
            with stage("Stack startup"):
                self._stage(work, host, stack, env=env)

    def preflight(self, work: Worktree, host: HostState, project: ProjectConfig) -> Preflight:
        """Inspect the container, probe the install marker and list Compose ports in one SSH round trip."""
        probe = project.install.command is not None
        discover = compose_discovery(work.root, project.ports)
        result = self.runner.run(
            ssh_argv(
                host.name,
                [
                    "sh",
                    "-s",
                    "--",
                    work.name,
                    str(work.root),
                    work.container,
                    "1" if probe else "0",
                    "1" if discover else "0",
                    work.work,
                    work.directory,
                    *install_paths(project.install.when),
                ],
            ),
            check=False,
            timeout=60,
            input=PREFLIGHT_SCRIPT,
        )
        if result.returncode == 255:
            raise ConnectionError_(f"Cannot reach host {host.name}", f"Run offbox host ssh {host.name} and check connectivity.")
        if result.returncode:
            raise CommandFailed(result.argv, result.returncode, result.stderr)
        try:
            return Preflight.model_validate_json(result.stdout)
        except ValidationError as exc:
            raise PreconditionError("Invalid environment preflight response", "Check the host Docker daemon and offbox logs.") from exc

    def _owned(self, work: Worktree, preflight: Preflight) -> bool:
        if not preflight.containers:
            return False
        if len(preflight.containers) != 1:
            raise PreconditionError("Environment ownership labels do not match", "Run offbox list and resolve conflicting resources.")
        container = preflight.containers[0]
        labels = container.config.labels or {}
        if (
            container.name.lstrip("/") != work.container
            or labels.get("offbox.env") != work.name
            or labels.get("offbox.worktree") != str(work.root)
        ):
            raise PreconditionError("Environment ownership labels do not match", "Run offbox list and resolve conflicting resources.")
        return container.state.running

    def restore_ports(
        self, manager: PortManager, work: Worktree, host: HostState, config: PortsConfig, snapshot: PortSnapshot | None
    ) -> list[str]:
        """Restore forwards without blocking on a busy host lock; failures become warnings and never affect the command."""
        warnings: list[str] = []
        try:
            with host_operation(self.paths, host.name, clock=self.clock):
                manager.restore(work, host, config, snapshot=snapshot, warn=warnings.append)
        except OffboxError as exc:
            if isinstance(exc.__cause__, BlockingIOError):
                logger.info("port restore skipped because another host operation holds the lock")
            else:
                logger.info("port restore skipped", exc_info=True)
                warnings.append(f"Port forwards were not restored: {exc.message}. Fix: {exc.fix or 'run offbox ports.'}")
        except Exception:
            logger.exception("unexpected port restore failure")
            warnings.append("Port forwards were not restored after an unexpected error. Fix: check offbox logs and run offbox ports.")
        return warnings

    def execute(self, options: ExecOptions, *, cwd: Path | None = None) -> int:
        from offbox.runtime import Runtime

        runtime = Runtime(self.paths, self.runner, clock=self.clock)
        work, recorded, host = runtime.current(cwd)
        project = load_config(work.root / "offbox.toml", ProjectConfig)
        install_paths(project.install.when)
        check_exec_env(self.paths, project.exec.env)
        unapproved = unapproved_exec_env(project.exec.env, recorded.approved_exec_env)
        if unapproved:
            raise PreconditionError(
                "exec.env forwards variables that offbox up has not approved: " + ", ".join(unapproved),
                "Review exec.env in offbox.toml, then run offbox up to approve it.",
            )
        check_sharing(project, recorded)
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
        preflight = self.preflight(work, host, project)
        if not self._owned(work, preflight):
            if not options.no_sync:
                try:
                    runtime.sync.pause(work, host)
                except OffboxError:
                    logger.warning("Cannot pause sync for a stopped environment", exc_info=True)
            raise PreconditionError("Environment is not running", "offbox up")
        Sharing(self.runner).apply(runtime.store, work, host, project, force=False, git=not options.no_sync)
        probe = parse_install_probe(preflight.install) if project.install.command is not None else None
        self.install(work, host, project.install, env=env, probe=probe)
        snapshot = None if preflight.compose_failed else PortSnapshot(containers=preflight.containers, compose=preflight.compose)
        warnings: list[str] = []
        deferred = tty

        def restore() -> None:
            for message in self.restore_ports(runtime.ports, work, host, project.ports, snapshot):
                if deferred:
                    warnings.append(message)
                else:
                    console.warn(message)

        context = contextvars.copy_context()
        worker = threading.Thread(target=context.run, args=(restore,), name="offbox-port-restore", daemon=True)
        try:
            with stage("Command execution"):
                return self.run(
                    work,
                    host,
                    options.command,
                    directory=directory,
                    env=env,
                    tty=tty,
                    force_pty=options.tty is True and not (sys.stdin.isatty() and sys.stdout.isatty()),
                    stdin=options.stdin,
                    started=worker.start,
                )
        finally:
            if worker.is_alive():
                worker.join(INTERRUPTED_PORT_RESTORE_JOIN_SECONDS if self.interrupted else PORT_RESTORE_JOIN_SECONDS)
            for message in warnings:
                console.warn(message)
            if worker.is_alive():
                console.warn("Port forwards are still being restored. Fix: run offbox ports to finish restoring them.")
