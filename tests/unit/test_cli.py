import pytest
from typer.testing import CliRunner


@pytest.mark.parametrize("arguments", [("--version",), ("version",)])
def test_version_writes_only_the_version_to_stdout(arguments: tuple[str, ...]) -> None:
    from offbox.cli import app

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0
    assert result.stdout == "0.1.0\n"
    assert result.stderr == ""
