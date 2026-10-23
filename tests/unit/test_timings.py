from pathlib import Path

import pytest
from typer.testing import CliRunner

from offbox import console
from offbox.cli import app
from offbox.doctor import Check, DoctorReport
from offbox.errors import PreconditionError
from offbox.execute import ExecOptions, Executor
from offbox.paths import Paths
from offbox.runner import Runner
from offbox.timings import collect, measured, stage


def test_timings_accumulate_repeated_stages_and_include_failed_stage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ticks = iter([0.0, 1.0, 3.0, 4.0, 5.0, 6.0, 9.0, 10.0])
    monkeypatch.setattr("offbox.timings.perf_counter", lambda: next(ticks))
    with collect(True):
        with stage("Sync flush"):
            pass
        with stage("Sync flush"):
            pass
        with pytest.raises(PreconditionError), stage("Dependencies"):
            raise PreconditionError("synthetic failure", "Retry.")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Sync flush: 3.000s (2 calls)" in captured.err
    assert "Dependencies: 3.000s" in captured.err
    assert "Total: 10.000s" in captured.err


def test_disabled_timings_do_not_read_clock_or_emit_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def unexpected_clock() -> float:
        raise AssertionError("Disabled timings must not read the clock")

    monkeypatch.setattr("offbox.timings.perf_counter", unexpected_clock)

    @measured("Command execution")
    def command(value: int) -> int:
        return value

    with collect(False):
        assert command(42) == 42
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_timing_session_resets_after_failure(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(PreconditionError), collect(True), stage("Dependencies"):
        raise PreconditionError("synthetic failure", "Retry.")
    assert "Dependencies:" in capsys.readouterr().err
    with stage("Uncollected"):
        pass
    assert capsys.readouterr().err == ""


def test_cli_timings_preserve_stdout_and_remote_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    def execute(_self: Executor, _options: ExecOptions, *, cwd: Path | None = None) -> int:
        with stage("Command execution"):
            console.output("remote payload")
            return 42

    monkeypatch.setattr(Executor, "execute", execute)
    result = CliRunner().invoke(app, ["--timings", "exec", "--", "synthetic-command"])
    assert result.exit_code == 42
    assert result.stdout == "remote payload\n"
    assert "Command execution:" in result.stderr
    assert "Total:" in result.stderr
    ordinary = CliRunner().invoke(app, ["exec", "--", "synthetic-command"])
    assert ordinary.exit_code == 42
    assert ordinary.stdout == "remote payload\n"
    assert ordinary.stderr == ""


def test_cli_timings_leave_json_parseable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    report = DoctorReport(checks=[Check(name="synthetic", ok=True, message="ready")])

    def diagnose(_paths: Paths, _runner: Runner) -> DoctorReport:
        return report

    monkeypatch.setattr("offbox.cli.diagnose", diagnose)
    result = CliRunner().invoke(app, ["--timings", "doctor", "--json"])
    assert result.exit_code == 0
    assert DoctorReport.model_validate_json(result.stdout) == report
    assert "Timings (CLI startup excluded):" in result.stderr


def test_cli_timings_report_failure_without_changing_fix_or_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    def execute(_self: Executor, _options: ExecOptions, *, cwd: Path | None = None) -> int:
        with stage("Sync flush"):
            raise PreconditionError("Source sync failed", "Check the connection.")

    monkeypatch.setattr(Executor, "execute", execute)
    result = CliRunner().invoke(app, ["--timings", "exec", "--", "synthetic-command"])
    assert result.exit_code == 3
    assert result.stdout == ""
    assert "Source sync failed" in result.stderr
    assert "Fix: Check the connection." in result.stderr
    assert "Sync flush:" in result.stderr
