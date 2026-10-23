from typing import Annotated

import typer

from offbox import console
from offbox.config import ProjectConfig, load_config
from offbox.errors import OffboxError
from offbox.host import host_operation
from offbox.paths import Paths
from offbox.runner import SubprocessRunner
from offbox.runtime import Runtime


def ports(
    ctx: typer.Context,
    json: Annotated[bool, typer.Option("--json")] = False,
    swap: Annotated[int | None, typer.Option("--swap", min=1, max=65535)] = None,
) -> None:
    """Restore this worktree's port forwards and show its localhost mappings."""
    runner: object = ctx.find_root().obj
    if not isinstance(runner, SubprocessRunner):
        raise OffboxError("Runner is unavailable", "Restart offbox.")
    paths = Paths.discover()
    runtime = Runtime(paths, runner)
    work, _env, host = runtime.current()
    config = load_config(work.root / "offbox.toml", ProjectConfig)
    with host_operation(paths, host.name):
        report = runtime.ports.restore(work, host, config.ports)
    if swap is not None:
        report = runtime.ports.swap(work, swap)
    if json:
        console.json_out(report)
    else:
        for forward in report.forwards:
            console.output(f"localhost:{forward.local} → {forward.env}:{forward.remote}")


def register(app: typer.Typer) -> None:
    app.command()(ports)
