import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import field_validator

from offbox import console
from offbox.config import HostPreset, Model, UserConfig, load_config
from offbox.drivers.base import HostVariables
from offbox.drivers.tofu import TofuDriver
from offbox.errors import CommandFailed, OffboxError, PreconditionError, ProviderInterrupted
from offbox.host import host_operation, public_key
from offbox.host.cloudinit import generate_host_key, render_cloud_init
from offbox.host.ready import Clock, SystemClock, tcp_probe, wait_ready
from offbox.paths import Paths
from offbox.providers import copy_provider, credentials_env, discover_provider, validate_name
from offbox.runner import Runner
from offbox.ssh import close_master, default_ssh_key, ensure_include, host_key_path, known_host, ssh_argv, sync_config, write_private
from offbox.state import HostState, StateStore
from offbox.timings import stage
from offbox.tools import ToolDiscovery

logger = logging.getLogger(__name__)
_NAME_TAKEN = ("uniqueness_error", "alreadyExists", "already exists")


def _name_taken(exc: CommandFailed) -> bool:
    return any(marker in exc.stderr_tail for marker in _NAME_TAKEN)


class CreateOptions(Model):
    name: str = "main"
    provider: str | None = None
    size: str | None = None
    region: str | None = None
    ssh_key: Path | None = None
    auto: bool = False
    idle_seconds: int | None = None

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return validate_name(value)


def _remove_unrecorded(directory: Path) -> None:
    try:
        shutil.rmtree(directory)
    except OSError:
        logger.exception("Cannot remove unrecorded host directory %s", directory)
        console.warn(f"Remove {directory} before retrying host creation.")


def create_host_locked(
    paths: Paths,
    runner: Runner,
    options: CreateOptions,
    *,
    executable: Path | None = None,
    clock: Clock | None = None,
    probe: Callable[[str, int, float], bool] = tcp_probe,
    ssh_config: Path | None = None,
) -> HostState:
    store = StateStore(paths.state)
    if options.name in store.read().hosts:
        raise PreconditionError(f"Host {options.name} already exists", f"Run offbox host destroy {options.name} or choose another name.")
    timer = clock or SystemClock()
    started = timer.now()
    user = load_config(paths.user_config, UserConfig)
    preset = user.hosts.get(options.name, HostPreset())
    provider = discover_provider(options.provider or preset.provider, paths)
    if provider.manifest.driver != "tofu":
        raise PreconditionError("This provider registers existing hosts", "Run offbox host init USER@ADDRESS.")
    credentials = credentials_env(provider.manifest, paths)
    key = (options.ssh_key or preset.ssh_key or default_ssh_key()).expanduser().resolve()
    authorized_key = public_key(runner, key)
    binary = executable or ToolDiscovery(runner, paths.cache).check("tofu").path
    defaults = provider.manifest.defaults
    size = options.size or preset.size or defaults.size
    region = options.region or preset.region or defaults.region
    image = preset.image or defaults.image
    if not size or not region or not image:
        raise PreconditionError("Provider size, region and image are required", "Set provider defaults or a matching host preset.")
    directory = paths.state / "hosts" / options.name
    workspace = directory / "tofu"
    created = recorded = False
    try:
        with store.transaction() as state:
            if options.name in state.hosts:
                raise PreconditionError(
                    f"Host {options.name} already exists", f"Run offbox host destroy {options.name} or choose another name."
                )
            if directory.exists():
                raise PreconditionError(
                    f"Host directory already exists: {directory}",
                    "Inspect the existing directory before removing it or choose another host name.",
                )
            directory.mkdir(parents=True, mode=0o700)
            created = True
            digest = copy_provider(provider, workspace)
            host = HostState(
                name=options.name,
                provider=provider.manifest.name,
                address="",
                ssh_user="offbox",
                created_at=datetime.now(UTC),
                provider_dir=workspace,
                provider_files_hash=digest,
                known_hosts_path=directory / "known_hosts",
                ssh_key=key,
                phase="creating",
                auto=options.auto,
                idle_seconds=options.idle_seconds,
            )
            state.hosts[host.name] = host
        recorded = True
    finally:
        if created and not recorded:
            _remove_unrecorded(directory)
    cleanup = f"Run offbox host destroy {host.name} to clean up; its provider workspace was retained."
    try:
        private, public = generate_host_key(runner, host_key_path(host))
        write_private(host.known_hosts_path, known_host(host.name, public))
        variables = HostVariables(
            name=f"offbox-{host.name}",
            size=size,
            region=region,
            image=image,
            ssh_public_key=authorized_key,
            user_data=render_cloud_init(authorized_key, private, public),
            labels={"offbox": "1", "offbox-host": host.name},
        )
        try:
            output = TofuDriver(runner, workspace, paths.cache, credentials, binary).create(variables)
        except CommandFailed as exc:
            if _name_taken(exc):
                raise PreconditionError(
                    f"Cloud resources named offbox-{host.name} already exist in the provider project",
                    "Delete them in the provider console or choose another host name, "
                    f"then run offbox host destroy {host.name} to clear the local record.",
                ) from exc
            raise
        host.address = output.ip
        host.ssh_user = output.ssh_user
        host.phase = "initializing"
        with store.transaction() as state:
            state.hosts[host.name] = host
            sync_config(paths, state)
            ensure_include(paths, ssh_config)
        wait_ready(host.name, host.address, host.ssh_port, runner, paths.log_file(), clock=timer, probe=probe)
        # Readiness polling can authenticate before bootstrap adds the Docker group.
        # A multiplexed connection retains that login's groups until it is replaced.
        with stage("SSH refresh"):
            close_master(runner, host.name)
            runner.run(ssh_argv(host.name, ["docker", "info", "--format", "{{.ServerVersion}}"]), timeout=10)
        host.phase = "ready"
        with store.transaction() as state:
            state.hosts[host.name] = host
            if state.default_host is None and not host.auto:
                state.default_host = host.name
    except ProviderInterrupted as exc:
        if exc.killed:
            host.create_interrupted = True
            with store.transaction() as state:
                if state.hosts.get(host.name) is not None:
                    state.hosts[host.name] = host
        fix = (
            f"Run offbox host destroy {host.name}, then check the provider console for resources labelled "
            f"offbox-host={host.name} and delete any that remain."
            if exc.killed
            else cleanup
        )
        raise OffboxError(f"Creating host {host.name} failed: {exc.message}", fix, exc.exit_code) from exc
    except OffboxError as exc:
        raise OffboxError(f"Creating host {host.name} failed: {exc.message}", cleanup, exc.exit_code) from exc
    except KeyboardInterrupt as exc:
        raise OffboxError(f"Creating host {host.name} was interrupted", cleanup, 130) from exc
    finally:
        host_key_path(host).unlink(missing_ok=True)
        host_key_path(host).with_name(host_key_path(host).name + ".pub").unlink(missing_ok=True)
    console.success(f"Host {host.name}: {host.address} ({timer.now() - started:.1f}s)")
    return host


def create_host(
    paths: Paths,
    runner: Runner,
    options: CreateOptions,
    *,
    executable: Path | None = None,
    clock: Clock | None = None,
    probe: Callable[[str, int, float], bool] = tcp_probe,
    ssh_config: Path | None = None,
) -> HostState:
    with host_operation(paths, options.name):
        return create_host_locked(paths, runner, options, executable=executable, clock=clock, probe=probe, ssh_config=ssh_config)
