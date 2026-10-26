import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from offbox.errors import PreconditionError
from offbox.paths import Paths
from offbox.state import EnvState

logger = logging.getLogger(__name__)
_ENV_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,36}")


def activity_path(paths: Paths, env: str) -> Path:
    if _ENV_NAME.fullmatch(env) is None:
        raise PreconditionError("Invalid environment name", "Repair the environment state.")
    return paths.state / "activity" / env


def touch(paths: Paths, env: str, now: datetime) -> None:
    """Record activity without locks; a failure is logged and never stops the command."""
    path = activity_path(paths, env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.touch(mode=0o600)
        stamp = now.timestamp()
        os.utime(path, (stamp, stamp))
    except OSError:
        logger.warning("cannot record activity for %s", env, exc_info=True)


def last_activity(paths: Paths, env: EnvState) -> datetime:
    try:
        return max(env.created_at, datetime.fromtimestamp(activity_path(paths, env.name).stat().st_mtime, UTC))
    except OSError:
        return env.created_at


def forget(paths: Paths, env: str) -> None:
    try:
        activity_path(paths, env).unlink(missing_ok=True)
    except OSError:
        logger.warning("cannot remove activity record for %s", env, exc_info=True)


def worktree_gone(root: Path) -> bool:
    """True only for a removed worktree: the path is missing while its parent exists. Unreadable or unmounted paths count as present."""
    try:
        root.stat()
        return False
    except (FileNotFoundError, NotADirectoryError):
        try:
            return root.parent.is_dir()
        except OSError:
            return False
    except OSError:
        return False
