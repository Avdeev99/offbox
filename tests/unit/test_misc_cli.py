from pathlib import Path

import pytest
from typer.testing import CliRunner

from offbox.cli import app
from offbox.paths import Paths
from offbox.ports import PortForward, PortsReport
from offbox.runner import Runner
from offbox.setup import SetupOptions
from offbox.status import Status, StatusCheck, StatusReport


@pytest.mark.parametrize("json_output", [False, True])
def test_status_cli_preserves_json_stdout_and_exact_human_problem_fixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_output: bool
) -> None:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    report = StatusReport(
        env="app-abcd",
        host="main",
        checks=[
            StatusCheck(name="disk", level="warning", message="Host disk 80% used", fix="Run offbox gc or offbox rm in unused worktrees."),
            StatusCheck(name="host", level="error", message="Host unreachable", fix="Run offbox host ssh main."),
        ],
    )

    def diagnose(self: Status, *, cwd: Path | None = None) -> StatusReport:
        return report

    monkeypatch.setattr(Status, "diagnose", diagnose)
    monkeypatch.setattr("offbox.cli.misc.Paths.discover", lambda: paths)
    result = CliRunner().invoke(app, ["status", *(["--json"] if json_output else [])])
    assert result.exit_code == 3
    if json_output:
        assert StatusReport.model_validate_json(result.stdout) == report
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert (
            result.stderr
            == "! Host disk 80% used\n  Fix: Run offbox gc or offbox rm in unused worktrees.\n✗ Host unreachable\n  Fix: Run offbox host ssh main.\n"
        )


def test_logs_cli_accepts_optional_stack_service_and_follow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    seen: list[tuple[bool, str | None]] = []

    def logs(selected: Paths, runner: Runner, *, follow: bool = False, service: str | None = None, cwd: Path | None = None) -> int:
        assert selected == paths
        seen.append((follow, service))
        return 0

    monkeypatch.setattr("offbox.cli.misc.Paths.discover", lambda: paths)
    monkeypatch.setattr("offbox.cli.misc.stack_logs", logs)
    result = CliRunner().invoke(app, ["logs", "--stack", "web", "--follow"])
    assert result.exit_code == 0
    assert seen == [(True, "web")]
    assert result.stdout == "" and result.stderr == ""


def test_setup_cli_non_tty_passes_explicit_options_without_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Callable

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    seen: list[SetupOptions] = []

    def setup(
        selected: Paths,
        runner: Runner,
        options: SetupOptions,
        *,
        ask: Callable[[str, str], str] | None = None,
        confirm: Callable[[str], bool] | None = None,
    ) -> None:
        assert selected == paths
        assert ask is None and confirm is None
        seen.append(options)

    monkeypatch.setattr("offbox.cli.misc.Paths.discover", lambda: paths)
    monkeypatch.setattr("offbox.cli.misc.run_setup", setup)
    result = CliRunner().invoke(
        app, ["setup", "--provider", "ssh", "--action", "init", "--target", "owner@example.com", "--port", "2222", "--accept-host-key"]
    )
    assert result.exit_code == 0
    assert seen == [SetupOptions(provider="ssh", action="init", target="owner@example.com", port=2222, accept_host_key=True)]


def test_human_status_lists_each_port_mapping_and_active_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    report = StatusReport(
        env="app-abcd",
        host="main",
        checks=[StatusCheck(name="ports", level="error", message="Port restore failed", fix="Run offbox ports.")],
        ports=PortsReport(
            forwards=[
                PortForward(env="app-abcd", host="main", remote=8000, local=8001, ip=None, active=False),
                PortForward(env="app-abcd", host="main", remote=3000, local=3000, ip=None, active=True),
            ]
        ),
    )

    def diagnose(self: Status, *, cwd: Path | None = None) -> StatusReport:
        return report

    monkeypatch.setattr(Status, "diagnose", diagnose)
    monkeypatch.setattr("offbox.cli.misc.Paths.discover", lambda: paths)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 3
    assert result.stdout == ""
    assert (
        result.stderr
        == "✗ Port restore failed\n  Fix: Run offbox ports.\n  localhost:8001 → app-abcd:8000 (inactive)\n  localhost:3000 → app-abcd:3000 (active)\n"
    )
