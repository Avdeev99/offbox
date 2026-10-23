import re
import shutil
from pathlib import Path

from offbox import console
from offbox.drivers.none import NoneDriver
from offbox.drivers.tofu import TofuDriver
from offbox.errors import PreconditionError
from offbox.host import host_operation, select_host
from offbox.naming import Worktree
from offbox.paths import Paths
from offbox.providers import credentials_env, load_provider, validate_name
from offbox.runner import Runner
from offbox.ssh import ssh_argv, sync_config
from offbox.state import StateStore
from offbox.sync import SyncManager
from offbox.tools import ToolDiscovery


def _destroy_host(paths: Paths, runner: Runner, name: str | None = None, *, executable: Path | None = None) -> None:
    host = select_host(paths, name)
    validate_name(host.name)
    directory = paths.state / "hosts" / host.name
    expected = directory / ("provider" if host.provider == "ssh" else "tofu")
    if host.provider_dir is None or host.provider_dir.resolve() != expected.resolve() or directory.is_symlink() or expected.is_symlink():
        raise PreconditionError(
            "Host provider workspace is missing or unsafe", "Restore the host's saved provider workspace before destroying it."
        )
    provider = load_provider(host.provider_dir)
    credentials = credentials_env(provider.manifest, paths)
    binary = None if provider.manifest.driver == "none" else executable or ToolDiscovery(runner, paths.cache).check("tofu").path
    store = StateStore(paths.state)
    with store.transaction() as state:
        if state.hosts.get(host.name) != host:
            raise PreconditionError("Host changed while preparing destruction", "Run host destroy again after other operations finish.")
        envs = [env for env in state.envs.values() if env.host == host.name]
        for env in envs:
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,36}", env.name) is None:
                raise PreconditionError("Invalid stored environment name", "Repair the environment state before destroying its host.")
            SyncManager(runner).terminate(Worktree(root=env.worktree_path, name=env.name), host)
        if host.address:
            master = runner.run(["ssh", "-O", "check", f"offbox-{host.name}"], check=False, timeout=5)
            if master.returncode == 0:
                runner.run(["ssh", "-O", "exit", f"offbox-{host.name}"], timeout=5)
        if provider.manifest.driver == "none":
            NoneDriver(runner, host).destroy()
            runner.run(ssh_argv(host.name, options=["-O", "exit"]), check=False, timeout=5)
        else:
            if binary is None:
                raise PreconditionError("Provider tool is unavailable", "Install OpenTofu or Terraform and retry.")
            TofuDriver(runner, host.provider_dir, paths.cache, credentials, binary).destroy()
        del state.hosts[host.name]
        state.envs = {name: env for name, env in state.envs.items() if env.host != host.name}
        if state.default_host == host.name:
            state.default_host = next((name for name, item in state.hosts.items() if item.phase == "ready"), None)
        sync_config(paths, state)
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        raise PreconditionError(
            f"Host destroyed, but cannot remove {directory}", "Remove the obsolete local host directory manually."
        ) from exc
    console.success(f"Destroyed host {host.name}")


def destroy_host(paths: Paths, runner: Runner, name: str | None = None, *, executable: Path | None = None) -> None:
    host = select_host(paths, name)
    with host_operation(paths, host.name):
        _destroy_host(paths, runner, host.name, executable=executable)
