import sys
from pathlib import Path
from typing import Annotated, Literal

import typer

from offbox import console
from offbox.errors import OffboxError, UsageError
from offbox.logs import follow_lines, stack_logs
from offbox.naming import find_worktree
from offbox.paths import Paths
from offbox.runner import SubprocessRunner
from offbox.setup import SetupOptions
from offbox.setup import setup as run_setup
from offbox.status import Status
from offbox.uninstall import uninstall as run_uninstall


def _runner(ctx: typer.Context) -> SubprocessRunner:
    runner: object = ctx.find_root().obj
    if not isinstance(runner, SubprocessRunner):
        raise OffboxError("Runner is unavailable", "Restart offbox.")
    return runner


def status(ctx: typer.Context, json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check this worktree's host, environment, sync, ports and stack."""
    report = Status(Paths.discover(), _runner(ctx)).diagnose()
    if json:
        console.json_out(report)
    else:
        for check in report.checks:
            if check.level == "ok":
                console.success(check.message)
            elif check.level == "warning":
                console.warn(check.message)
                console.info(f"  Fix: {check.fix}")
            else:
                console.error(check.message, check.fix)
            if check.name == "ports":
                for forward in report.ports.forwards:
                    state = "active" if forward.active else "inactive"
                    console.info(f"  localhost:{forward.local} → {forward.env}:{forward.remote} ({state})")
    raise typer.Exit(report.exit_code)


def logs(
    ctx: typer.Context,
    service: Annotated[str | None, typer.Argument()] = None,
    follow: Annotated[bool, typer.Option("--follow")] = False,
    stack: Annotated[bool, typer.Option("--stack")] = False,
) -> None:
    """Read today's environment log or stream Compose logs with --stack [SERVICE]."""
    if service is not None and not stack:
        raise UsageError("A service requires --stack", "Run offbox logs --stack SERVICE.")
    runner, paths = _runner(ctx), Paths.discover()
    try:
        if stack:
            raise typer.Exit(stack_logs(paths, runner, follow=follow, service=service))
        work = find_worktree(runner)
        for line in follow_lines(paths, work.name, follow=follow):
            console.text(line)
    except KeyboardInterrupt as exc:
        raise typer.Exit(130) from exc


def setup(
    ctx: typer.Context,
    provider: Annotated[Literal["hetzner", "ssh"] | None, typer.Option("--provider")] = None,
    action: Annotated[Literal["check", "create", "init"] | None, typer.Option("--action")] = None,
    name: Annotated[str, typer.Option("--name")] = "main",
    ssh_key: Annotated[Path | None, typer.Option("--ssh-key")] = None,
    size: Annotated[str | None, typer.Option("--size")] = None,
    region: Annotated[str | None, typer.Option("--region")] = None,
    target: Annotated[str | None, typer.Option("--target")] = None,
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 22,
    accept_host_key: Annotated[bool, typer.Option("--accept-host-key")] = False,
) -> None:
    """Check prerequisites and explicitly create or register a host."""

    def ask(message: str, default: str) -> str:
        return str(typer.prompt(message, default=default))

    interactive = sys.stdin.isatty()
    run_setup(
        Paths.discover(),
        _runner(ctx),
        SetupOptions(
            provider=provider,
            action=action,
            name=name,
            ssh_key=ssh_key,
            size=size,
            region=region,
            target=target,
            port=port,
            accept_host_key=accept_host_key,
        ),
        ask=ask if interactive else None,
        confirm=typer.confirm if interactive else None,
    )


def uninstall(ctx: typer.Context, yes: Annotated[bool, typer.Option("--yes")] = False) -> None:
    """Remove local offbox resources while retaining user configuration and remote hosts."""
    run_uninstall(Paths.discover(), _runner(ctx), yes=yes)


def register(app: typer.Typer) -> None:
    app.command()(status)
    app.command()(logs)
    app.command()(setup)
    app.command()(uninstall)
