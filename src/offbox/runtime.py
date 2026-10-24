import hashlib
import logging
import math
import re
import shlex
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from offbox import DEFAULT_IMAGE, __version__, console
from offbox.config import EnvConfig, Model, ProjectConfig, load_config
from offbox.errors import CommandFailed, ConnectionError_, OffboxError, PreconditionError
from offbox.host import host_operation, select_host
from offbox.host.ready import Clock, SystemClock
from offbox.naming import Worktree, find_worktree, validate_env_name
from offbox.paths import Paths
from offbox.ports import PortManager
from offbox.runner import Result, Runner
from offbox.ssh import ssh_argv
from offbox.state import EnvState, HostState, StateStore
from offbox.sync import SyncManager
from offbox.timings import measured

logger = logging.getLogger(__name__)


class DockerModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, hide_input_in_errors=True)


class ContainerStatus(DockerModel):
    running: bool = Field(alias="Running")
    status: str = Field(default="unknown", alias="Status")
    exit_code: int = Field(default=0, alias="ExitCode")


class ContainerConfig(DockerModel):
    labels: dict[str, str] | None = Field(default=None, alias="Labels")


class ContainerInfo(DockerModel):
    name: str = Field(alias="Name")
    image: str = Field(alias="Image")
    state: ContainerStatus = Field(alias="State")
    config: ContainerConfig = Field(alias="Config")


class VolumeInfo(DockerModel):
    name: str = Field(alias="Name")
    labels: dict[str, str] | None = Field(default=None, alias="Labels")


class BuiltImage(Model):
    tag: str
    digest: str


class HostResources(Model):
    cpus: int = Field(gt=0)
    memory: int = Field(gt=0)


class EnvSummary(Model):
    name: str
    host: str
    worktree: Path
    status: str
    running: bool = False
    image_digest: str | None = None
    fix: str | None = None


class EnvReport(Model):
    envs: list[EnvSummary] = Field(default_factory=list[EnvSummary])


class GcTarget(Model):
    host: str
    worktree: Worktree
    reason: str
    registered: bool


class GcPlan(Model):
    targets: list[GcTarget] = Field(default_factory=list[GcTarget])
    running: list[GcTarget] = Field(default_factory=list[GcTarget])


def worktree_exists(root: Path) -> bool:
    try:
        root.stat()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise PreconditionError(f"Cannot inspect worktree {root}", "Restore access to the worktree before running GC.") from exc


def _labels(work: Worktree) -> list[str]:
    return ["--label", f"offbox.env={work.name}", "--label", f"offbox.worktree={work.root}", "--label", f"offbox.version={__version__}"]


def volume_command(work: Worktree) -> list[str]:
    return ["docker", "volume", "create", *_labels(work), work.volume]


def _byte_limit(value: str, field: str, *, minimum: int = 1) -> None:
    match = re.fullmatch(r"(\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?)(?: ?(b|[kmgtp](?:b|ib)?))?", value, flags=re.IGNORECASE)
    if match is None:
        raise PreconditionError(f"Invalid {field} size", f"Set env.{field} to a positive Docker byte size, for example 8g.")
    suffix = (match[2] or "b").lower()
    exponent = {"b": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5}[suffix[0]]
    try:
        amount = Decimal(match[1]) * (1024**exponent)
        if not amount.is_finite() or amount < minimum or amount > 2**63 - 1:
            raise PreconditionError(f"Invalid {field} size", f"Set env.{field} between {minimum} and {2**63 - 1} bytes.")
    except DecimalException as exc:
        raise PreconditionError(f"Invalid {field} size", f"Set env.{field} to a finite Docker byte size.") from exc


def container_command(work: Worktree, config: EnvConfig, host_cpus: int, host_memory: int) -> list[str]:
    if config.dockerfile is not None or config.image is None:
        raise PreconditionError(
            "Custom Dockerfile builds require source sync", "Set env.image to a prebuilt image until source sync is available."
        )
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]*", config.image) is None:
        raise PreconditionError("Invalid container image reference", "Set env.image to a valid prebuilt Docker image reference.")
    cpus, memory = config.resource_limits(host_cpus, host_memory)
    if not math.isfinite(cpus) or cpus < 0.01 or cpus > host_cpus:
        raise PreconditionError("Invalid CPU limit", "Set env.cpus between 0.01 and the host CPU count.")
    _byte_limit(memory, "memory", minimum=6 * 1024**2)
    _byte_limit(config.shm_size, "shm_size")
    return [
        "docker",
        "run",
        "-d",
        "--name",
        work.container,
        "--hostname",
        work.name,
        "--privileged",
        "--cgroupns=private",
        "--network",
        "offbox",
        "--cpus",
        f"{cpus:g}",
        "--memory",
        memory,
        "--shm-size",
        config.shm_size,
        "--restart",
        "no",
        "-v",
        f"{work.work}:/work",
        "-v",
        f"{work.volume}:/var/lib/docker",
        *_labels(work),
        config.image,
    ]


_GUARD = """set -eu
name=$1
worktree=$2
container=$3
volume=$4
directory=/srv/offbox/envs/$name
for path in /srv/offbox/envs "$directory" "$directory/work"; do
    if [ -L "$path" ]; then
        printf '%s\\n' 'offbox: refusing a symlinked environment directory' >&2
        exit 3
    fi
done
container_id=$(docker ps -aq --filter "name=^/${container}$")
volume_name=$(docker volume ls -q --filter "name=^${volume}$")
if [ -n "$container_id" ]; then
    env_label=$(docker container inspect --format '{{index .Config.Labels "offbox.env"}}' "$container")
    path_label=$(docker container inspect --format '{{index .Config.Labels "offbox.worktree"}}' "$container")
    if [ "$env_label" != "$name" ] || [ "$path_label" != "$worktree" ]; then
        printf '%s\\n' 'offbox: refusing a container with conflicting ownership labels' >&2
        exit 3
    fi
fi
if [ -n "$volume_name" ]; then
    env_label=$(docker volume inspect --format '{{index .Labels "offbox.env"}}' "$volume")
    path_label=$(docker volume inspect --format '{{index .Labels "offbox.worktree"}}' "$volume")
    if [ "$env_label" != "$name" ] || [ "$path_label" != "$worktree" ]; then
        printf '%s\\n' 'offbox: refusing a volume with conflicting ownership labels' >&2
        exit 3
    fi
fi
"""


def mutation_script(
    work: Worktree, action: Literal["up", "fresh", "stop", "remove", "prune", "prepare"], command: Sequence[str] = (), *, pull: bool = True
) -> str:
    if action == "prepare":
        return _GUARD + '\nmkdir -p -- "$directory/work"\n'
    if action in ("up", "fresh"):
        if not command:
            raise PreconditionError("Container command is missing", "Provide a valid environment configuration.")
        fresh = "true" if action == "fresh" else "false"
        prepare_image = shlex.join(["docker", "pull", command[-1]]) + " >/dev/null" if pull else ":"
        return (
            _GUARD
            + f"""
if {fresh} || [ -z "$container_id" ]; then
    {prepare_image}
    if {fresh}; then
        if [ -n "$container_id" ]; then docker rm -f "$container" >/dev/null; fi
        if [ -n "$volume_name" ]; then docker volume rm "$volume" >/dev/null; fi
        volume_name=
    fi
    mkdir -p -- "$directory/work"
    if [ -z "$volume_name" ]; then {shlex.join(volume_command(work))} >/dev/null; fi
    {shlex.join(command)} >/dev/null
elif [ "$(docker container inspect --format '{{{{.State.Running}}}}' "$container")" != true ]; then
    docker start "$container" >/dev/null
fi
docker container inspect "$container"
"""
        )
    if action == "prune":
        return (
            _GUARD
            + """
if [ -n "$container_id" ] && [ "$(docker container inspect --format '{{.State.Running}}' "$container")" = true ]; then
    docker exec "$container" docker system prune -f >/dev/null
fi
"""
        )
    if action == "stop":
        return (
            _GUARD
            + """
if [ -n "$container_id" ] && [ "$(docker container inspect --format '{{.State.Running}}' "$container")" = true ]; then
    docker stop --time 30 "$container" >/dev/null
fi
"""
        )
    return (
        _GUARD
        + """
if [ -n "$container_id" ]; then docker rm -f "$container" >/dev/null; fi
if [ -n "$volume_name" ]; then docker volume rm "$volume" >/dev/null; fi
sudo -n rm -rf -- "$directory"
"""
    )


class Runtime:
    def __init__(self, paths: Paths, runner: Runner, clock: Clock | None = None) -> None:
        self.paths = paths
        self.runner = runner
        self.clock = clock or SystemClock()
        self.store = StateStore(paths.state)
        self.sync = SyncManager(runner)
        self.ports = PortManager(paths, runner, clock=self.clock)

    def remote(self, host: HostState, argv: Sequence[str], *, check: bool = True, timeout: float = 30, input: str | None = None) -> Result:
        result = self.runner.run(ssh_argv(host.name, argv), check=False, timeout=timeout, input=input)
        if result.returncode == 255:
            raise ConnectionError_(f"Cannot reach host {host.name}", f"Run offbox host ssh {host.name} and check connectivity.")
        if check and result.returncode:
            logger.error("remote failure stdout=%s stderr=%s", result.stdout, result.stderr)
            if "offbox: refusing" in result.stderr:
                raise PreconditionError(result.stderr.strip(), "Inspect the environment ownership and paths before retrying.")
            raise CommandFailed(result.argv, result.returncode, result.stderr)
        return result

    def inspect(self, host: HostState, work: Worktree, *, timeout: float = 30) -> ContainerInfo | None:
        result = self.remote(host, ["docker", "container", "inspect", work.container], check=False, timeout=timeout)
        if result.returncode:
            if "no such container" in result.stderr.lower() or "no such object" in result.stderr.lower():
                return None
            raise CommandFailed(result.argv, result.returncode, result.stderr)
        return self._container(result.stdout)

    def _container(self, text: str) -> ContainerInfo:
        try:
            values = TypeAdapter(list[ContainerInfo]).validate_json(text)
            if len(values) != 1:
                raise ValueError("Expected one container")
            return values[0]
        except (ValueError, ValidationError) as exc:
            raise PreconditionError(
                "Invalid Docker container inspection response", "Check the host Docker daemon and offbox logs."
            ) from exc

    def _owned(self, work: Worktree, container: ContainerInfo) -> None:
        labels = container.config.labels or {}
        if (
            container.name.lstrip("/") != work.container
            or labels.get("offbox.env") != work.name
            or labels.get("offbox.worktree") != str(work.root)
        ):
            raise PreconditionError(
                f"Container {work.container} has conflicting ownership", "Inspect its labels or choose a different worktree path."
            )

    def _host(self, name: str | None) -> HostState:
        host = select_host(self.paths, name)
        if host.phase != "ready":
            raise PreconditionError(f"Host {host.name} is not ready", "Finish host initialisation before running an environment.")
        return host

    def _recorded(self, work: Worktree) -> EnvState | None:
        env = self.store.read().envs.get(work.name)
        if env is not None and (env.name != work.name or env.worktree_path != work.root):
            raise PreconditionError("Environment name collides with another worktree", "Use a different worktree directory name.")
        return env

    def current(self, cwd: Path | None = None) -> tuple[Worktree, EnvState, HostState]:
        work = find_worktree(self.runner, cwd)
        env = self._recorded(work)
        if env is None:
            raise PreconditionError("Environment is not registered", "offbox up")
        return work, env, self._host(env.host)

    @measured("Container operation")
    def mutate(
        self,
        host: HostState,
        work: Worktree,
        action: Literal["up", "fresh", "stop", "remove", "prune", "prepare"],
        command: Sequence[str] = (),
        *,
        pull: bool = True,
    ) -> Result:
        return self.remote(
            host,
            ["flock", "/var/lib/offbox/lock", "sh", "-s", "--", work.name, str(work.root), work.container, work.volume],
            input=mutation_script(work, action, command, pull=pull),
            timeout=600,
        )

    def up(self, *, cwd: Path | None = None, host_name: str | None = None, fresh: bool = False, no_stack: bool = False) -> EnvState:
        from offbox.execute import Executor, execution_env, install_paths

        work = find_worktree(self.runner, cwd)
        project = load_config(work.root / "offbox.toml", ProjectConfig)
        install_paths(project.install.when)
        execution_env(project.exec.env, {}, tty=False)
        config = project.env
        dockerfile = self.dockerfile_path(work, config.dockerfile) if config.dockerfile is not None else None
        previous = self._recorded(work)
        if previous is not None and host_name is not None and previous.host != host_name:
            raise PreconditionError("Environment is registered on another host", "Remove it before selecting a different host.")
        host = self._host(previous.host if previous is not None else host_name)
        with host_operation(self.paths, host.name, wait_timeout=120, clock=self.clock):
            self.remote(host, ["test", "-f", "/var/lib/offbox/ready"], timeout=5)
            resource_result = self.remote(host, ["docker", "info", "--format", '{"cpus":{{.NCPU}},"memory":{{.MemTotal}}}'])
            try:
                resources = HostResources.model_validate_json(resource_result.stdout)
            except ValidationError as exc:
                raise PreconditionError("Invalid host resource information", "Check the host Docker daemon.") from exc
            resolved = config.model_copy(update={"dockerfile": None, "image": config.image or DEFAULT_IMAGE})
            command = container_command(work, resolved, resources.cpus, resources.memory)
            existing = self.inspect(host, work)
            if existing is not None:
                self._owned(work, existing)
            with self.store.transaction() as state:
                stored_host = state.hosts.get(host.name)
                if (
                    stored_host is None
                    or stored_host.model_dump(exclude={"control_master_pid"}) != host.model_dump(exclude={"control_master_pid"})
                    or state.envs.get(work.name) != previous
                ):
                    raise PreconditionError("Environment or host changed while preparing up", "Retry offbox up.")
                env = previous or EnvState(name=work.name, host=host.name, worktree_path=work.root, created_at=datetime.now(UTC))
                state.envs[env.name] = env
            self.mutate(host, work, "prepare")
            self.sync.ensure(work, host, project.sync)
            built: BuiltImage | None = None
            if dockerfile is not None:
                self.sync.flush(work, host)
                built = self.build_image(host, work, dockerfile)
                command[-1] = built.tag
            if fresh:
                self.sync.pause(work, host)
                self.ports.cancel(work, host)
            result = self.mutate(host, work, "fresh" if fresh else "up", command, pull=dockerfile is None)
            container = self._container(result.stdout)
            self._owned(work, container)
            self.wait_ready(host, work, container)
            if fresh:
                self.sync.ensure(work, host, project.sync)
            self.sync.flush(work, host)
            Executor(self.paths, self.runner, clock=self.clock).up_stages(work, host, project, no_stack=no_stack)
            ports = self.ports.restore(work, host, project.ports)
            for forward in ports.forwards:
                console.info(f"localhost:{forward.local} → {forward.env}:{forward.remote}")
            if built is not None and built.digest != container.image:
                console.warn("Custom image changed; run offbox up --fresh to use it. Existing Docker data is retained.")
            env.image_digest = container.image
            with self.store.transaction() as state:
                if state.envs.get(env.name) is None:
                    raise PreconditionError("Environment registration disappeared", "Run offbox list and retry up.")
                current = state.envs[env.name]
                if current.host != host.name or current.worktree_path != work.root:
                    raise PreconditionError("Environment registration changed", "Run offbox list before retrying.")
                current.image_digest = env.image_digest
                env = current
        console.success(f"Environment {env.name} ready on {host.name}")
        return env

    def dockerfile_path(self, work: Worktree, configured: Path) -> str:
        relative = configured
        if configured.is_absolute():
            try:
                relative = configured.relative_to(work.root)
            except ValueError as exc:
                raise PreconditionError("Dockerfile is outside the worktree", "Set env.dockerfile to a file inside the worktree.") from exc
        if ".." in relative.parts or relative == Path("."):
            raise PreconditionError("Dockerfile is outside the worktree", "Set env.dockerfile to a file inside the worktree.")
        candidate = work.root
        try:
            for component in relative.parts:
                candidate /= component
                if candidate.is_symlink():
                    raise PreconditionError("Dockerfile path contains a symlink", "Use a regular Dockerfile path inside the worktree.")
            if not candidate.is_file():
                raise PreconditionError("Dockerfile does not exist", "Create the configured Dockerfile before running offbox up.")
        except OSError as exc:
            raise PreconditionError("Cannot inspect Dockerfile", "Check Dockerfile permissions and retry.") from exc
        return relative.as_posix()

    def custom_image_tag(self, host: HostState, work: Worktree, dockerfile: str) -> str:
        from offbox.execute import Executor

        digest = Executor(self.paths, self.runner).install_hash(work, host, [dockerfile])
        try:
            local = hashlib.sha256((work.root / dockerfile).read_bytes()).hexdigest()
        except OSError as exc:
            raise PreconditionError("Cannot read the configured Dockerfile", "Check its permissions and run offbox up.") from exc
        expected = hashlib.sha256(f"{local}  {dockerfile}\n".encode()).hexdigest()
        if digest != expected:
            raise PreconditionError(
                "Configured Dockerfile changes are not synchronized", "Run offbox up --fresh after source sync completes."
            )
        project_hash = hashlib.sha256(str(work.root).encode()).hexdigest()[:16]
        return f"offbox-local/{project_hash}:{local}"

    @measured("Image build")
    def build_image(self, host: HostState, work: Worktree, dockerfile: str) -> BuiltImage:
        project_hash = hashlib.sha256(str(work.root).encode()).hexdigest()[:16]
        script = (
            _GUARD
            + """
target=$directory/work/$5
parent=$target
while [ "$parent" != "$directory/work" ]; do
    if [ -L "$parent" ]; then
        printf '%s\\n' 'offbox: refusing a symlinked Dockerfile path' >&2
        exit 3
    fi
    parent=${parent%/*}
done
if [ ! -f "$target" ] || [ ! -r "$target" ]; then
    printf '%s\\n' 'offbox: refusing a missing synced Dockerfile; check sync ignores' >&2
    exit 3
fi
file_hash=$(sha256sum < "$target" | cut -d' ' -f1)
case "$file_hash" in *[!a-f0-9]*|'') exit 3 ;; esac
[ "${#file_hash}" -eq 64 ] || exit 3
tag=offbox-local/$6:$file_hash
cached=$(docker image ls -q --no-trunc "$tag")
if [ -z "$cached" ]; then
    docker build -t "$tag" -f "$target" "$directory/work" >&2
fi
image_id=$(docker image inspect --format '{{json .Id}}' "$tag")
printf '{"tag":"%s","digest":%s}\\n' "$tag" "$image_id"
"""
        )
        result = self.remote(
            host,
            [
                "flock",
                "/var/lib/offbox/lock",
                "sh",
                "-s",
                "--",
                work.name,
                str(work.root),
                work.container,
                work.volume,
                dockerfile,
                project_hash,
            ],
            input=script,
            timeout=1800,
        )
        try:
            image = BuiltImage.model_validate_json(result.stdout)
        except ValidationError as exc:
            raise PreconditionError("Invalid custom image build result", "Inspect the Docker build log and retry offbox up.") from exc
        if (
            re.fullmatch(r"offbox-local/" + project_hash + r":[a-f0-9]{64}", image.tag) is None
            or re.fullmatch(r"sha256:[a-f0-9]{64}", image.digest) is None
        ):
            raise PreconditionError("Invalid custom image build result", "Inspect the Docker build log and retry offbox up.")
        return image

    def diagnostics(self, host: HostState, work: Worktree) -> None:
        for command in (
            ["docker", "logs", "--tail", "40", work.container],
            ["docker", "exec", work.container, "tail", "-n", "40", "/var/log/dockerd.log"],
        ):
            try:
                result = self.remote(host, command, check=False, timeout=5)
                logger.error("environment diagnostic: %s\n%s", result.stdout, result.stderr)
            except OffboxError:
                logger.exception("Cannot retrieve environment diagnostics")

    @measured("Container readiness")
    def wait_ready(self, host: HostState, work: Worktree, container: ContainerInfo, *, timeout: float = 60) -> None:
        deadline = self.clock.now() + timeout
        while self.clock.now() < deadline:
            if not container.state.running:
                self.diagnostics(host, work)
                raise PreconditionError(
                    f"Environment {work.name} exited with code {container.state.exit_code}",
                    "Inspect offbox logs and run offbox up --fresh.",
                )
            try:
                result = self.remote(
                    host,
                    [
                        "docker",
                        "exec",
                        work.container,
                        "sh",
                        "-c",
                        "test -f /run/offbox/ready && docker --host=unix:///var/run/docker.sock info >/dev/null 2>&1",
                    ],
                    check=False,
                    timeout=min(5, deadline - self.clock.now()),
                )
                if result.returncode == 0:
                    return
                self.clock.sleep(min(1, max(0, deadline - self.clock.now())))
                if self.clock.now() < deadline:
                    current = self.inspect(host, work, timeout=min(5, deadline - self.clock.now()))
                    if current is None:
                        raise PreconditionError("Environment container disappeared", "Run offbox up again.")
                    self._owned(work, current)
                    container = current
            except OffboxError as exc:
                if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                    self.clock.sleep(min(1, max(0, deadline - self.clock.now())))
                    continue
                self.diagnostics(host, work)
                raise
        self.diagnostics(host, work)
        raise PreconditionError(
            f"Environment {work.name} did not become ready in {timeout:g}s", "Inspect offbox logs and run offbox up --fresh."
        )

    def _check_record(self, work: Worktree, env: EnvState, host: HostState) -> None:
        state = self.store.read()
        current = state.envs.get(work.name)
        stored_host = state.hosts.get(host.name)
        if (
            stored_host is None
            or stored_host.model_dump(exclude={"control_master_pid"}) != host.model_dump(exclude={"control_master_pid"})
            or current is None
            or current.host != env.host
            or current.worktree_path != work.root
        ):
            raise PreconditionError("Environment changed while preparing the operation", "Run offbox list and retry.")

    def stop(self, *, cwd: Path | None = None) -> None:
        work, env, host = self.current(cwd)
        with host_operation(self.paths, host.name):
            self._check_record(work, env, host)
            self.ports.cancel(work, host)
            self.sync.pause(work, host)
            self.mutate(host, work, "stop")
        console.success(f"Stopped {work.name}; data retained")

    def _remove(self, host: HostState, work: Worktree, *, registered: bool) -> None:
        current = self.store.read().envs.get(work.name)
        if current is not None and (
            (current.host == host.name and (not registered or current.worktree_path != work.root))
            or (registered and current.host != host.name)
        ):
            raise PreconditionError("Environment registration changed before cleanup", "Review offbox list and run cleanup again.")
        if current is not None and current.host == host.name:
            self.ports.cancel(work, host)
        if current is None or current.host == host.name:
            self.sync.terminate(work, host)
        self.mutate(host, work, "remove")
        with self.store.transaction() as state:
            current = state.envs.get(work.name)
            if current is not None and current.host == host.name and current.worktree_path == work.root:
                del state.envs[work.name]

    def remove(self, *, cwd: Path | None = None) -> None:
        work, env, host = self.current(cwd)
        with host_operation(self.paths, host.name):
            self._check_record(work, env, host)
            self._remove(host, work, registered=True)
        console.success(f"Removed {work.name}")

    def report(self) -> EnvReport:
        state = self.store.read()
        report = EnvReport()
        for env in sorted(state.envs.values(), key=lambda item: item.name):
            work = Worktree(root=env.worktree_path, name=env.name)
            host = state.hosts.get(env.host)
            summary = EnvSummary(name=env.name, host=env.host, worktree=env.worktree_path, status="missing", image_digest=env.image_digest)
            if host is None:
                summary.status = "host missing"
                summary.fix = "Restore the registered host before managing this environment."
            else:
                try:
                    container = self.inspect(host, work)
                    if container is not None:
                        self._owned(work, container)
                        summary.status = container.state.status
                        summary.running = container.state.running
                    else:
                        summary.fix = "Run offbox up in this worktree."
                except ConnectionError_ as exc:
                    summary.status = "unreachable"
                    summary.fix = exc.fix
            report.envs.append(summary)
        return report

    def shell(self, *, cwd: Path | None = None) -> int:
        work, _env, host = self.current(cwd)
        container = self.inspect(host, work)
        if container is None or not container.state.running:
            raise PreconditionError("Environment is not running", "Run offbox up before opening a shell.")
        self._owned(work, container)
        return self.runner.stream(
            ssh_argv(host.name, ["docker", "exec", "-it", "-u", "dev", "-w", "/work", work.container, "bash", "-l"], options=["-t"]),
            stdin=True,
            tty=True,
        )

    def _containers(self, host: HostState) -> list[ContainerInfo]:
        result = self.remote(host, ["docker", "ps", "-aq", "--filter", "label=offbox.env"])
        identifiers = result.stdout.split()
        if not identifiers:
            return []
        if any(re.fullmatch(r"[a-f0-9]{12,64}", value) is None for value in identifiers):
            raise PreconditionError("Invalid managed container identifiers", "Inspect the Docker daemon before running GC.")
        result = self.remote(host, ["docker", "container", "inspect", *identifiers])
        try:
            return TypeAdapter(list[ContainerInfo]).validate_json(result.stdout)
        except ValidationError as exc:
            raise PreconditionError("Invalid Docker inventory", "Check the host Docker daemon and offbox logs.") from exc

    def _volumes(self, host: HostState) -> list[VolumeInfo]:
        result = self.remote(host, ["docker", "volume", "ls", "-q", "--filter", "label=offbox.env"])
        names = result.stdout.split()
        if not names:
            return []
        if any(re.fullmatch(r"offbox-[a-z0-9][a-z0-9-]{0,36}-docker", value) is None for value in names):
            raise PreconditionError("Invalid managed volume names", "Inspect the Docker volumes before running GC.")
        result = self.remote(host, ["docker", "volume", "inspect", *names])
        try:
            return TypeAdapter(list[VolumeInfo]).validate_json(result.stdout)
        except ValidationError as exc:
            raise PreconditionError("Invalid Docker volume inventory", "Check the host Docker daemon and offbox logs.") from exc

    def _resource_worktree(self, labels: dict[str, str] | None, resource_name: str, *, volume: bool = False) -> Worktree:
        values = labels or {}
        name = validate_env_name(values.get("offbox.env", ""))
        root = Path(values.get("offbox.worktree", ""))
        if not root.is_absolute():
            raise PreconditionError("Managed resource has no absolute worktree label", "Inspect its ownership labels before running GC.")
        work = Worktree(root=root, name=name)
        if resource_name.lstrip("/") != (work.volume if volume else work.container):
            raise PreconditionError("Managed resource name does not match its labels", "Inspect its ownership labels before running GC.")
        return work

    def gc_plan(self) -> GcPlan:
        state = self.store.read()
        targets: dict[tuple[str, str], GcTarget] = {}
        running: list[GcTarget] = []
        for stored_env in state.envs.values():
            if stored_env.host not in state.hosts:
                raise PreconditionError(
                    f"Environment {stored_env.name} has an unregistered host", "Restore its host registration before running GC."
                )
            if not worktree_exists(stored_env.worktree_path):
                work = Worktree(root=stored_env.worktree_path, name=stored_env.name)
                targets[(stored_env.host, stored_env.name)] = GcTarget(
                    host=stored_env.host, worktree=work, reason="worktree no longer exists", registered=True
                )
        for host in sorted(state.hosts.values(), key=lambda item: item.name):
            if not host.address or host.phase != "ready":
                continue
            for container in self._containers(host):
                work = self._resource_worktree(container.config.labels, container.name)
                env = state.envs.get(work.name)
                registered = env is not None and env.host == host.name
                if registered and env is not None and env.worktree_path != work.root:
                    raise PreconditionError("Container labels conflict with registered worktree", "Inspect state and ownership before GC.")
                if not registered:
                    targets[(host.name, work.name)] = GcTarget(host=host.name, worktree=work, reason="orphan container", registered=False)
                elif container.state.running and (host.name, work.name) not in targets:
                    running.append(GcTarget(host=host.name, worktree=work, reason="prune unused Docker data", registered=True))
            for volume in self._volumes(host):
                work = self._resource_worktree(volume.labels, volume.name, volume=True)
                env = state.envs.get(work.name)
                registered = env is not None and env.host == host.name
                if registered and env is not None and env.worktree_path != work.root:
                    raise PreconditionError("Volume labels conflict with registered worktree", "Inspect state and ownership before GC.")
                key = (host.name, work.name)
                if key in targets and targets[key].worktree.root != work.root:
                    raise PreconditionError("Container and volume labels disagree", "Inspect resource ownership before GC.")
                if not registered and key not in targets:
                    targets[key] = GcTarget(host=host.name, worktree=work, reason="orphan volume", registered=False)
        return GcPlan(targets=list(targets.values()), running=running)

    def collect(self, plan: GcPlan) -> None:
        hosts = sorted({target.host for target in [*plan.targets, *plan.running]})
        for name in hosts:
            host = self._host(name)
            with host_operation(self.paths, name):
                for target in plan.targets:
                    if target.host == name:
                        if target.reason == "worktree no longer exists" and worktree_exists(target.worktree.root):
                            raise PreconditionError(
                                "A worktree selected for cleanup exists again", "Review a new GC plan before removing it."
                            )
                        self._remove(host, target.worktree, registered=target.registered)
                for target in plan.running:
                    if target.host == name:
                        env = self.store.read().envs.get(target.worktree.name)
                        if env is None or env.host != name or env.worktree_path != target.worktree.root:
                            raise PreconditionError("Environment changed before pruning", "Review the GC plan and retry.")
                        self.mutate(host, target.worktree, "prune")
        console.success(f"Garbage collection removed {len(plan.targets)} environments")
