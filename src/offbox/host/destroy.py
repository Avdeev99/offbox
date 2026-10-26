import logging
import re
import shutil
from collections.abc import Callable
from functools import partial
from pathlib import Path

from offbox import console
from offbox.drivers.none import NoneDriver
from offbox.drivers.tofu import TofuDriver
from offbox.errors import OffboxError, PreconditionError, ProviderInterrupted, UsageError
from offbox.host import host_operation, select_host
from offbox.host.activity import forget as forget_activity
from offbox.naming import Worktree
from offbox.paths import Paths
from offbox.providers import credentials_env, load_provider, validate_name
from offbox.runner import Runner
from offbox.ssh import close_master, sync_config
from offbox.state import EnvState, HostState, StateStore
from offbox.sync import SyncManager
from offbox.tools import ToolDiscovery

logger = logging.getLogger(__name__)


def _best_effort(action: Callable[[], None], warning: str) -> None:
    try:
        action()
    except OffboxError as exc:
        logger.warning("%s", warning, exc_info=True)
        console.warn(f"{warning} ({exc.message})")


def _release_local(runner: Runner, cache: Path, host: HostState, envs: dict[str, EnvState], *, strict: bool) -> None:
    sync = SyncManager(runner, cache=cache)
    for env in envs.values():
        work = Worktree(root=env.worktree_path, name=env.name)
        if strict:
            sync.terminate(work, host)
        else:
            _best_effort(
                partial(sync.terminate, work, host),
                f"Cannot stop source sync for {env.name}; afterwards run mutagen sync list and terminate offbox-{env.name} if it remains",
            )
    if not host.address:
        return
    if strict:
        close_master(runner, host.name)
    else:
        _best_effort(
            partial(close_master, runner, host.name),
            f"Cannot close the SSH master for offbox-{host.name}; stop that local ssh process manually",
        )


def _destroy_cloud(runner: Runner, host: HostState, workspace: Path, cache: Path, credentials: dict[str, str], binary: Path) -> None:
    try:
        TofuDriver(runner, workspace, cache, credentials, binary).destroy()
    except ProviderInterrupted as exc:
        fix = f"Run offbox host destroy {host.name} again."
        if exc.killed:
            fix = (
                f"Run offbox host destroy {host.name} again, then check the provider console for resources labelled "
                f"offbox-host={host.name} and delete any that remain."
            )
        raise ProviderInterrupted(f"Destroying host {host.name} was interrupted", fix, killed=exc.killed) from exc


def destroy_host_locked(
    paths: Paths, runner: Runner, name: str | None = None, *, executable: Path | None = None, forget: bool = False
) -> None:
    host = select_host(paths, name)
    validate_name(host.name)
    directory = paths.state / "hosts" / host.name
    expected = directory / ("provider" if host.provider == "ssh" else "tofu")
    if host.provider_dir is None or host.provider_dir.resolve() != expected.resolve() or directory.is_symlink() or expected.is_symlink():
        raise PreconditionError(
            "Host provider workspace is missing or unsafe", "Restore the host's saved provider workspace before destroying it."
        )
    provider = load_provider(host.provider_dir)
    existing = provider.manifest.driver == "none"
    if forget and not existing:
        raise UsageError(
            f"Host {host.name} is a cloud host; --forget would leave it running and billing",
            f"Run offbox host destroy {host.name} --yes to delete it.",
        )
    credentials = credentials_env(provider.manifest, paths)
    binary = None if existing else executable or ToolDiscovery(runner, paths.cache).check("tofu").path
    store = StateStore(paths.state)
    snapshot = store.read()
    if snapshot.hosts.get(host.name) != host:
        raise PreconditionError("Host changed while preparing destruction", "Run host destroy again after other operations finish.")
    envs = {name: env for name, env in snapshot.envs.items() if env.host == host.name}
    for env in envs.values():
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,36}", env.name) is None:
            raise PreconditionError("Invalid stored environment name", "Repair the environment state before destroying its host.")
    _release_local(runner, paths.cache, host, envs, strict=existing and not forget)
    if existing and not forget:
        NoneDriver(runner, host).destroy()
        _best_effort(
            partial(close_master, runner, host.name),
            f"Cannot close the SSH master for offbox-{host.name}; stop that local ssh process manually",
        )
    elif not existing:
        if binary is None:
            raise PreconditionError("Provider tool is unavailable", "Install OpenTofu or Terraform and retry.")
        _destroy_cloud(runner, host, host.provider_dir, paths.cache, credentials, binary)
    with store.transaction() as state:
        if state.hosts.get(host.name) != host or {name: env for name, env in state.envs.items() if env.host == host.name} != envs:
            logger.warning("host %s changed during destruction", host.name)
            console.warn(
                f"Host {host.name} changed while it was being destroyed; removing its record and environments anyway. "
                "Review offbox list and mutagen sync list for environments added meanwhile."
            )
        state.hosts.pop(host.name, None)
        state.envs = {name: env for name, env in state.envs.items() if env.host != host.name}
        if state.default_host == host.name:
            state.default_host = next((name for name, item in state.hosts.items() if item.phase == "ready"), None)
        sync_config(paths, state)
    for env_name in envs:
        forget_activity(paths, env_name)
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        raise PreconditionError(
            f"Host destroyed, but cannot remove {directory}", "Remove the obsolete local host directory manually."
        ) from exc
    if forget:
        console.warn(
            f"Forgot host {host.name} without contacting {host.address or 'the server'}. Still on the server: "
            "offbox-labelled Docker containers and volumes, the offbox Docker network, /srv/offbox, /var/lib/offbox, "
            "the offbox user, /home/offbox/.ssh/authorized_keys and its passwordless sudo rule /etc/sudoers.d/offbox."
        )
        console.success(f"Forgot host {host.name}")
        return
    console.success(f"Destroyed host {host.name}")


def destroy_host(paths: Paths, runner: Runner, name: str | None = None, *, executable: Path | None = None, forget: bool = False) -> None:
    host = select_host(paths, name)
    with host_operation(paths, host.name):
        destroy_host_locked(paths, runner, host.name, executable=executable, forget=forget)
