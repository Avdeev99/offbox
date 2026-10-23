import os
import signal
import subprocess
from collections.abc import Sequence

import pytest

from offbox.errors import PreconditionError
from offbox.runner import FakeRunner, Script, SubprocessRunner


class Process:
    def __init__(self, results: Sequence[int | None] = ()) -> None:
        self.pid = 123456
        self.returncode: int | None = None
        self.results = list(results)
        self.waits: list[float | None] = []
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.returncode is not None:
            return self.returncode
        value = self.results.pop(0) if self.results else None
        if value is None:
            raise subprocess.TimeoutExpired(["ssh"], timeout or 0)
        self.returncode = value
        return value

    def kill(self) -> None:
        self.killed = True
        self.returncode = -signal.SIGKILL


@pytest.mark.parametrize(("stdin", "tty"), [(False, False), (True, False), (False, True)])
def test_start_stream_preserves_streams_and_controls_only_non_tty_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: bool, tty: bool
) -> None:
    process = Process([42])
    options: dict[str, object] = {}
    seen: list[list[str]] = []

    def popen(argv: Sequence[str], **kwargs: object) -> Process:
        seen.append(list(argv))
        options.update(kwargs)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    handle = SubprocessRunner().start_stream(["ssh", "host", "argument with spaces"], stdin=stdin, tty=tty)
    assert handle.wait() == 42
    assert seen == [["ssh", "host", "argument with spaces"]]
    assert options["stdin"] == (None if stdin or tty else subprocess.DEVNULL)
    assert options["start_new_session"] is (not tty)
    assert "stdout" not in options
    assert "stderr" not in options
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


def test_stream_handle_timeout_does_not_kill_live_command(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Process([None, 255])

    def popen(argv: Sequence[str], **kwargs: object) -> Process:
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    handle = SubprocessRunner().start_stream(["ssh", "host"])
    assert handle.poll() is None
    assert handle.wait(0.1) is None
    assert process.killed is False
    assert handle.wait(0.1) == 255
    assert handle.poll() == 255
    assert process.waits == [0.1, 0.1]


@pytest.mark.parametrize("tty", [False, True])
def test_stream_kill_reaps_own_process_without_killing_shared_tty_group(monkeypatch: pytest.MonkeyPatch, tty: bool) -> None:
    process = Process()
    groups: list[tuple[int, int]] = []

    def popen(argv: Sequence[str], **kwargs: object) -> Process:
        return process

    def killpg(pid: int, signum: int) -> None:
        groups.append((pid, signum))
        process.returncode = -signum

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "killpg", killpg)
    handle = SubprocessRunner().start_stream(["ssh", "host"], tty=tty)
    handle.kill()
    assert handle.poll() == -signal.SIGKILL
    assert process.waits == [1.0]
    assert groups == ([] if tty else [(123456, signal.SIGKILL)])
    assert process.killed is tty


def test_stream_kill_has_bounded_reap_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Process()

    def popen(argv: Sequence[str], **kwargs: object) -> Process:
        return process

    def killpg(pid: int, signum: int) -> None:
        pass

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "killpg", killpg)
    handle = SubprocessRunner().start_stream(["ssh", "host"])
    with pytest.raises(PreconditionError, match="reap"):
        handle.kill()
    assert process.waits == [1.0]


def test_fake_stream_scripts_observations_completion_and_cleanup() -> None:
    runner = FakeRunner([Script(prefix=["ssh"], method="start_stream", returncode=42, stream_results=[None, None, 42])])
    handle = runner.start_stream(["ssh", "host"], stdin=True)
    assert handle.poll() is None
    assert handle.wait(0.1) is None
    assert handle.wait(0.1) == 42
    assert handle.poll() == 42
    assert runner.calls[0].method == "start_stream"
    assert runner.calls[0].argv == ["ssh", "host"]
    assert runner.calls[0].stdin is True
    assert runner.calls[0].capture is False
    assert runner.processes[0].waits == [0.1, 0.1]
    assert runner.processes[0].killed is False


def test_fake_stream_kill_records_cleanup_and_finishes() -> None:
    runner = FakeRunner([Script(prefix=["ssh"], method="start_stream", stream_results=[None, None])])
    handle = runner.start_stream(["ssh", "host"])
    assert handle.poll() is None
    handle.kill()
    assert handle.poll() == -signal.SIGKILL
    assert handle.wait(0) == -signal.SIGKILL
    assert runner.processes[0].killed is True


def test_fake_pending_stream_remains_running_until_explicit_completion_or_kill() -> None:
    runner = FakeRunner([Script(prefix=["ssh"], method="start_stream", stream_results=[None])])
    handle = runner.start_stream(["ssh", "host"])
    assert handle.poll() is None
    assert handle.wait(0.1) is None
    assert handle.wait(0.1) is None
    handle.kill()
    assert handle.poll() == -signal.SIGKILL


def test_fake_unbounded_wait_requires_a_scripted_completion() -> None:
    runner = FakeRunner([Script(prefix=["ssh"], method="start_stream", stream_results=[None])])
    handle = runner.start_stream(["ssh", "host"])
    with pytest.raises(PreconditionError, match="scripted completion"):
        handle.wait()


def test_start_stream_launch_errors_and_empty_argv_are_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def popen(argv: Sequence[str], **kwargs: object) -> Process:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(PreconditionError, match="Cannot run missing"):
        SubprocessRunner().start_stream(["missing"])
    with pytest.raises(PreconditionError, match="Empty command"):
        SubprocessRunner().start_stream([])
