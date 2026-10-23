import fcntl
import ipaddress
import logging
import re
import socket
from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, contextmanager
from ipaddress import IPv4Address
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from offbox.config import Model, PortsConfig
from offbox.errors import CommandFailed, ConnectionError_, OffboxError, PreconditionError
from offbox.host import host_operation
from offbox.naming import Worktree
from offbox.paths import Paths
from offbox.runner import Result, Runner
from offbox.ssh import master_pid, ssh_argv
from offbox.state import EnvState, HostState, State, StateStore
from offbox.timings import measured

logger = logging.getLogger(__name__)


class DockerModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, hide_input_in_errors=True)


class Publisher(DockerModel):
    published_port: int = Field(alias="PublishedPort", ge=0, le=65535)
    protocol: str = Field(alias="Protocol")


class ComposeService(DockerModel):
    publishers: list[Publisher] | None = Field(default=None, alias="Publishers")


class PortForward(Model):
    env: str
    host: str
    remote: int = Field(ge=1, le=65535)
    local: int = Field(ge=1, le=65535)
    ip: IPv4Address | None
    active: bool


class PortsReport(Model):
    forwards: list[PortForward] = Field(default_factory=list[PortForward])


class ContainerNetwork(DockerModel):
    ip: str = Field(alias="IPAddress")


class ContainerNetworks(DockerModel):
    networks: dict[str, ContainerNetwork] = Field(alias="Networks")


class ContainerState(DockerModel):
    running: bool = Field(alias="Running")


class ContainerConfig(DockerModel):
    labels: dict[str, str] | None = Field(default=None, alias="Labels")


class PortContainer(DockerModel):
    name: str = Field(alias="Name")
    state: ContainerState = Field(alias="State")
    config: ContainerConfig = Field(alias="Config")
    network: ContainerNetworks = Field(alias="NetworkSettings")


class PortSnapshot(Model):
    containers: list[PortContainer]
    compose: str


PORT_DISCOVERY_SCRIPT = r"""set -eu
name=$1
worktree=$2
container=$3
inspection=$(docker container inspect "$container")
compose=
if printf '%s\n' "$inspection" | jq -e --arg name "$name" --arg worktree "$worktree" --arg container "$container" '
    length == 1 and .[0].Name == ("/" + $container)
    and .[0].Config.Labels["offbox.env"] == $name
    and .[0].Config.Labels["offbox.worktree"] == $worktree
    and .[0].State.Running == true
' >/dev/null; then
    compose=$(docker exec -u dev -w /work "$container" docker compose ps --format json)
fi
compose_json=$(printf '%s' "$compose" | jq -Rs .)
printf '%s\n%s\n' "$inspection" "$compose_json" | jq -cs '{containers: .[0], compose: .[1]}'
"""


class ForwardSpec(Model):
    env: str
    host: str
    remote: int = Field(ge=1, le=65535)
    local: int = Field(ge=1, le=65535)
    ip: IPv4Address

    @property
    def value(self) -> str:
        return f"127.0.0.1:{self.local}:{self.ip}:{self.remote}"


def parse_compose(text: str) -> list[int]:
    if not text.strip():
        return []
    try:
        if text.lstrip().startswith("["):
            services = TypeAdapter(list[ComposeService]).validate_json(text)
        else:
            services = [ComposeService.model_validate_json(line) for line in text.splitlines() if line.strip()]
    except ValidationError as exc:
        raise PreconditionError("Invalid Compose port response", "Check docker compose ps --format json inside the environment.") from exc
    return sorted(
        {item.published_port for service in services for item in service.publishers or () if item.protocol == "tcp" and item.published_port}
    )


def loopback_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def allocate_port(preferred: int, reserved: set[int], probe: Callable[[int], bool]) -> int:
    if not 1 <= preferred <= 65535:
        raise PreconditionError("Invalid preferred local port", "Choose a port between 1 and 65535.")
    for candidate in range(preferred, 65536):
        if candidate not in reserved and probe(candidate):
            return candidate
    raise PreconditionError("No free local port is available", "Stop a competing local listener or remove unused offbox port mappings.")


def forwarding_command(action: Literal["forward", "cancel"], spec: ForwardSpec) -> list[str]:
    return ["ssh", "-O", action, "-L", spec.value, f"offbox-{spec.host}"]


class PortManager:
    """Restore/cancel callers hold host_operation; swap acquires its own host locks."""

    def __init__(self, paths: Paths, runner: Runner, probe: Callable[[int], bool] = loopback_available) -> None:
        self.paths = paths
        self.runner = runner
        self.probe = probe
        self.store = StateStore(paths.state)

    @contextmanager
    def _operation(self) -> Generator[None]:
        try:
            self.paths.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle = (self.paths.state / "ports.lock").open("a", encoding="utf-8")
        except OSError as exc:
            raise PreconditionError("Cannot open port operation lock", "Check the offbox state directory permissions.") from exc
        with handle as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PreconditionError("Another port operation is running", "Wait for that operation and retry offbox ports.") from exc
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _host(self, state: State, host: HostState) -> HostState:
        current = state.hosts.get(host.name)
        if current is None or current.model_copy(update={"control_master_pid": None}) != host.model_copy(
            update={"control_master_pid": None}
        ):
            raise PreconditionError("Host changed during port setup", "Run offbox ports again after the host operation finishes.")
        if current.phase != "ready":
            raise PreconditionError("Host is not ready for port forwarding", "Finish host initialization before restoring ports.")
        return current

    def _env(self, state: State, work: Worktree, host: HostState) -> EnvState:
        self._host(state, host)
        env = state.envs.get(work.name)
        if env is None or env.host != host.name or env.worktree_path != work.root:
            raise PreconditionError("Environment changed during port setup", "Run offbox up in the correct worktree.")
        if any(not 1 <= remote <= 65535 or not 1 <= local <= 65535 for remote, local in env.local_ports.items()):
            raise PreconditionError("Stored port mapping is invalid", "Repair the environment's port state before continuing.")
        return env

    def _mux(self, argv: list[str]) -> Result:
        return self.runner.run(argv, check=False, env={"LC_ALL": "C"}, timeout=10)

    def _checked(self, result: Result, fix: str) -> None:
        if result.returncode:
            error = CommandFailed(result.argv, result.returncode, result.stderr)
            error.fix = fix
            raise error

    def _master(self, host: HostState, *, start: bool = True) -> int | None:
        pid = master_pid(self.runner, host.name)
        created = pid is None
        if created and start:
            started = self._mux(["ssh", "-fN", f"offbox-{host.name}"])
            if started.returncode:
                raise ConnectionError_(
                    f"Cannot start SSH master for {host.name}", f"Check offbox host ssh {host.name} and retry offbox ports."
                )
            pid = master_pid(self.runner, host.name)
        if pid is None and start:
            raise ConnectionError_(f"SSH master for {host.name} is unavailable", "Check SSH connectivity and retry offbox ports.")
        with self.store.transaction() as state:
            stored = self._host(state, host)
            if created or stored.control_master_pid != pid:
                for env in state.envs.values():
                    if env.host == host.name:
                        env.active_forwards = []
                stored.control_master_pid = pid
        return pid

    def _remote(self, host: HostState, argv: Sequence[str], *, input: str | None = None, timeout: float = 15) -> Result:
        result = self.runner.run(ssh_argv(host.name, argv), check=False, timeout=timeout, input=input)
        if result.returncode == 255:
            raise ConnectionError_(f"Cannot reach host {host.name}", f"Check offbox host ssh {host.name} and retry.")
        self._checked(result, "Run offbox up and check the environment's Docker/Compose configuration.")
        return result

    def _ip(self, work: Worktree, host: HostState) -> IPv4Address:
        result = self._remote(host, ["docker", "container", "inspect", work.container])
        try:
            containers = TypeAdapter(list[PortContainer]).validate_json(result.stdout)
        except ValidationError as exc:
            raise PreconditionError(
                "Invalid managed container network", "Inspect the offbox network and container ownership before retrying."
            ) from exc
        return self._container_ip(work, containers)

    def _container_ip(self, work: Worktree, containers: Sequence[PortContainer]) -> IPv4Address:
        try:
            if len(containers) != 1:
                raise ValueError("Expected one managed container")
            container = containers[0]
            labels = container.config.labels or {}
            if (
                container.name.lstrip("/") != work.container
                or labels.get("offbox.env") != work.name
                or labels.get("offbox.worktree") != str(work.root)
            ):
                raise ValueError("Conflicting container ownership")
            if not container.state.running:
                raise PreconditionError("Environment is not running", "Run offbox up before restoring ports.")
            return ipaddress.IPv4Address(container.network.networks["offbox"].ip)
        except (ValueError, KeyError) as exc:
            raise PreconditionError(
                "Invalid managed container network", "Inspect the offbox network and container ownership before retrying."
            ) from exc

    def _discovery(self, work: Worktree, host: HostState) -> tuple[IPv4Address, list[int]]:
        result = self._remote(host, ["sh", "-s", "--", work.name, str(work.root), work.container], input=PORT_DISCOVERY_SCRIPT, timeout=30)
        try:
            snapshot = PortSnapshot.model_validate_json(result.stdout)
        except ValidationError as exc:
            raise PreconditionError(
                "Invalid managed container network", "Inspect the offbox network and container ownership before retrying."
            ) from exc
        return self._container_ip(work, snapshot.containers), parse_compose(snapshot.compose)

    def _spec(self, env: EnvState, remote: int) -> ForwardSpec:
        if env.forward_ip is None:
            raise PreconditionError(
                "Forward destination metadata is missing", "Close the offbox SSH master and run offbox ports to restore its forwards."
            )
        return ForwardSpec(env=env.name, host=env.host, remote=remote, local=env.local_ports[remote], ip=env.forward_ip)

    def _cancel_spec(self, spec: ForwardSpec) -> None:
        result = self._mux(forwarding_command("cancel", spec))
        diagnostics = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        absent = "mux_client_forward: forwarding request failed: port not forwarded"
        summary = "muxclient: master cancel forward request failed"
        if absent in diagnostics and all(line in (absent, summary) for line in diagnostics):
            return
        fix = "Retry offbox ports; the previous listener could not be cancelled safely."
        self._checked(result, fix)
        if diagnostics:
            raise PreconditionError("Cannot confirm SSH forward cancellation: " + result.stderr.strip(), fix)

    def _clear(self, work: Worktree, host: HostState, remotes: Sequence[int], *, remove: bool = False) -> None:
        for remote in remotes:
            env = self._env(self.store.read(), work, host)
            if env.forward_ip is not None:
                self._cancel_spec(self._spec(env, remote))
            elif remote in env.active_forwards:
                raise PreconditionError(
                    "Forward destination metadata is missing", "Close the offbox SSH master and run offbox ports again."
                )
            with self.store.transaction() as state:
                current = self._env(state, work, host)
                current.active_forwards = [value for value in current.active_forwards if value != remote]
                if remove:
                    del current.local_ports[remote]
        with self.store.transaction() as state:
            current = self._env(state, work, host)
            if not current.local_ports or all(remote in remotes for remote in current.local_ports):
                current.forward_ip = None

    def _reserved(self, state: State, env_name: str, remote: int) -> set[int]:
        return {local for env in state.envs.values() for key, local in env.local_ports.items() if env.name != env_name or key != remote}

    def _reserve(self, work: Worktree, host: HostState, remote: int, ip: IPv4Address, *, replace: bool = False) -> ForwardSpec:
        with self.store.transaction() as state:
            env = self._env(state, work, host)
            reserved = self._reserved(state, env.name, remote)
            current = env.local_ports.get(remote)
            if current is None or replace:
                preferred = max(remote, current + 1) if current is not None else remote
                current = allocate_port(preferred, reserved, self.probe)
                env.local_ports[remote] = current
            elif current in reserved:
                raise PreconditionError("Stored local port has multiple owners", "Repair duplicate port mappings before retrying.")
            env.forward_ip = ip
            return self._spec(env, remote)

    def _mark(self, work: Worktree, host: HostState, spec: ForwardSpec, pid: int) -> None:
        with self.store.transaction() as state:
            env = self._env(state, work, host)
            if (
                self._host(state, host).control_master_pid != pid
                or env.local_ports.get(spec.remote) != spec.local
                or env.forward_ip != spec.ip
            ):
                raise PreconditionError("Port mapping changed while forwarding", "Run offbox ports again to reconcile the listener.")
            env.active_forwards = sorted({*env.active_forwards, spec.remote})

    def _forward(self, work: Worktree, host: HostState, remote: int, ip: IPv4Address, pid: int) -> None:
        spec = self._reserve(work, host, remote, ip)
        for attempt in range(3):
            result = self._mux(forwarding_command("forward", spec))
            if result.returncode == 0:
                self._mark(work, host, spec, pid)
                return
            bind_failure = any(
                re.fullmatch(r"mux_client_forward: forwarding request failed: Port forwarding failed", line.strip())
                or "Address already in use" in line
                for line in result.stderr.splitlines()
            )
            if not bind_failure or self.probe(spec.local) or attempt == 2:
                self._checked(result, f"Local port {spec.local} could not be forwarded; stop its competing listener or retry offbox ports.")
            with self.store.transaction() as state:
                env = self._env(state, work, host)
                env.active_forwards = [value for value in env.active_forwards if value != remote]
            spec = self._reserve(work, host, remote, ip, replace=True)

    def report(self, env: EnvState) -> PortsReport:
        return PortsReport(
            forwards=[
                PortForward(
                    env=env.name, host=env.host, remote=remote, local=local, ip=env.forward_ip, active=remote in env.active_forwards
                )
                for remote, local in sorted(env.local_ports.items())
            ]
        )

    def _restore(self, work: Worktree, host: HostState, config: PortsConfig) -> PortsReport:
        self._env(self.store.read(), work, host)
        ip = None
        ports = set(config.forward)
        if config.auto and any(
            (work.root / name).is_file() for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
        ):
            ip, discovered = self._discovery(work, host)
            ports.update(discovered)
        desired = sorted(ports)
        current = self._env(self.store.read(), work, host)
        if not desired and not current.local_ports:
            return self.report(current)
        if ip is None:
            ip = self._ip(work, host)
        pid = self._master(host)
        if pid is None:
            raise ConnectionError_("SSH master is unavailable", "Run offbox ports again after checking SSH connectivity.")
        env = self._env(self.store.read(), work, host)
        obsolete = [remote for remote in env.local_ports if remote not in desired]
        if obsolete:
            self._clear(work, host, obsolete, remove=True)
        env = self._env(self.store.read(), work, host)
        if env.forward_ip is not None and env.forward_ip != ip:
            self._clear(work, host, list(env.local_ports))
        for remote in desired:
            current = self._env(self.store.read(), work, host)
            if remote not in current.active_forwards:
                self._forward(work, host, remote, ip, pid)
        return self.report(self._env(self.store.read(), work, host))

    @measured("Port forwarding")
    def restore(self, work: Worktree, host: HostState, config: PortsConfig) -> PortsReport:
        with self._operation():
            return self._restore(work, host, config)

    def cancel(self, work: Worktree, host: HostState) -> None:
        with self._operation():
            env = self._env(self.store.read(), work, host)
            if not env.local_ports:
                return
            if self._master(host, start=False) is None:
                with self.store.transaction() as state:
                    self._env(state, work, host).forward_ip = None
                return
            self._clear(work, host, list(env.local_ports))

    def _set_pair_active(self, specs: Sequence[ForwardSpec], active: Sequence[ForwardSpec]) -> None:
        with self.store.transaction() as state:
            for spec in specs:
                env = state.envs.get(spec.env)
                if env is None or env.host != spec.host or env.local_ports.get(spec.remote) != spec.local or env.forward_ip != spec.ip:
                    raise PreconditionError("Port swap state changed", "Run offbox ports in both worktrees before retrying.")
                env.active_forwards = [remote for remote in env.active_forwards if remote != spec.remote]
                if spec in active:
                    env.active_forwards = sorted({*env.active_forwards, spec.remote})

    def _rollback(self, old: list[ForwardSpec], installed: list[ForwardSpec]) -> list[str]:
        failures: list[str] = []
        for spec in installed:
            try:
                self._cancel_spec(spec)
            except OffboxError as exc:
                failures.append(exc.message)
        restored: list[ForwardSpec] = []
        for spec in old:
            try:
                result = self._mux(forwarding_command("forward", spec))
                self._checked(result, "Run offbox ports in the affected worktree.")
                restored.append(spec)
            except OffboxError as exc:
                failures.append(exc.message)
        try:
            self._set_pair_active(old, restored)
        except OffboxError as exc:
            failures.append(exc.message)
        return failures

    def swap(self, work: Worktree, remote: int) -> PortsReport:
        with self._operation():
            snapshot = self.store.read()
            selected = snapshot.envs.get(work.name)
            if selected is None or selected.worktree_path != work.root or remote not in selected.local_ports:
                raise PreconditionError(
                    "Remote port is not mapped for this worktree", "Run offbox ports after configuring or publishing that port."
                )
            owners = [(env, key) for env in snapshot.envs.values() for key, local in env.local_ports.items() if local == remote]
            if len(owners) > 1:
                raise PreconditionError("Preferred local port has multiple owners", "Repair duplicate local port mappings before swapping.")
            owner = owners[0] if owners else None
            hosts = {selected.host, *([owner[0].host] if owner else [])}
            with ExitStack() as locks:
                for name in sorted(hosts):
                    locks.enter_context(host_operation(self.paths, name))
                targets = {selected.name: selected}
                if owner:
                    targets[owner[0].name] = owner[0]
                for env in targets.values():
                    host = snapshot.hosts.get(env.host)
                    if host is None:
                        raise PreconditionError("Port owner has no registered host", "Restore its host registration before swapping ports.")
                    self._restore(
                        Worktree(root=env.worktree_path, name=env.name), host, PortsConfig(forward=list(env.local_ports), auto=False)
                    )
                state = self.store.read()
                current = state.envs[work.name]
                if current.local_ports[remote] == remote:
                    return self.report(current)
                if current.local_ports != selected.local_ports or (owner and state.envs[owner[0].name].local_ports != owner[0].local_ports):
                    raise PreconditionError("Port mappings changed before the swap", "Run offbox ports and retry the swap.")
                if owner is None and not self.probe(remote):
                    raise PreconditionError(
                        f"Local port {remote} is used outside offbox", "Stop that local listener before retrying the swap."
                    )
                old = [self._spec(current, remote)]
                new = [old[0].model_copy(update={"local": remote})]
                if owner:
                    other = self._spec(state.envs[owner[0].name], owner[1])
                    old.append(other)
                    new.append(other.model_copy(update={"local": old[0].local}))
                self._set_pair_active(old, [])
                installed: list[ForwardSpec] = []
                try:
                    for spec in old:
                        self._cancel_spec(spec)
                    for spec in new:
                        result = self._mux(forwarding_command("forward", spec))
                        self._checked(result, "Run offbox ports in both worktrees and retry the swap.")
                        installed.append(spec)
                    with self.store.transaction() as state:
                        for before, after in zip(old, new, strict=True):
                            committed = state.envs.get(before.env)
                            if (
                                committed is None
                                or committed.host != before.host
                                or committed.local_ports.get(before.remote) != before.local
                                or committed.forward_ip != before.ip
                            ):
                                raise PreconditionError(
                                    "Port owner changed during swap", "Run offbox ports in both worktrees to recover the listeners."
                                )
                            committed.local_ports[after.remote] = after.local
                            committed.active_forwards = sorted({*committed.active_forwards, after.remote})
                except OffboxError as exc:
                    failures = self._rollback(old, installed)
                    detail = "Original mappings restored." if not failures else "Recovery incomplete: " + "; ".join(failures)
                    retry = "run offbox ports in the affected worktrees before retrying --swap."
                    if failures:
                        commands = "; ".join(f"ssh -O exit offbox-{name}" for name in sorted(hosts))
                        fix = f"Run {commands}, then {retry}"
                    else:
                        fix = "Run offbox ports in the affected worktrees before retrying --swap."
                    raise OffboxError("Port swap failed. " + detail, fix) from exc
                return self.report(self.store.read().envs[work.name])
