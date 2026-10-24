import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import Field, TypeAdapter
from typer._click import Context
from typer.core import TyperCommand

from offbox import console
from offbox.config import Model
from offbox.errors import OffboxError, PreconditionError
from offbox.host import select_host
from offbox.host.create import CreateOptions, create_host
from offbox.host.destroy import destroy_host
from offbox.host.init import InitOptions, init_host
from offbox.paths import Paths
from offbox.providers import validate_name
from offbox.runner import Runner, SubprocessRunner
from offbox.ssh import ssh_argv
from offbox.state import HostState, StateStore

app = typer.Typer(no_args_is_help=True, help="Create, register and manage hosts.")


class HostSummary(Model):
    name: str
    provider: str
    ip: str
    envs: int
    reachable: bool
    phase: Literal["creating", "initializing", "ready"]
    default: bool


class HostReport(Model):
    hosts: list[HostSummary] = Field(default_factory=list[HostSummary])


def host_report(paths: Paths, runner: Runner) -> HostReport:
    state = StateStore(paths.state).read()

    def describe(host: HostState) -> HostSummary:
        reachable = False
        if host.address:
            try:
                reachable = (
                    runner.run(ssh_argv(host.name, ["true"], options=["-o", "ConnectTimeout=3"]), check=False, timeout=3).returncode == 0
                )
            except OffboxError:
                reachable = False
        return HostSummary(
            name=host.name,
            provider=host.provider,
            ip=host.address,
            envs=sum(env.host == host.name for env in state.envs.values()),
            reachable=reachable,
            phase=host.phase,
            default=state.default_host == host.name,
        )

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(state.hosts)))) as executor:
        return HostReport(hosts=list(executor.map(describe, [host for _, host in sorted(state.hosts.items())])))


def use_host(paths: Paths, name: str) -> None:
    validate_name(name)
    with StateStore(paths.state).transaction() as state:
        if name not in state.hosts or state.hosts[name].phase != "ready":
            raise PreconditionError(f"Host {name} is not ready or registered", "Create or initialise the host before selecting it.")
        state.default_host = name
    console.success(f"Default host: {name}")


def _runner(ctx: typer.Context) -> SubprocessRunner:
    value: object = ctx.find_root().obj
    if not isinstance(value, SubprocessRunner):
        raise OffboxError("Runner is unavailable", "Restart offbox.")
    return value


@app.command()
def create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument()] = "main",
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    size: Annotated[str | None, typer.Option("--size")] = None,
    region: Annotated[str | None, typer.Option("--region")] = None,
    ssh_key: Annotated[Path | None, typer.Option("--ssh-key")] = None,
) -> None:
    """Provision a VM and wait for its bootstrap to complete."""
    create_host(Paths.discover(), _runner(ctx), CreateOptions(name=name, provider=provider, size=size, region=region, ssh_key=ssh_key))


@app.command()
def init(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="USER@ADDRESS")],
    name: Annotated[str, typer.Option("--name")] = "main",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 22,
    accept_host_key: Annotated[bool, typer.Option("--accept-host-key")] = False,
    ssh_key: Annotated[Path | None, typer.Option("--ssh-key")] = None,
) -> None:
    """Bootstrap an existing server while preserving its owner's access."""
    init_host(
        Paths.discover(),
        _runner(ctx),
        InitOptions(target=target, name=name, port=port, accept_host_key=accept_host_key, ssh_key=ssh_key),
        confirm=typer.confirm if sys.stdin.isatty() else None,
    )


@app.command("list")
def list_hosts(ctx: typer.Context, json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List registered hosts and their reachability."""
    report = host_report(Paths.discover(), _runner(ctx))
    if json:
        console.json_out(report)
    elif not report.hosts:
        console.info("No hosts registered")
    else:
        for host in report.hosts:
            console.info(
                f"{host.name}  {host.provider}  {host.ip or 'pending'}  envs={host.envs}  reachable={'yes' if host.reachable else 'no'}  {host.phase}"
            )


@app.command()
def use(name: Annotated[str, typer.Argument()]) -> None:
    """Choose the default host."""
    use_host(Paths.discover(), name)


@app.command()
def destroy(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument()] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
    forget: Annotated[
        bool, typer.Option("--forget", help="For an unreachable SSH host: remove only the local record and leave the server untouched.")
    ] = False,
) -> None:
    """Destroy a host and its environments."""
    paths = Paths.discover()
    host = select_host(paths, name)
    envs = [env.name for env in StateStore(paths.state).read().envs.values() if env.host == host.name]
    action = "Forgetting" if forget else "Destroying"
    console.warn(f"{action} {host.name} loses environments: {', '.join(envs) if envs else '(none)'}")
    if not yes:
        if not sys.stdin.isatty():
            flags = " --forget" if forget else ""
            raise PreconditionError("Destruction requires --yes without a TTY", f"Run offbox host destroy {host.name}{flags} --yes.")
        if not typer.confirm(f"{'Forget' if forget else 'Destroy'} host {host.name}?"):
            raise PreconditionError("Destruction cancelled", "Run the command again when ready.")
    destroy_host(paths, _runner(ctx), host.name, forget=forget)


class HostSshCommand(TyperCommand):
    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if "--" in args:
            index = args.index("--")
            ctx.meta["offbox_remote_command"] = tuple(args[index + 1 :])
            args = args[:index]
        return super().parse_args(ctx, args)


@app.command(cls=HostSshCommand)
def ssh(ctx: typer.Context, name: Annotated[str | None, typer.Argument()] = None) -> None:
    """Open a host shell or run a command after --."""
    host = select_host(Paths.discover(), name)
    if not host.address:
        raise PreconditionError("Host has no recorded address", f"Run offbox host destroy {host.name} to clean up the pending host.")
    raw: object = ctx.meta.get("offbox_remote_command", ())
    command = TypeAdapter(list[str]).validate_python(raw)
    tty = not command
    if tty and not sys.stdin.isatty():
        raise PreconditionError("Interactive SSH requires a TTY", "Pass a command after -- for noninteractive SSH.")
    code = _runner(ctx).stream(ssh_argv(host.name, command, options=["-t"] if tty else []), stdin=tty, tty=tty)
    raise typer.Exit(code)
