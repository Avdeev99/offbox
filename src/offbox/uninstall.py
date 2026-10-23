import os
import shutil
import stat
from contextlib import ExitStack
from pathlib import Path

from offbox import console
from offbox.errors import PreconditionError
from offbox.host import host_operation
from offbox.logging import close_logs
from offbox.paths import Paths
from offbox.runner import Runner
from offbox.ssh import close_master, remove_include
from offbox.state import StateStore
from offbox.sync import SyncManager


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


def uninstall(paths: Paths, runner: Runner, *, yes: bool = False, ssh_config: Path | None = None) -> None:
    for path in (paths.state, paths.cache, paths.control):
        _validate_owned_directory(path, paths.config)
    if paths.config.is_symlink():
        raise PreconditionError("Offbox config directory is a symlink", "Restore the owned config directory before uninstalling.")
    state = StateStore(paths.state).read()
    if state.hosts:
        names = ", ".join(sorted(state.hosts))
        fix = "; ".join(f"offbox host destroy {name}" for name in sorted(state.hosts))
        if not yes:
            raise PreconditionError(
                f"Hosts remain: {names}. Uninstall does not destroy remote hosts.",
                f"Run {fix} first, or offbox uninstall --yes to retain the hosts.",
            )
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
