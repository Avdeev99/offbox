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
