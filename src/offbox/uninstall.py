import logging
import os
import shutil
import stat
from contextlib import ExitStack
from pathlib import Path

from offbox import console
from offbox.errors import OffboxError, PreconditionError
from offbox.host import host_operation
from offbox.logging import close_logs
from offbox.paths import Paths
from offbox.providers import load_provider
from offbox.runner import Runner
from offbox.ssh import close_master, remove_include
from offbox.state import HostState, StateStore
from offbox.sync import SyncManager
from offbox.tools import cached_mutagen

logger = logging.getLogger(__name__)


def _validate_owned_directory(path: Path, config: Path) -> None:
    if path.resolve().is_relative_to(config.resolve()) or config.resolve().is_relative_to(path.resolve()):
        raise PreconditionError(
            f"Cleanup directory overlaps user configuration: {path}", "Separate offbox config, state, cache and control directories."
        )
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PreconditionError(f"Cannot inspect cleanup directory {path}", "Check local directory permissions.") from exc
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise PreconditionError(f"Unsafe cleanup directory {path}", "Restore an owned directory without symlinks before uninstalling.")


def _is_cloud(host: HostState) -> bool:
    if host.provider_dir is not None:
        try:
            return load_provider(host.provider_dir).manifest.driver != "none"
        except OffboxError:
            logger.warning("cannot read provider workspace for host %s", host.name, exc_info=True)
    return host.provider != "ssh"


def _describe(host: HostState) -> str:
    return f"{host.name} ({host.provider}, {host.address or 'no address yet'}, label offbox-host={host.name})"


def _stop_cached_mutagen_daemon(paths: Paths, runner: Runner) -> None:
    executable = cached_mutagen(paths.cache)
    if executable is None or shutil.which("mutagen") is not None:
        return
    result = runner.run([str(executable), "daemon", "stop"], check=False, timeout=30)
    if result.returncode != 0:
        logger.warning("cannot stop the downloaded Mutagen daemon: %s", result.stderr.strip())


def uninstall(
    paths: Paths, runner: Runner, *, yes: bool = False, abandon_cloud_hosts: bool = False, ssh_config: Path | None = None
) -> None:
    for path in (paths.state, paths.cache, paths.control):
        _validate_owned_directory(path, paths.config)
    if paths.config.is_symlink():
        raise PreconditionError("Offbox config directory is a symlink", "Restore the owned config directory before uninstalling.")
    state = StateStore(paths.state).read()
    if state.hosts:
        names = ", ".join(sorted(state.hosts))
        fix = "; ".join(f"offbox host destroy {name}" for name in sorted(state.hosts))
        cloud = [host for _, host in sorted(state.hosts.items()) if _is_cloud(host)]
        retain = "offbox uninstall --yes --abandon-cloud-hosts" if cloud else "offbox uninstall --yes"
        if not yes:
            raise PreconditionError(
                f"Hosts remain: {names}. Uninstall does not destroy remote hosts.",
                f"Run {fix} first, or {retain} to retain the hosts.",
            )
        if cloud and not abandon_cloud_hosts:
            raise PreconditionError(
                "Cloud hosts remain and keep billing after uninstall: " + "; ".join(_describe(host) for host in cloud),
                "Run "
                + "; ".join(f"offbox host destroy {host.name} --yes" for host in cloud)
                + " first, or add --abandon-cloud-hosts to keep them running without local records.",
            )
        for host in cloud:
            console.warn(f"Abandoning cloud host {_describe(host)}; delete it in the provider console to stop billing.")
        console.warn(f"Hosts remain remotely: {names}. Their local provisioning state will be removed.")
    with ExitStack() as locks:
        for name in sorted(state.hosts):
            locks.enter_context(host_operation(paths, name))
        SyncManager(runner).terminate_all()
        for host in state.hosts.values():
            if host.address:
                close_master(runner, host.name)
        if StateStore(paths.state).read() != state:
            raise PreconditionError(
                "Local state changed during uninstall", "Wait for other offbox operations to finish and retry uninstall."
            )
        _stop_cached_mutagen_daemon(paths, runner)
        try:
            close_logs()
            for path in dict.fromkeys((paths.cache, paths.control, paths.state)):
                if path.exists():
                    shutil.rmtree(path)
            remove_include(paths, ssh_config)
            (paths.config / "ssh_config").unlink(missing_ok=True)
        except OSError as exc:
            raise PreconditionError(
                "Cannot remove offbox local resources", "Check directory permissions and rerun offbox uninstall --yes."
            ) from exc
    console.success("Removed offbox local resources; remote hosts and user configuration are retained")
