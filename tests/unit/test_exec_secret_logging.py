from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from offbox.cli import ErrorGroup
from offbox.logging import redact
from offbox.paths import Paths


@pytest.fixture
def parsing_app() -> typer.Typer:
    app = typer.Typer(cls=ErrorGroup)

    @app.callback()
    def root() -> None:
        pass

    @app.command()
    def version() -> None:
        pass

    return app


@pytest.mark.parametrize(
    ("arguments", "secret"),
    [
        (["exec", "--env", "PASSWORD=parse-password-one-z97", "--", "true"], "parse-password-one-z97"),
        (["-v", "exec", "--env=API_TOKEN=equal-token-two-k83", "--", "true"], "equal-token-two-k83"),
        (["exec", "--env", "PASSWORD=bad-option-secret-three-q65", "--unknown-option"], "bad-option-secret-three-q65"),
    ],
)
def test_explicit_exec_secret_is_redacted_before_initial_invocation_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str], secret: str, parsing_app: typer.Typer
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = CliRunner().invoke(parsing_app, arguments)
    assert result.exit_code == 2
    text = Paths.discover().log_file().read_text()
    assert "invocation argv=" in text
    assert secret not in text
    assert secret not in result.stderr
    assert "[REDACTED]" in text


def test_prescan_preserves_repeated_values_equals_and_command_separator() -> None:
    from offbox.logging import register_exec_secrets

    register_exec_secrets(
        [
            "exec",
            "--env",
            "PASSWORD=first-secret-4321",
            "--env=PASSWORD=second-secret=a=b-4321",
            "--",
            "echo",
            "--env=API_KEY=command-literal-4321",
        ]
    )
    assert redact("first-secret-4321 second-secret=a=b-4321", {}) == "[REDACTED] [REDACTED]"
    assert redact("command-literal-4321", {}) == "command-literal-4321"


def test_prescan_does_not_parse_other_commands_or_incomplete_options() -> None:
    from offbox.logging import register_exec_secrets

    register_exec_secrets(["host", "ssh", "--", "exec", "--env", "API_KEY=remote-command-87654"])
    register_exec_secrets(["exec", "--env"])
    register_exec_secrets(["exec", "--env", "--"])
    assert redact("remote-command-87654", {}) == "remote-command-87654"
