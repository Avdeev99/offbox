import logging
from typing import Annotated

import typer
from typer._click import ClickException, Context
from typer.core import TyperGroup
from typer.exceptions import Abort, Exit

from offbox import __version__, console
from offbox.cli.env import register as register_env_commands
from offbox.cli.exec import register as register_exec_command
from offbox.cli.host import app as host_app
from offbox.doctor import diagnose
from offbox.errors import OffboxError
from offbox.logging import configure, redact, register_exec_secrets
from offbox.paths import Paths
from offbox.runner import SubprocessRunner

logger = logging.getLogger(__name__)


class ErrorGroup(TyperGroup):
    def make_context(self, info_name: str | None, args: list[str], parent: Context | None = None, **extra: object) -> Context:
        paths = Paths.discover()
        log_path = paths.log_file()
        try:
            register_exec_secrets(args)
            configure(paths, [info_name or "offbox", *args])
            return super().make_context(info_name, args, parent, **extra)
        except ClickException as exc:
            console.error(exc.format_message(), "Run offbox --help for valid arguments.")
            raise Exit(exc.exit_code) from exc
        except OSError as exc:
            console.error(f"Cannot initialize log file {log_path}", "Check XDG_STATE_HOME permissions and available disk space.")
            raise Exit(1) from exc

    def invoke(self, ctx: Context) -> object:
        log_path = Paths.discover().log_file()
        try:
            return super().invoke(ctx)
        except OffboxError as exc:
            logger.exception("offbox failure")
            console.error(redact(exc.message), redact(exc.fix) if exc.fix else "Check the log and retry.")
            raise Exit(exc.exit_code) from exc
        except ClickException as exc:
            console.error(exc.format_message(), "Run offbox --help for valid arguments.")
            raise Exit(exc.exit_code) from exc
        except (Exit, Abort):
            raise
        except Exception as exc:
            logger.exception("unexpected failure")
            console.error(f"unexpected error, see {log_path}", "Check the log and report this error.")
            raise Exit(1) from exc


app = typer.Typer(no_args_is_help=True, add_completion=False, cls=ErrorGroup)
app.add_typer(host_app, name="host")
register_env_commands(app)
register_exec_command(app)


def _version_callback(value: bool) -> None:
    if value:
        console.version(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True, help="Show the version and exit.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Echo commands with secrets redacted.")] = False,
) -> None:
    ctx.obj = SubprocessRunner(verbose=verbose)


@app.command()
def version() -> None:
    """Show the version and exit."""
    console.version(__version__)


@app.command()
def doctor(ctx: typer.Context, json: Annotated[bool, typer.Option("--json", help="Emit a typed JSON report.")] = False) -> None:
    """Check local tools, configuration and default host connectivity."""
    runner = ctx.obj
    if not isinstance(runner, SubprocessRunner):
        raise OffboxError("Runner is unavailable", "Restart offbox.")
    report = diagnose(Paths.discover(), runner)
    if json:
        console.json_out(report)
    else:
        for check in report.checks:
            if check.ok:
                console.success(check.message)
            else:
                console.error(check.message, check.fix)
    raise typer.Exit(report.exit_code)
