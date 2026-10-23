import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

from offbox.paths import Paths

_registered_secrets: set[str] = set()
_SECRET_NAME = re.compile(r"(?:TOKEN|SECRET|PASSWORD|KEY)$", re.IGNORECASE)


def register_secrets(env: Mapping[str, str]) -> None:
    _registered_secrets.update(value for name, value in env.items() if value and _SECRET_NAME.search(name))


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
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("offbox")
    for previous in root.handlers[:]:
        root.removeHandler(previous)
        previous.close()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    root.propagate = False
    root.info("invocation argv=%s", [redact(arg) for arg in argv])
    return target
