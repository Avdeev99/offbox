import json as jsonlib
from typing import Annotated

import typer

from offbox import console
from offbox.errors import OffboxError
from offbox.naming import find_worktree
from offbox.paths import Paths
from offbox.project import report
from offbox.runner import SubprocessRunner


def config(ctx: typer.Context, json: Annotated[bool, typer.Option("--json", help="Emit a typed JSON report.")] = False) -> None:
    """Show this worktree's effective settings, where each comes from and its OFFBOX_* variable."""
    runner: object = ctx.find_root().obj
    if not isinstance(runner, SubprocessRunner):
        raise OffboxError("Runner is unavailable", "Restart offbox.")
    paths = Paths.discover()
    result = report(paths, find_worktree(runner).root)
    if json:
        console.json_out(result)
        return
    files = result.files
    console.output("Sources, later wins: default < user < project < local < env")
    for name, path, suffix in (("user", files.user, " [project]"), ("project", files.project, ""), ("local", files.local, "")):
        console.output(f"  {name:<8} {path}{suffix}{'' if path.is_file() else ' (not present)'}")
    console.output("")
    rows = [
        (setting.key, "unset" if setting.value is None else jsonlib.dumps(setting.value), setting.source, setting.env)
        for setting in result.settings
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(3)]
    for key, value, source, env in rows:
        console.output(f"{key:<{widths[0]}}  {value:<{widths[1]}}  {source:<{widths[2]}}  {env}")


def register(app: typer.Typer) -> None:
    app.command()(config)
