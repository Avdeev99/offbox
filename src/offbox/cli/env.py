import sys
from typing import Annotated

import typer

from offbox import console
from offbox.errors import OffboxError, PreconditionError
from offbox.paths import Paths
from offbox.runner import SubprocessRunner
from offbox.runtime import Runtime


def _runtime(ctx: typer.Context) -> Runtime:
    value: object = ctx.find_root().obj
    if not isinstance(value, SubprocessRunner):
        raise OffboxError("Runner is unavailable", "Restart offbox.")
    return Runtime(Paths.discover(), value)


def _confirm(message: str, yes: bool, fix: str) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise PreconditionError("This operation requires --yes without a TTY", fix)
    if not typer.confirm(message):
        raise PreconditionError("Operation cancelled", "Run the command again when ready.")


def up(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option("--host", help="Host for a new environment. Default: the default host.")] = None,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="Recreate the container and delete its inner Docker data; source files are kept.")
    ] = False,
    no_stack: Annotated[bool, typer.Option("--no-stack", help="Skip starting the project's Compose stack.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm --fresh without a prompt.")] = False,
) -> None:
    """Create or start this worktree's isolated environment."""
    if fresh:
        _confirm(
            "Recreate this environment and delete its inner Docker data (images, volumes, containers)?",
            yes,
            "Run offbox up --fresh --yes.",
        )
    _runtime(ctx).up(host_name=host, fresh=fresh, no_stack=no_stack)


def stop(ctx: typer.Context) -> None:
    """Stop this environment while retaining its data."""
    _runtime(ctx).stop()


def rm(ctx: typer.Context, yes: Annotated[bool, typer.Option("--yes", help="Confirm without a prompt.")] = False) -> None:
    """Remove this worktree's remote environment and Docker data."""
    _confirm("Remove this worktree's remote environment and all its Docker data?", yes, "Run offbox rm --yes to confirm removal.")
    _runtime(ctx).remove()


def list_envs(ctx: typer.Context, json: Annotated[bool, typer.Option("--json", help="Emit a typed JSON report.")] = False) -> None:
    """List environments and their live container status."""
    report = _runtime(ctx).report()
    if json:
        console.json_out(report)
    elif not report.envs:
        console.info("No environments registered")
    else:
        for env in report.envs:
            used = f"  last used {env.last_used.astimezone().strftime('%Y-%m-%d %H:%M')}" if env.last_used else ""
            console.info(f"{env.name}  {env.host}  {env.status}  {env.worktree}{used}")
            if env.fix:
                console.warn(f"Fix: {env.fix}")


def shell(ctx: typer.Context) -> None:
    """Open an interactive shell inside this environment."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise PreconditionError("An environment shell requires a TTY", "Run offbox shell in an interactive terminal.")
    raise typer.Exit(_runtime(ctx).shell())


def gc(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan without changing anything.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Apply the plan without a prompt.")] = False,
) -> None:
    """Remove stale or orphan environments and prune unused inner Docker data."""
    runtime = _runtime(ctx)
    plan = runtime.gc_plan()
    for target in plan.targets:
        console.info(f"Remove {target.host}/{target.worktree.name}: {target.reason}")
    for target in plan.running:
        console.info(f"Prune unused Docker data in {target.host}/{target.worktree.name}")
    if dry_run:
        console.info("Dry run; no resources changed")
        return
    if not plan.targets and not plan.running:
        console.info("Nothing to clean up")
        return
    _confirm("Apply this garbage collection plan?", yes, "Run offbox gc --dry-run to review, then offbox gc --yes.")
    runtime.collect(plan)


def register(app: typer.Typer) -> None:
    app.command()(up)
    app.command()(stop)
    app.command()(rm)
    app.command("list")(list_envs)
    app.command()(shell)
    app.command()(gc)
