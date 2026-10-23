import logging
import os
import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from offbox import console
from offbox.errors import CommandFailed, PreconditionError
from offbox.logging import redact, register_secrets

logger = logging.getLogger(__name__)


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Result: ...

    def stream(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdin: bool = False, tty: bool = False) -> int: ...


class SubprocessRunner:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def _start(self, argv: Sequence[str], env: Mapping[str, str] | None) -> tuple[float, dict[str, str]]:
        values = dict(os.environ)
        if env is not None:
            values.update(env)
        if not argv:
            raise PreconditionError("Empty command", "Supply a command to run.")
        register_secrets(values)
        display = shlex.join([redact(arg, values) for arg in argv])
        logger.debug("command %s", display)
        if self.verbose:
            console.command(display)
        return time.monotonic(), values

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Result:
        start, values = self._start(argv, env)
        try:
            completed = subprocess.run(
                list(argv), capture_output=capture, input=input, env=values, cwd=cwd, timeout=timeout, text=True, check=False
            )
        except subprocess.TimeoutExpired as exc:
            logger.error("command timeout duration=%.3f", time.monotonic() - start)
            raise PreconditionError(
                f"Command timed out: {shlex.join([redact(arg, values) for arg in argv])}", "Check connectivity and try again."
            ) from exc
        except OSError as exc:
            logger.error("command launch failed duration=%.3f", time.monotonic() - start)
            raise PreconditionError(f"Cannot run {argv[0]}", f"Install {argv[0]} and check its permissions.") from exc
        result = Result(argv=list(argv), returncode=completed.returncode, stdout=completed.stdout or "", stderr=completed.stderr or "")
        logger.debug("command exit=%s duration=%.3f", result.returncode, time.monotonic() - start)
        if check and result.returncode:
            raise CommandFailed([redact(arg, values) for arg in argv], result.returncode, redact(result.stderr, values))
        return result

    def stream(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdin: bool = False, tty: bool = False) -> int:
        start, values = self._start(argv, env)
        try:
            result = subprocess.run(list(argv), env=values, stdin=None if stdin or tty else subprocess.DEVNULL, check=False)
        except OSError as exc:
            logger.error("stream launch failed duration=%.3f", time.monotonic() - start)
            raise PreconditionError(f"Cannot run {argv[0]}", f"Install {argv[0]} and check its permissions.") from exc
        logger.debug("stream exit=%s duration=%.3f", result.returncode, time.monotonic() - start)
        return result.returncode


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["run", "stream"]
    argv: list[str]
    check: bool = True
    capture: bool = True
    input: str | None = None
    env: dict[str, str] | None = None
    cwd: Path | None = None
    timeout: float | None = None
    stdin: bool = False
    tty: bool = False


class Script(BaseModel):
    prefix: list[str]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    method: Literal["run", "stream"] = "run"


class FakeRunner:
    def __init__(self, scripts: Sequence[Script] = ()) -> None:
        self.scripts = list(scripts)
        self.calls: list[Call] = []

    def _match(self, call: Call) -> Result:
        self.calls.append(call)
        for index, script in enumerate(self.scripts):
            if call.method == script.method and call.argv[: len(script.prefix)] == script.prefix:
                self.scripts.pop(index)
                return Result(argv=call.argv, returncode=script.returncode, stdout=script.stdout, stderr=script.stderr)
        raise PreconditionError(f"Unexpected {call.method} call: {call.argv}", "Script this command in FakeRunner.")

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Result:
        call = Call(
            method="run",
            argv=list(argv),
            check=check,
            capture=capture,
            input=input,
            env=dict(env) if env is not None else None,
            cwd=cwd,
            timeout=timeout,
        )
        result = self._match(call)
        if check and result.returncode:
            raise CommandFailed(argv, result.returncode, result.stderr)
        return result

    def stream(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdin: bool = False, tty: bool = False) -> int:
        return self._match(
            Call(method="stream", argv=list(argv), env=dict(env) if env is not None else None, stdin=stdin, tty=tty)
        ).returncode
