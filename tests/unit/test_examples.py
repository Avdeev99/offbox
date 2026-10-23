import shlex
from datetime import UTC, datetime
from pathlib import Path

import pytest

from offbox.config import ProjectConfig, load_config
from offbox.errors import CommandFailed
from offbox.execute import Executor
from offbox.naming import Worktree
from offbox.paths import Paths
from offbox.runner import FakeRunner, Script
from offbox.state import HostState

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.parametrize(
    ("name", "install", "lockfile", "stack", "ports"),
    [
        ("python-compose", "uv sync --frozen", "uv.lock", "docker compose up -d --build --wait", [8000, 5432]),
        ("node-playwright", "npm ci && npx playwright install chromium", "package-lock.json", "docker compose up -d --wait", [3000]),
    ],
)
@pytest.mark.parametrize("install_status", [0, 23])
def test_examples_install_before_stack_and_retain_failure(
    tmp_path: Path, name: str, install: str, lockfile: str, stack: str, ports: list[int], install_status: int
) -> None:
    root = EXAMPLES / name
    project = load_config(root / "offbox.toml", ProjectConfig)
    assert project.install.command == install
    assert project.install.when == [lockfile]
    assert (root / lockfile).is_file()
    assert project.stack.commands(root) == (stack, "docker compose down")
    assert project.ports.forward == ports
    assert project.ports.auto is True
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
        known_hosts_path=tmp_path / "known_hosts",
    )
    work = Worktree(root=root, name="example-abcd")
    scripts = [
        Script(prefix=["ssh"], stdout="a" * 64 + "  -\n"),
        Script(prefix=["ssh"], stdout="missing"),
        Script(prefix=["ssh"], method="start_stream", returncode=install_status),
    ]
    if install_status == 0:
        scripts.extend(
            [
                Script(prefix=["ssh"], stdout="b" * 64 + "  -\n"),
                Script(prefix=["ssh"]),
                Script(prefix=["ssh"], method="start_stream"),
            ]
        )
    runner = FakeRunner(scripts)
    executor = Executor(
        Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control"),
        runner,
        id_factory=lambda: "c" * 32,
    )
    if install_status:
        with pytest.raises(CommandFailed) as caught:
            executor.up_stages(work, host, project)
        assert caught.value.returncode == 23
        assert len(runner.calls) == 3
    else:
        executor.up_stages(work, host, project)
        assert len(runner.calls) == 6
        assert runner.calls[4].argv[-1].endswith("b" * 64)
    stages = [shlex.split(call.argv[-1]) for call in runner.calls if call.method == "start_stream"]
    assert [stage[:2] for stage in stages] == [["sh", "-c"]] * len(stages)
    assert [shlex.split(stage[-1])[-4:] for stage in stages] == [
        ["sh", "-c", command, ">&2"] for command in ([install] if install_status else [install, stack])
    ]
