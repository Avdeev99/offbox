from pathlib import Path
from typing import Annotated, Literal

import typer

from offbox import console
from offbox.errors import OffboxError, UsageError
from offbox.logs import follow_lines, stack_logs
from offbox.naming import find_worktree
from offbox.paths import Paths
from offbox.runner import SubprocessRunner
from offbox.status import Status
from offbox.uninstall import uninstall as run_uninstall


def _runner(ctx: typer.Context) -> SubprocessRunner:
    runner: object = ctx.find_root().obj
    if not isinstance(runner, SubprocessRunner):
        raise OffboxError("Runner is unavailable", "Restart offbox.")
    return runner


def status(ctx: typer.Context, json: Annotated[bool, typer.Option("--json", help="Emit a typed JSON report.")] = False) -> None:
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
    service: Annotated[str | None, typer.Argument(help="Compose service to show; requires --stack.")] = None,
    follow: Annotated[bool, typer.Option("--follow", help="Keep streaming new lines.")] = False,
    stack: Annotated[bool, typer.Option("--stack", help="Show the project's Compose stack logs instead of offbox's log.")] = False,
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
    provider: Annotated[
        Literal["hetzner", "gcp", "ssh"] | None, typer.Option("--provider", help="Host provider: hetzner, gcp or ssh (existing server).")
    ] = None,
    action: Annotated[
        Literal["check", "create", "init"] | None,
        typer.Option("--action", help="check prerequisites only, create a cloud host, or init an existing server."),
    ] = None,
    name: Annotated[str, typer.Option("--name", help="Host name to create or register.")] = "main",
    ssh_key: Annotated[
        Path | None, typer.Option("--ssh-key", help="Private SSH key. Default: ~/.ssh/id_ed25519, else ~/.ssh/offbox_ed25519.")
    ] = None,
    size: Annotated[str | None, typer.Option("--size", help="Cloud machine type for --action create.")] = None,
    region: Annotated[str | None, typer.Option("--region", help="Cloud location or zone for --action create.")] = None,
    target: Annotated[str | None, typer.Option("--target", help="USER@ADDRESS of the existing server for --action init.")] = None,
    port: Annotated[int, typer.Option("--port", min=1, max=65535, help="SSH port of the existing server.")] = 22,
    host_key_fingerprint: Annotated[
        str | None, typer.Option("--host-key-fingerprint", help="Expected SHA256:... host key fingerprint for --action init.")
    ] = None,
    accept_host_key: Annotated[
        bool, typer.Option("--accept-host-key", help="Trust the scanned host key without verification; prefer --host-key-fingerprint.")
    ] = False,
) -> None:
    """Check prerequisites, install missing tools and explicitly create or register a host."""
    from offbox.setup import SetupOptions
    from offbox.setup import setup as run_setup

    def ask(message: str, default: str) -> str:
        return str(typer.prompt(message, default=default))

    interactive = console.interactive()
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
            host_key_fingerprint=host_key_fingerprint,
        ),
        ask=ask if interactive else None,
        confirm=typer.confirm if interactive else None,
        offer=console.confirm if interactive else None,
    )


def uninstall(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm without a prompt.")] = False,
    abandon_cloud_hosts: Annotated[
        bool, typer.Option("--abandon-cloud-hosts", help="Remove local records even though cloud hosts keep running and billing.")
    ] = False,
) -> None:
    """Remove local offbox resources while retaining user configuration and remote hosts."""
    run_uninstall(Paths.discover(), _runner(ctx), yes=yes, abandon_cloud_hosts=abandon_cloud_hosts)


def register(app: typer.Typer) -> None:
    app.command()(status)
    app.command()(logs)
    app.command()(setup)
    app.command()(uninstall)
