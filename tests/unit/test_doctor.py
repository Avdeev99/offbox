from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeRunner, Script
from typer.testing import CliRunner

from offbox import cli
from offbox.doctor import Check, DoctorReport, check_key, diagnose
from offbox.errors import PreconditionError
from offbox.paths import Paths
from offbox.runner import SubprocessRunner
from offbox.state import HostState, State, StateStore
from offbox.tools import ToolDiscovery


def _tool_scripts() -> list[Script]:
    return [
        Script(prefix=["ssh", "-V"], stderr="OpenSSH_9.9"),
        Script(prefix=["git", "--version"], stdout="git version 2.49.0"),
        Script(prefix=["mutagen", "--version"], stdout="mutagen version 0.18.1"),
        Script(prefix=["tofu", "--version"], stdout="OpenTofu v1.10.6"),
    ]


def _paths(root: Path) -> Paths:
    return Paths(root / "config", root / "state", root / "cache", root / "control")


def _register_default_host(paths: Paths, root: Path) -> None:
    StateStore(paths.state).write(
        State(
            default_host="main",
            hosts={
                "main": HostState(
                    name="main",
                    provider="ssh",
                    address="192.0.2.1",
                    ssh_user="offbox",
                    created_at=datetime(2026, 10, 23, tzinfo=UTC),
                    known_hosts_path=root / "known_hosts",
                )
            },
        )
    )


def test_all_doctor_checks_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = FakeRunner(
        [*_tool_scripts(), Script(prefix=["ssh", "-o"]), Script(prefix=["ssh-add", "-l"], stdout="256 SHA256:agent test (ED25519)")]
    )
    paths = _paths(tmp_path)
    _register_default_host(paths, tmp_path)
    report = diagnose(paths, runner, project_root=tmp_path, tools=ToolDiscovery(runner, paths.cache, lookup=lambda name: name))
    assert report.ok is True
    assert report.exit_code == 0
    assert [check.name for check in report.checks] == [
        "ssh",
        "git",
        "mutagen",
        "tofu",
        "user-config",
        "project-config",
        "credentials",
        "state",
        "host",
        "ssh-key",
    ]
    assert runner.calls[-1].argv == ["ssh-add", "-l"]
    assert DoctorReport.model_validate_json(report.model_dump_json()) == report


def test_default_host_unreachable_exit_three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = _paths(tmp_path)
    StateStore(paths.state).write(
        State(
            default_host="main",
            hosts={
                "main": HostState(
                    name="main",
                    provider="ssh",
                    address="192.0.2.1",
                    ssh_user="offbox",
                    created_at=datetime(2026, 10, 23, tzinfo=UTC),
                    known_hosts_path=tmp_path / "known_hosts",
                )
            },
        )
    )
    runner = FakeRunner([*_tool_scripts(), Script(prefix=["ssh", "-o"], returncode=255), Script(prefix=["ssh-add", "-l"], returncode=1)])
    report = diagnose(paths, runner, project_root=tmp_path, tools=ToolDiscovery(runner, paths.cache, lookup=lambda name: name))
    assert report.exit_code == 3
    assert [(check.name, check.ok) for check in report.checks if not check.ok] == [("host", False), ("ssh-key", False)]
    assert runner.calls[4].argv == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "offbox-main", "--", "true"]
    assert all(check.fix for check in report.checks if not check.ok)


def test_invalid_config_and_state_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = _paths(tmp_path)
    paths.config.mkdir()
    paths.state.mkdir()
    paths.user_config.write_bytes(b"\xff")
    paths.credentials.write_bytes(b"\xff")
    paths.credentials.chmod(0o600)
    (paths.state / "state.json").write_bytes(b"\xff")
    (tmp_path / "offbox.toml").write_text('unknown="key"')
    runner = FakeRunner([*_tool_scripts(), Script(prefix=["ssh-add", "-l"], returncode=1)])
    report = diagnose(paths, runner, project_root=tmp_path, tools=ToolDiscovery(runner, paths.cache, lookup=lambda name: name))
    assert report.exit_code == 3
    assert {check.name for check in report.checks if not check.ok} == {"user-config", "credentials", "project-config", "state", "ssh-key"}


def test_key_empty_passphrase_fallback(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.touch()
    runner = FakeRunner([Script(prefix=["ssh-add", "-l"], returncode=1), Script(prefix=["ssh-keygen", "-y"], stdout="ssh-ed25519 public")])
    assert check_key(runner, key).ok is True
    assert runner.calls[1].argv == ["ssh-keygen", "-y", "-P", "", "-f", str(key)]


def test_configured_key_matches_agent(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.touch()
    runner = FakeRunner(
        [
            Script(prefix=["ssh-add", "-l"], stdout="256 SHA256:match identity"),
            Script(prefix=["ssh-keygen", "-lf"], stdout="256 SHA256:match identity"),
        ]
    )
    assert check_key(runner, key, configured=True).ok is True
    assert runner.calls[1].argv == ["ssh-keygen", "-lf", str(key)]


def test_missing_configured_key_unrelated_agent_rejected(tmp_path: Path) -> None:
    key = tmp_path / "missing_key"
    runner = FakeRunner(
        [Script(prefix=["ssh-add", "-l"], stdout="256 SHA256:unrelated identity"), Script(prefix=["ssh-keygen", "-lf"], returncode=1)]
    )
    result = check_key(runner, key, configured=True)
    assert result.ok is False
    assert result.fix == f"Generate {key} or load it with ssh-add {key}."
    assert runner.calls[1].argv == ["ssh-keygen", "-lf", str(key) + ".pub"]


@pytest.mark.parametrize("json_mode", [False, True])
def test_doctor_cli_failed_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, json_mode: bool) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    report = DoctorReport(checks=[Check(name="ssh", ok=False, message="missing ssh", fix="Install OpenSSH.")])

    def diagnostic(paths: Paths, runner: SubprocessRunner) -> DoctorReport:
        return report

    monkeypatch.setattr(cli, "diagnose", diagnostic)
    result = CliRunner().invoke(cli.app, ["doctor", "--json"] if json_mode else ["doctor"])
    assert result.exit_code == 3
    if json_mode:
        assert DoctorReport.model_validate_json(result.stdout) == report
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert "✗ missing ssh" in result.stderr
        assert "Fix: Install OpenSSH." in result.stderr


@pytest.mark.parametrize("expected", [True, False])
def test_cli_error_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, expected: bool) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    def diagnostic(paths: Paths, runner: SubprocessRunner) -> DoctorReport:
        if expected:
            raise PreconditionError("broken configuration", "Repair it.")
        raise RuntimeError("test unexpected bug")

    monkeypatch.setattr(cli, "diagnose", diagnostic)
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == (3 if expected else 1)
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert "Fix:" in result.stderr
    assert ("broken configuration" if expected else "unexpected error, see") in result.stderr
    text = next((tmp_path / "offbox/logs").glob("*.log")).read_text()
    assert "Traceback" in text
    assert ("broken configuration" if expected else "test unexpected bug") in text


@pytest.mark.parametrize("arguments", [["--invalid"], ["doctor", "--invalid"], ["unknown"]])
def test_usage_error_has_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 2
    assert "Fix:" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("arguments", [["--version"], ["version"], ["doctor"]])
def test_bad_logging_directory_has_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    obstacle = tmp_path / "file"
    obstacle.touch()
    monkeypatch.setenv("XDG_STATE_HOME", str(obstacle))
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 1
    assert "Fix:" in result.stderr
    assert "Traceback" not in result.stderr


def test_doctor_discovers_worktree_root_from_nested_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    nested = tmp_path / "src/nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    (tmp_path / "offbox.toml").write_text('[env]\nimgae="typo"\n')
    runner = FakeRunner(
        [*_tool_scripts(), Script(prefix=["git", "rev-parse"], stdout=str(tmp_path) + "\n"), Script(prefix=["ssh-add", "-l"], returncode=1)]
    )
    paths = _paths(tmp_path)
    report = diagnose(paths, runner, tools=ToolDiscovery(runner, paths.cache, lookup=lambda name: name))
    project = next(check for check in report.checks if check.name == "project-config")
    assert project.ok is False
    assert project.message == f"Invalid configuration in {tmp_path / 'offbox.toml'}: env.imgae"
    assert runner.calls[4].argv == ["git", "rev-parse", "--show-toplevel"]
    assert runner.calls[4].cwd == nested


def test_doctor_outside_git_uses_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    runner = FakeRunner(
        [*_tool_scripts(), Script(prefix=["git", "rev-parse"], returncode=128), Script(prefix=["ssh-add", "-l"], returncode=1)]
    )
    paths = _paths(tmp_path)
    report = diagnose(paths, runner, tools=ToolDiscovery(runner, paths.cache, lookup=lambda name: name))
    assert next(check for check in report.checks if check.name == "project-config").ok is True


def test_explicit_setup_key_is_checked_instead_of_missing_default_key(tmp_path: Path) -> None:
    key = tmp_path / "explicit-identity"
    key.write_text("synthetic private key")
    paths = _paths(tmp_path)
    runner = FakeRunner(
        [
            *_tool_scripts(),
            Script(prefix=["ssh-add", "-l"], returncode=1),
            Script(prefix=["ssh-keygen", "-y", "-P", "", "-f", str(key)], stdout="ssh-ed25519 AAAA"),
        ]
    )
    report = diagnose(paths, runner, project_root=tmp_path, tools=ToolDiscovery(runner, paths.cache, lookup=lambda name: name), ssh_key=key)
    assert [check.name for check in report.checks if not check.ok] == ["host"]
    assert report.checks[-1].name == "ssh-key"
    assert report.checks[-1].message == "SSH key is usable without a prompt"
    assert runner.scripts == []


def test_missing_default_host_is_a_failure_pointing_to_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = FakeRunner([*_tool_scripts(), Script(prefix=["ssh-add", "-l"], stdout="256 SHA256:agent test (ED25519)")])
    paths = _paths(tmp_path)
    report = diagnose(paths, runner, project_root=tmp_path, tools=ToolDiscovery(runner, paths.cache, lookup=lambda name: name))
    assert [check for check in report.checks if not check.ok] == [
        Check(name="host", ok=False, message="No default host configured", fix="Run offbox setup.")
    ]
    assert report.exit_code == 3


def test_doctor_reports_the_downloaded_mutagen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.tools import mutagen_directory

    monkeypatch.setenv("HOME", str(tmp_path))
    paths = _paths(tmp_path)
    directory = mutagen_directory(paths.cache)
    directory.mkdir(parents=True)
    (directory / "mutagen-agents.tar.gz").write_bytes(b"agents")
    (directory / "mutagen").write_bytes(b"binary")
    scripts = [script for script in _tool_scripts() if script.prefix[0] != "mutagen"]
    runner = FakeRunner([*scripts, Script(prefix=[str(directory / "mutagen"), "--version"], stdout="Mutagen version 0.18.1")])
    discovery = ToolDiscovery(runner, paths.cache, lookup=lambda name: None if name == "mutagen" else name)
    report = diagnose(paths, runner, project_root=tmp_path, tools=discovery)
    mutagen = next(check for check in report.checks if check.name == "mutagen")
    assert mutagen == Check(name="mutagen", ok=True, message=f"mutagen 0.18.1 ({directory / 'mutagen'}, downloaded by offbox)")
