import json
import logging
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from offbox.config import Model
from offbox.paths import Paths

_registered_secrets: set[str] = set()
_SECRET_NAME = re.compile(r"(?:TOKEN|SECRET|PASSWORD|KEY)$", re.IGNORECASE)
_invocation: ContextVar[str] = ContextVar("offbox_invocation", default="")
_environment: ContextVar[str | None] = ContextVar("offbox_environment", default=None)


class LogEntry(Model):
    timestamp: datetime
    invocation: str
    env: str | None
    level: str
    logger: str
    message: str


def bind_environment(name: str) -> None:
    _environment.set(name)
    logging.getLogger(__name__).debug("worktree context established")


def close_logs() -> None:
    root = logging.getLogger("offbox")
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return LogEntry(
            timestamp=datetime.fromtimestamp(record.created, UTC),
            invocation=_invocation.get(),
            env=_environment.get(),
            level=record.levelname,
            logger=record.name,
            message=redact(message),
        ).model_dump_json()


def register_secrets(env: Mapping[str, str]) -> None:
    values = [value for name, value in env.items() if value and _SECRET_NAME.search(name)]
    _registered_secrets.update(values)
    _registered_secrets.update(part for value in values for part in value.splitlines() if part)


def register_exec_secrets(argv: Sequence[str]) -> None:
    command = next((index for index, value in enumerate(argv) if not value.startswith("-")), None)
    if command is None or argv[command] != "exec":
        return
    index = command + 1
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            return
        entry: str | None = None
        if argument == "--env":
            index += 1
            if index >= len(argv) or argv[index] == "--":
                return
            entry = argv[index]
        elif argument.startswith("--env="):
            entry = argument[len("--env=") :]
        if entry is not None:
            name, separator, value = entry.partition("=")
            if separator:
                register_secrets({name: value})
        index += 1


def redact(value: str, env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    secrets = sorted(
        _registered_secrets | {item for name, item in values.items() if item and _SECRET_NAME.search(name)}, key=len, reverse=True
    )
    for secret in secrets:
        for variant in sorted({secret, repr(secret)[1:-1], json.dumps(secret)[1:-1]}, key=len, reverse=True):
            value = value.replace(variant, "[REDACTED]")
    return value


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure(paths: Paths, argv: Sequence[str], day: date | None = None) -> Path:
    current = day or date.today()
    paths.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    for candidate in paths.logs.glob("*.log"):
        try:
            file_day = date.fromisoformat(candidate.stem)
        except ValueError:
            continue
        if file_day <= current - timedelta(days=14):
            candidate.unlink()
    target = paths.log_file(current)
    handler = logging.FileHandler(target, encoding="utf-8")
    target.chmod(0o600)
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger("offbox")
    close_logs()
    _invocation.set(uuid.uuid4().hex)
    _environment.set(None)
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    root.propagate = False
    root.info("invocation argv=%s", [redact(arg) for arg in argv])
    return target
