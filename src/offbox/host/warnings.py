import logging
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, ValidationError

from offbox.config import Model
from offbox.paths import Paths
from offbox.ssh import write_private

logger = logging.getLogger(__name__)


class HostWarning(Model):
    host: str
    message: str
    created_at: datetime


class HostWarnings(Model):
    warnings: list[HostWarning] = Field(default_factory=list[HostWarning])


def _warnings_path(paths: Paths) -> Path:
    return paths.state / "warnings.json"


def read_warnings(paths: Paths) -> list[HostWarning]:
    try:
        return HostWarnings.model_validate_json(_warnings_path(paths).read_text()).warnings
    except FileNotFoundError:
        return []
    except (OSError, ValidationError):
        logger.warning("ignoring unreadable host warnings", exc_info=True)
        return []


def _write_warnings(paths: Paths, warnings: list[HostWarning]) -> None:
    write_private(_warnings_path(paths), HostWarnings(warnings=warnings).model_dump_json(indent=2) + "\n")


def dismiss_warning(paths: Paths, name: str) -> bool:
    current = read_warnings(paths)
    kept = [warning for warning in current if warning.host != name]
    if len(kept) == len(current):
        return False
    _write_warnings(paths, kept)
    return True


def warn_interrupted(paths: Paths, name: str) -> None:
    """Keep a reminder after destroying a host whose creation was killed mid-apply, which can leave untracked resources."""
    warnings = [warning for warning in read_warnings(paths) if warning.host != name]
    warnings.append(
        HostWarning(
            host=name,
            message=f"Creation of {name} was interrupted; check the provider console for resources labelled offbox-host={name}",
            created_at=datetime.now(UTC),
        )
    )
    _write_warnings(paths, warnings)
