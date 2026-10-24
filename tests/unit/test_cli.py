import re
from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.mark.parametrize("arguments", [("--version",), ("version",)])
def test_version_writes_only_the_version_to_stdout(arguments: tuple[str, ...]) -> None:
    from offbox.cli import app

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0
    assert result.stdout == "0.1.0\n"
    assert result.stderr == ""


def test_keyboard_interrupt_is_logged_and_exits_130(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.cli import app
    from offbox.paths import Paths
    from offbox.runner import Runner

    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))

    def interrupted(_paths: Paths, _runner: Runner) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("offbox.cli.host.host_report", interrupted)
    result = CliRunner().invoke(app, ["host", "list"])
    assert result.exit_code == 130
    assert result.stderr == "! Interrupted\n"
    assert "offbox interrupted" in Paths.discover().log_file().read_text()


def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))


def test_fresh_up_without_tty_requires_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.cli import app
    from offbox.runtime import Runtime

    isolate(tmp_path, monkeypatch)

    def up(_self: Runtime, **_options: object) -> None:
        pytest.fail("fresh up ran without confirmation")

    monkeypatch.setattr(Runtime, "up", up)
    result = CliRunner().invoke(app, ["up", "--fresh"])
    assert result.exit_code == 3
    assert "Fix: Run offbox up --fresh --yes." in result.stderr


def test_fresh_up_with_yes_reaches_the_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.cli import app
    from offbox.runtime import Runtime

    isolate(tmp_path, monkeypatch)
    calls: list[tuple[str | None, bool, bool]] = []

    def up(_self: Runtime, *, host_name: str | None = None, fresh: bool = False, no_stack: bool = False) -> None:
        calls.append((host_name, fresh, no_stack))

    monkeypatch.setattr(Runtime, "up", up)
    result = CliRunner().invoke(app, ["up", "--fresh", "--yes"])
    assert result.exit_code == 0
    assert calls == [(None, True, False)]


def test_cli_secures_state_and_cache_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.cli import app
    from offbox.paths import Paths

    isolate(tmp_path, monkeypatch)
    paths = Paths.discover()
    paths.state.mkdir(parents=True, mode=0o755)
    paths.state.chmod(0o755)
    assert CliRunner().invoke(app, ["version"]).exit_code == 0
    assert paths.state.stat().st_mode & 0o777 == 0o700
    assert paths.cache.stat().st_mode & 0o777 == 0o700


def test_exec_usage_shows_command_separator() -> None:
    from offbox.cli import app

    result = CliRunner().invoke(app, ["exec", "--help"], prog_name="offbox", terminal_width=200)
    assert result.exit_code == 0
    assert "Usage: offbox exec [OPTIONS] -- COMMAND [ARGS]..." in re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)


def test_every_cli_parameter_has_help() -> None:
    import typer.main
    from typer.core import TyperGroup

    from offbox.cli import app

    missing: list[str] = []

    def walk(command: object, prefix: str) -> None:
        for parameter in getattr(command, "params", []):
            if not getattr(parameter, "help", None):
                missing.append(f"{prefix} {parameter.name}")
        if isinstance(command, TyperGroup):
            for name, child in command.commands.items():
                walk(child, f"{prefix} {name}")

    walk(typer.main.get_command(app), "offbox")
    assert missing == []


def test_long_fix_lines_are_not_wrapped_on_non_tty_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    from offbox import console

    fix = "Run " + " ".join(f"step-{index}" for index in range(40))
    console.error("Failure", fix)
    assert capsys.readouterr().err == f"✗ Failure\n  Fix: {fix}\n"


@pytest.mark.subprocess
def test_cli_import_skips_template_and_download_modules() -> None:
    import subprocess
    import sys

    probe = "import sys, offbox.cli; print(sorted(m for m in ('jinja2', 'urllib.request', 'ssl') if m in sys.modules))"
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=30)
    assert result.stdout == "[]\n"
