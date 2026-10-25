from typing import Annotated

import typer
from pydantic import TypeAdapter
from typer._click import Context
from typer.core import TyperCommand

from offbox.errors import OffboxError, UsageError
from offbox.execute import ExecOptions, Executor, parse_env
from offbox.paths import Paths
from offbox.runner import SubprocessRunner


class ExecCommand(TyperCommand):
    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if "--" in args:
            index = args.index("--")
            ctx.meta["offbox_exec_command"] = tuple(args[index + 1 :])
            args = args[:index]
        return super().parse_args(ctx, args)

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        return [*super().collect_usage_pieces(ctx), "-- COMMAND [ARGS]..."]


def execute(
    ctx: typer.Context,
    no_sync: Annotated[bool, typer.Option("--no-sync", help="Skip the source sync flush before running.")] = False,
    tty: Annotated[bool, typer.Option("--tty", help="Allocate a remote TTY. Default: when stdin and stdout are terminals.")] = False,
    no_tty: Annotated[bool, typer.Option("--no-tty", help="Never allocate a remote TTY.")] = False,
    stdin: Annotated[bool, typer.Option("--stdin", help="Forward local stdin to a non-TTY command.")] = False,
    env: Annotated[list[str] | None, typer.Option("--env", help="Set NAME=value in the command environment; repeatable.")] = None,
) -> None:
    """Run a command after -- inside this worktree's environment."""
    if tty and no_tty:
        raise UsageError("--tty and --no-tty are mutually exclusive", "Choose one TTY mode.")
    command = TypeAdapter(list[str]).validate_python(ctx.meta.get("offbox_exec_command", ()))
    if not command:
        raise UsageError("A command is required after --", "Run offbox exec -- COMMAND [ARGS...].")
    runner: object = ctx.find_root().obj
    if not isinstance(runner, SubprocessRunner):
        raise OffboxError("Runner is unavailable", "Restart offbox.")
    options = ExecOptions(
        command=command, tty=True if tty else False if no_tty else None, no_sync=no_sync, stdin=stdin, overrides=parse_env(env or [])
    )
    raise typer.Exit(Executor(Paths.discover(), runner).execute(options))


def register(app: typer.Typer) -> None:
    app.command("exec", cls=ExecCommand)(execute)
