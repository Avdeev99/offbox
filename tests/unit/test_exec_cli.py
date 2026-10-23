from pathlib import Path

import pytest
from typer.testing import CliRunner

from offbox.cli import app
from offbox.execute import ExecOptions, Executor


@pytest.mark.parametrize(("flags", "tty", "stdin"), [([], None, False), (["--tty"], True, False), (["--no-tty", "--stdin"], False, True)])
def test_cli_preserves_literal_command_argv_and_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str], tty: bool | None, stdin: bool
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    seen: list[ExecOptions] = []

    def execute(self: Executor, options: ExecOptions, *, cwd: Path | None = None) -> int:
        seen.append(options)
        return 42

    monkeypatch.setattr(Executor, "execute", execute)
    result = CliRunner().invoke(app, ["exec", *flags, "--env", "CUSTOM=a=b", "--", "tool", "--env", "literal", "", "a; true"])
    assert result.exit_code == 42
    assert result.stdout == "" and result.stderr == ""
    assert seen[0].command == ["tool", "--env", "literal", "", "a; true"]
    assert seen[0].tty is tty
    assert seen[0].stdin is stdin
    assert seen[0].overrides["CUSTOM"].get_secret_value() == "a=b"


@pytest.mark.parametrize(
    "args",
    [["exec"], ["exec", "--"], ["exec", "true"], ["exec", "--tty", "--no-tty", "--", "true"], ["exec", "--env", "INVALID", "--", "true"]],
)
def test_invalid_exec_usage_is_exit_two_with_no_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Fix:" in result.stderr
