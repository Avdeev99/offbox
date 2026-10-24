import logging
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.errors import CommandFailed, PreconditionError
from offbox.logging import configure
from offbox.paths import Paths
from offbox.runner import SubprocessRunner


def test_fake_records_exact_options_and_prefixes(tmp_path: Path) -> None:
    runner = FakeRunner([Script(prefix=["git"], stdout="ok"), Script(prefix=["ssh"], method="stream", returncode=42)])
    assert runner.run(["git", "status"], check=False, capture=True, input="data", env={"CI": "1"}, cwd=tmp_path, timeout=2).stdout == "ok"
    call = runner.calls[0]
    assert call.argv == ["git", "status"]
    assert (call.check, call.capture, call.input, call.env, call.cwd, call.timeout) == (False, True, "data", {"CI": "1"}, tmp_path, 2)
    assert runner.stream(["ssh", "host"], stdin=True, tty=True) == 42
    assert runner.calls[1].stdin is True
    assert runner.calls[1].tty is True
    with pytest.raises(PreconditionError, match="Unexpected run"):
        runner.run(["other"])


def test_fake_check_failure() -> None:
    runner = FakeRunner([Script(prefix=["bad"], returncode=4, stderr="bad output")])
    with pytest.raises(CommandFailed) as caught:
        runner.run(["bad", "args"])
    assert caught.value.returncode == 4
    assert caught.value.stderr_tail == "bad output"


def test_run_options_and_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    observed: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append((list(argv), kwargs))
        return subprocess.CompletedProcess(list(argv), 42, "output", "error")

    monkeypatch.setattr(subprocess, "run", run)
    runner = SubprocessRunner()
    result = runner.run(
        ["tool", "argument with space"], check=False, capture=False, input="input", env={"LOCAL_VAR": "yes"}, cwd=tmp_path, timeout=2.5
    )
    assert (result.argv, result.returncode, result.stdout, result.stderr) == (["tool", "argument with space"], 42, "output", "error")
    argv, kwargs = observed[0]
    assert argv == ["tool", "argument with space"]
    assert kwargs["capture_output"] is False
    assert kwargs["input"] == "input"
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 2.5
    assert kwargs["text"] is True
    env = kwargs["env"]
    assert isinstance(env, Mapping)
    assert env["LOCAL_VAR"] == "yes"
    with pytest.raises(CommandFailed, match="exit 42"):
        runner.run(["tool"])


@pytest.mark.parametrize(("stdin", "tty", "inherited"), [(False, False, False), (True, False, True), (False, True, True)])
def test_stream_preserves_status_and_io(monkeypatch: pytest.MonkeyPatch, stdin: bool, tty: bool, inherited: bool) -> None:
    seen: list[dict[str, object]] = []

    def run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs)
        return subprocess.CompletedProcess(list(argv), 255)

    monkeypatch.setattr(subprocess, "run", run)
    assert SubprocessRunner().stream(["ssh", "host"], stdin=stdin, tty=tty) == 255
    assert seen[0]["stdin"] == (None if inherited else subprocess.DEVNULL)
    assert "stdout" not in seen[0]
    assert "stderr" not in seen[0]


@pytest.mark.parametrize("secret", ["runner-token", "s'ecret", "both'\"quotes", "first\nsecond"])
def test_redacts_verbose_errors_and_tracebacks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], secret: str
) -> None:
    path = configure(Paths(tmp_path, tmp_path, tmp_path, tmp_path), ["doctor"])

    def run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), 1, stderr=secret)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(PreconditionError) as caught:
        try:
            SubprocessRunner(verbose=True).run(["tool", secret], env={"API_TOKEN": secret}, timeout=1)
        except PreconditionError:
            logging.getLogger("offbox.test").exception("timeout")
            raise
    text = capsys.readouterr().err + path.read_text() + caught.value.message
    assert secret not in text
    assert repr(secret)[1:-1] not in text
    assert "[REDACTED]" in text


def test_launch_error_and_empty_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(PreconditionError, match="Cannot run missing"):
        SubprocessRunner().run(["missing"])
    with pytest.raises(PreconditionError, match="Cannot run missing"):
        SubprocessRunner().stream(["missing"])
    with pytest.raises(PreconditionError, match="Empty command"):
        SubprocessRunner().run([])


@pytest.mark.parametrize("input", [None, "explicit script input"])
def test_administrative_commands_do_not_inherit_user_stdin(monkeypatch: pytest.MonkeyPatch, input: str | None) -> None:
    seen: list[tuple[object, object]] = []

    def run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append((kwargs.get("stdin"), kwargs.get("input")))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    SubprocessRunner().run(["ssh", "host", "inspect"], input=input)
    assert seen == [(subprocess.DEVNULL if input is None else None, input)]


@pytest.mark.subprocess
def test_administrative_read_cannot_consume_a_later_user_stream() -> None:
    import sys

    script = """from offbox.runner import SubprocessRunner
runner = SubprocessRunner()
runner.run(['cat'])
process = runner.start_stream(['cat'], stdin=True)
status = process.wait(timeout=2)
if status is None:
    process.kill()
    raise SystemExit(1)
raise SystemExit(status)
"""
    result = SubprocessRunner().run([sys.executable, "-c", script], input="user payload\n", timeout=5)
    assert result.stdout == "user payload\n"
    assert result.stderr == ""
