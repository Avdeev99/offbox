import logging
import os
import selectors
import shlex
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from offbox import console
from offbox.errors import CommandFailed, PreconditionError, ProviderInterrupted
from offbox.logging import redact, register_secrets

logger = logging.getLogger(__name__)


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class StreamProcess(Protocol):
    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int | None:
        """Return None if a bounded wait expires while the process is still running."""
        ...

    def kill(self) -> None: ...


class _SubprocessStream:
    def __init__(self, process: subprocess.Popen[bytes], *, grouped: bool, started: float) -> None:
        self.process = process
        self.grouped = grouped
        self.started = started
        self.finished = False

    def _finish(self, code: int | None) -> int | None:
        if code is not None and not self.finished:
            logger.debug("stream exit=%s duration=%.3f", code, time.monotonic() - self.started)
            self.finished = True
        return code

    def poll(self) -> int | None:
        try:
            return self._finish(self.process.poll())
        except OSError as exc:
            raise PreconditionError("Cannot inspect streaming command", "Check the local process and retry.") from exc

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self._finish(self.process.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            return None
        except OSError as exc:
            raise PreconditionError("Cannot wait for streaming command", "Check the local process and retry.") from exc

    def kill(self) -> None:
        if self.poll() is not None:
            return
        try:
            with suppress(ProcessLookupError):
                if self.grouped:
                    os.killpg(self.process.pid, signal.SIGKILL)
                else:
                    self.process.kill()
        except OSError as exc:
            raise PreconditionError("Cannot stop streaming command", "Stop the local SSH process and inspect the remote command.") from exc
        if self.wait(timeout=1.0) is None:
            raise PreconditionError("Cannot reap streaming command within 1s", "Inspect and stop the local SSH process manually.")


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
        interruptible: bool = False,
    ) -> Result: ...

    def stream(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdin: bool = False, tty: bool = False) -> int: ...

    def start_stream(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdin: bool = False, tty: bool = False
    ) -> StreamProcess: ...


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

    def _log_output(self, name: str, content: bytes, env: Mapping[str, str]) -> None:
        text = redact(content.decode(errors="replace").rstrip("\r\n"), env)
        logger.info("%s: %s", name, text)
        if self.verbose:
            console.info(text)

    def _run_logged(
        self, argv: Sequence[str], env: Mapping[str, str], cwd: Path | None, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        deadline = None if timeout is None else time.monotonic() + timeout

        def remaining() -> float | None:
            if deadline is None:
                return None
            duration = deadline - time.monotonic()
            if duration <= 0:
                raise subprocess.TimeoutExpired(list(argv), timeout or 0)
            return duration

        with subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd, start_new_session=True
        ) as process:
            if process.stdout is None or process.stderr is None:
                raise PreconditionError("Cannot capture provider output", "Check subprocess pipe support and retry.")
            stdout, stderr = process.stdout.fileno(), process.stderr.fileno()
            outputs = {stdout: bytearray(), stderr: bytearray()}
            pending = {stdout: bytearray(), stderr: bytearray()}
            names = {stdout: "stdout", stderr: "stderr"}
            interrupts = 0

            def interrupt() -> None:
                nonlocal interrupts
                interrupts += 1
                if interrupts == 1:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGINT)
                    console.warn("Interrupt received; waiting for the provider to save state. Do not close this terminal.")
                elif interrupts == 2:
                    console.warn(
                        "The provider is still finishing and recording created resources. "
                        "Press Ctrl-C again to force-stop it; untracked cloud resources may keep billing."
                    )
                else:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise ProviderInterrupted(
                        "Provider operation was force-stopped before saving its state",
                        "Check the provider console for resources labelled offbox-host and delete them, then run offbox host destroy NAME.",
                        killed=True,
                    )

            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    selector.register(process.stderr, selectors.EVENT_READ)
                    while selector.get_map():
                        try:
                            for key, _ in selector.select(remaining()):
                                chunk = os.read(key.fd, 65536)
                                if not chunk:
                                    selector.unregister(key.fd)
                                    if pending[key.fd]:
                                        self._log_output(names[key.fd], bytes(pending[key.fd]), env)
                                    continue
                                outputs[key.fd].extend(chunk)
                                pending[key.fd].extend(chunk)
                                while b"\n" in pending[key.fd]:
                                    end = pending[key.fd].index(b"\n") + 1
                                    self._log_output(names[key.fd], bytes(pending[key.fd][:end]), env)
                                    del pending[key.fd][:end]
                        except KeyboardInterrupt:
                            interrupt()
                while True:
                    try:
                        returncode = process.wait(timeout=remaining())
                        break
                    except KeyboardInterrupt:
                        interrupt()
                if interrupts:
                    raise ProviderInterrupted(
                        "Provider operation interrupted", "Run offbox host list and destroy the interrupted host.", killed=False
                    )
            except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
            return subprocess.CompletedProcess(
                list(argv), returncode, bytes(outputs[stdout]).decode(errors="replace"), bytes(outputs[stderr]).decode(errors="replace")
            )

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
        interruptible: bool = False,
    ) -> Result:
        start, values = self._start(argv, env)
        try:
            if interruptible:
                completed = self._run_logged(argv, values, cwd, timeout)
            else:
                completed = subprocess.run(
                    list(argv),
                    capture_output=capture,
                    input=input,
                    stdin=subprocess.DEVNULL if input is None else None,
                    env=values,
                    cwd=cwd,
                    timeout=timeout,
                    text=True,
                    check=False,
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

    def start_stream(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdin: bool = False, tty: bool = False
    ) -> StreamProcess:
        started, values = self._start(argv, env)
        try:
            process = subprocess.Popen(
                list(argv), env=values, stdin=None if stdin or tty else subprocess.DEVNULL, start_new_session=not tty
            )
        except OSError as exc:
            logger.error("stream launch failed duration=%.3f", time.monotonic() - started)
            raise PreconditionError(f"Cannot run {argv[0]}", f"Install {argv[0]} and check its permissions.") from exc
        return _SubprocessStream(process, grouped=not tty, started=started)
