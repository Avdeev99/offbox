import signal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from offbox.errors import CommandFailed, PreconditionError
from offbox.runner import Result, StreamProcess


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["run", "stream", "start_stream"]
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
    model_config = ConfigDict(extra="forbid")

    prefix: list[str]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    method: Literal["run", "stream", "start_stream"] = "run"
    stream_results: list[int | None] = Field(default_factory=list[int | None])


class FakeStreamProcess:
    def __init__(self, returncode: int, results: Sequence[int | None] = ()) -> None:
        self.results = list(results) if results else [returncode]
        self.returncode: int | None = None
        self.waits: list[float | None] = []
        self.killed = False

    def poll(self) -> int | None:
        if self.returncode is None:
            code = self.results.pop(0) if self.results else None
            if code is not None:
                self.returncode = code
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        self.waits.append(timeout)
        result = self.poll()
        while result is None and timeout is None:
            if not self.results:
                raise PreconditionError("Fake stream has no scripted completion")
            result = self.poll()
        return result

    def kill(self) -> None:
        if self.poll() is None:
            self.killed = True
            self.returncode = -signal.SIGKILL
            self.results.clear()


class FakeRunner:
    def __init__(self, scripts: Sequence[Script] = ()) -> None:
        self.scripts = list(scripts)
        self.calls: list[Call] = []
        self.processes: list[FakeStreamProcess] = []

    def _script(self, call: Call) -> Script:
        self.calls.append(call)
        for index, script in enumerate(self.scripts):
            if call.method == script.method and call.argv[: len(script.prefix)] == script.prefix:
                self.scripts.pop(index)
                return script
        raise PreconditionError(f"Unexpected {call.method} call: {call.argv}")

    def _match(self, call: Call) -> Result:
        script = self._script(call)
        return Result(argv=call.argv, returncode=script.returncode, stdout=script.stdout, stderr=script.stderr)

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

    def start_stream(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdin: bool = False, tty: bool = False
    ) -> StreamProcess:
        call = Call(method="start_stream", argv=list(argv), capture=False, env=dict(env) if env is not None else None, stdin=stdin, tty=tty)
        script = self._script(call)
        process = FakeStreamProcess(script.returncode, script.stream_results)
        self.processes.append(process)
        return process
