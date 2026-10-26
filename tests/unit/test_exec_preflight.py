import fcntl
import json
import os
import shlex
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.errors import PreconditionError
from offbox.execute import INSTALL_PROBE_SCRIPT, PREFLIGHT_SCRIPT, ExecOptions, Executor, install_marker
from offbox.naming import worktree_name
from offbox.paths import Paths
from offbox.runner import Result, StreamProcess
from offbox.state import EnvState, HostState, State, StateStore

DIGEST = "a" * 64


class StrictClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class LockedRunner(FakeRunner):
    """FakeRunner shared between exec's main thread and its port-restore thread."""

    def __init__(self, scripts: Sequence[Script] = ()) -> None:
        super().__init__(scripts)
        self.lock = threading.Lock()

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
        interruptible: bool = False,
    ) -> Result:
        with self.lock:
            return super().run(
                argv, check=check, capture=capture, input=input, env=env, cwd=cwd, timeout=timeout, interruptible=interruptible
            )

    def stream(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdin: bool = False, tty: bool = False) -> int:
        with self.lock:
            return super().stream(argv, env=env, stdin=stdin, tty=tty)

    def start_stream(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: bool = False,
        tty: bool = False,
        keepalive: bool = False,
    ) -> StreamProcess:
        with self.lock:
            return super().start_stream(argv, env=env, stdin=stdin, tty=tty, keepalive=keepalive)


def setup(root: Path, config: str = "", *, ports: dict[int, int] | None = None) -> tuple[Paths, Path, str]:
    paths = Paths(root / "config", root / "state", root / "cache", root / "control")
    workroot = root / "project"
    workroot.mkdir()
    (workroot / "offbox.toml").write_text(config)
    name = worktree_name(workroot)
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime(2026, 10, 24, tzinfo=UTC),
        known_hosts_path=root / "known_hosts",
        control_master_pid=200,
    )
    env = EnvState(
        name=name,
        host="main",
        worktree_path=workroot,
        created_at=datetime(2026, 10, 24, tzinfo=UTC),
        local_ports=ports or {},
        forward_ip=IPv4Address("10.89.0.2") if ports else None,
    )
    StateStore(paths.state).write(State(hosts={"main": host}, default_host="main", envs={name: env}))
    return paths, workroot, name


def preflight(root: Path, name: str, *, install: str = "", compose: str = "", compose_failed: bool = False) -> str:
    container = {
        "Name": f"/offbox-{name}",
        "Image": "sha256:" + "f" * 64,
        "State": {"Running": True},
        "Config": {"Labels": {"offbox.env": name, "offbox.worktree": str(root)}},
        "NetworkSettings": {"Networks": {"offbox": {"IPAddress": "10.89.0.2"}}},
    }
    return json.dumps({"containers": [container], "install": install, "compose": compose, "compose_failed": compose_failed})


def master(pid: int = 200, *, stderr: str | None = None, returncode: int = 0) -> Script:
    return Script(
        prefix=["ssh", "-O", "check"], returncode=returncode, stderr=stderr if stderr is not None else f"Master running (pid={pid})"
    )


def test_busy_host_lock_skips_port_restore_without_waiting_and_runs_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths, root, name = setup(tmp_path, "[ports]\nforward=[8000]\nauto=false\n", ports={8000: 8000})
    (paths.state / "host-locks").mkdir()
    clock = StrictClock()
    runner = LockedRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh", "offbox-main"], stdout=preflight(root, name)),
            Script(prefix=["ssh", "offbox-main"], method="start_stream", returncode=7),
        ]
    )
    with (paths.state / "host-locks" / "main.lock").open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        status = Executor(paths, runner, clock=clock).execute(ExecOptions(command=["pytest"], no_sync=True), cwd=root)
    assert status == 7
    assert clock.sleeps == []
    assert [call.argv[:3] for call in runner.calls if call.argv[:2] == ["ssh", "-O"]] == []
    assert "Port forwards" not in capsys.readouterr().err


def test_port_restore_failure_warns_and_never_changes_the_command_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths, root, name = setup(tmp_path, "[ports]\nforward=[8000]\nauto=false\n", ports={8000: 8000})
    runner = LockedRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh", "offbox-main"], stdout=preflight(root, name)),
            Script(prefix=["ssh", "offbox-main"], method="start_stream", returncode=3),
            master(returncode=255, stderr="permission denied on control socket"),
        ]
    )
    assert Executor(paths, runner).execute(ExecOptions(command=["pytest"], no_sync=True), cwd=root) == 3
    error = " ".join(capsys.readouterr().err.split())
    assert "Port forwards were not restored: Cannot check SSH master for main" in error
    assert StateStore(paths.state).read().envs[name].local_ports == {8000: 8000}


def test_tty_port_warnings_wait_until_the_command_exits(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths, root, name = setup(tmp_path, "[ports]\nforward=[8000]\nauto=false\n", ports={8000: 8000})
    restored = threading.Event()
    during: list[str] = []

    class TtyRunner(LockedRunner):
        def run(
            self,
            argv: Sequence[str],
            *,
            check: bool = True,
            capture: bool = True,
            input: str | None = None,
            env: Mapping[str, str] | None = None,
            cwd: Path | None = None,
            timeout: float | None = None,
            interruptible: bool = False,
        ) -> Result:
            try:
                return super().run(
                    argv, check=check, capture=capture, input=input, env=env, cwd=cwd, timeout=timeout, interruptible=interruptible
                )
            finally:
                if argv[:3] == ["ssh", "-O", "check"]:
                    restored.set()

        def stream(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdin: bool = False, tty: bool = False) -> int:
            assert restored.wait(5)
            during.append(capsys.readouterr().err)
            return super().stream(argv, env=env, stdin=stdin, tty=tty)

    runner = TtyRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh", "offbox-main"], stdout=preflight(root, name)),
            master(returncode=255, stderr="permission denied on control socket"),
            Script(prefix=["ssh", "-tt"], method="stream", returncode=5),
        ]
    )
    assert Executor(paths, runner).execute(ExecOptions(command=["bash"], no_sync=True, tty=True), cwd=root) == 5
    assert during == [""]
    assert "Port forwards were not restored" in capsys.readouterr().err


def test_one_preflight_round_trip_carries_ownership_install_probe_and_compose_ports(tmp_path: Path) -> None:
    paths, root, name = setup(tmp_path, '[install]\ncommand="uv sync"\nwhen=["uv.lock"]\n')
    (root / "compose.yaml").write_text("services: {}\n")
    compose = json.dumps([{"Publishers": [{"PublishedPort": 5432, "Protocol": "tcp"}]}])
    runner = LockedRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(
                prefix=["ssh", "offbox-main"],
                stdout=preflight(root, name, install=f"{DIGEST}  -\n{install_marker(DIGEST, 'uv sync')}", compose=compose),
            ),
            Script(prefix=["ssh", "offbox-main"], method="start_stream"),
            master(),
            Script(prefix=["ssh", "-O", "forward", "-L", "127.0.0.1:5432:10.89.0.2:5432"]),
        ]
    )
    assert Executor(paths, runner).execute(ExecOptions(command=["true"], no_sync=True), cwd=root) == 0
    remote = [call for call in runner.calls if call.argv[:2] == ["ssh", "offbox-main"] and call.method == "run"]
    assert len(remote) == 1
    assert remote[0].input == PREFLIGHT_SCRIPT
    assert (
        shlex.split(remote[0].argv[-1])[:12]
        == [
            "sh",
            "-s",
            "--",
            name,
            str(root),
            f"offbox-{name}",
            "1",
            "1",
            f"/srv/offbox/envs/{name}/work",
            f"/srv/offbox/envs/{name}",
            "uv.lock",
        ][:12]
    )
    assert [call.method for call in runner.calls] == ["run", "run", "start_stream", "run", "run"]
    assert StateStore(paths.state).read().envs[name].active_forwards == [5432]


def test_preflight_script_runs_compose_and_probe_only_for_an_owned_running_container() -> None:
    gate = PREFLIGHT_SCRIPT.index(".[0].State.Running == true")
    assert PREFLIGHT_SCRIPT.index('hashed=$(offbox_hash "$work" "$@")\n        marker=$(offbox_marker "$directory")') > gate
    assert PREFLIGHT_SCRIPT.index("docker compose ps --format json") > gate
    assert "no such container" in PREFLIGHT_SCRIPT
    assert "compose_failed=true" in PREFLIGHT_SCRIPT


def test_failed_compose_discovery_falls_back_to_port_restore_and_still_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths, root, name = setup(tmp_path)
    (root / "compose.yaml").write_text("services: {}\n")
    runner = LockedRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(
                prefix=["ssh", "offbox-main", "--", shlex.join(["sh", "-s", "--", name, str(root), f"offbox-{name}"])],
                returncode=1,
                stderr="compose.yaml: invalid",
            ),
            Script(prefix=["ssh", "offbox-main"], stdout=preflight(root, name, compose_failed=True)),
            Script(prefix=["ssh", "offbox-main"], method="start_stream"),
        ]
    )
    assert Executor(paths, runner).execute(ExecOptions(command=["true"], no_sync=True), cwd=root) == 0
    assert "Port forwards were not restored" in capsys.readouterr().err


def test_parallel_exec_waits_for_a_running_install_then_reprobes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths, root, name = setup(tmp_path, '[install]\ncommand="npm ci"\nwhen=["lock"]\n')
    directory = paths.state / "install-locks"
    directory.mkdir(parents=True, mode=0o700)
    held = os.open(directory / name, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)

    class ReleasingClock(StrictClock):
        def sleep(self, seconds: float) -> None:
            super().sleep(seconds)
            if len(self.sleeps) == 3:
                os.close(held)

    clock = ReleasingClock()
    runner = LockedRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh", "offbox-main"], stdout=preflight(root, name, install=f"{DIGEST}  -\nmissing")),
            Script(prefix=["ssh", "offbox-main"], stdout=f"{DIGEST}  -\n{install_marker(DIGEST, 'npm ci')}\n"),
            Script(prefix=["ssh", "offbox-main"], method="start_stream"),
        ]
    )
    assert Executor(paths, runner, clock=clock).execute(ExecOptions(command=["pytest"], no_sync=True), cwd=root) == 0
    assert runner.calls[2].input == INSTALL_PROBE_SCRIPT
    assert not any("npm ci" in call.argv[-1] for call in runner.calls)
    assert len(clock.sleeps) == 3
    assert "Waiting for another installation" in capsys.readouterr().err


def test_install_lock_wait_is_bounded_and_never_starts_the_command(tmp_path: Path) -> None:
    paths, root, name = setup(tmp_path, '[install]\ncommand="npm ci"\nwhen=["lock"]\n')
    directory = paths.state / "install-locks"
    directory.mkdir(parents=True, mode=0o700)
    held = os.open(directory / name, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    clock = StrictClock()
    runner = LockedRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh", "offbox-main"], stdout=preflight(root, name, install=f"{DIGEST}  -\nmissing")),
        ]
    )
    try:
        with pytest.raises(PreconditionError, match="Installation is still running for this environment after waiting 600s"):
            Executor(paths, runner, clock=clock).execute(ExecOptions(command=["pytest"], no_sync=True), cwd=root)
    finally:
        os.close(held)
    assert clock.value == pytest.approx(600)
    assert all(call.method == "run" for call in runner.calls)


def test_uncontended_install_uses_the_preflight_probe_without_reprobing(tmp_path: Path) -> None:
    paths, root, name = setup(tmp_path, '[install]\ncommand="npm ci"\nwhen=["lock"]\n')
    runner = LockedRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh", "offbox-main"], stdout=preflight(root, name, install=f"{DIGEST}  -\nmissing")),
            Script(prefix=["ssh", "offbox-main"], method="start_stream"),
            Script(prefix=["ssh", "offbox-main"], stdout="b" * 64 + "  -\n"),
            Script(prefix=["ssh", "offbox-main"]),
            Script(prefix=["ssh", "offbox-main"], method="start_stream", returncode=4),
        ]
    )
    assert Executor(paths, runner).execute(ExecOptions(command=["pytest"], no_sync=True), cwd=root) == 4
    assert "npm ci" in runner.calls[2].argv[-1]
    assert runner.calls[4].argv[-1].endswith(install_marker("b" * 64, "npm ci"))
    assert "pytest" in runner.calls[5].argv[-1]


def test_interrupted_exec_waits_only_briefly_for_an_unfinished_port_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from offbox import execute

    paths, root, name = setup(tmp_path)
    release = threading.Event()
    joins: list[float | None] = []

    class RecordingThread(threading.Thread):
        def join(self, timeout: float | None = None) -> None:
            joins.append(timeout)

    executor = Executor(
        paths, FakeRunner([Script(prefix=["git"], stdout=str(root)), Script(prefix=["ssh", "offbox-main"], stdout=preflight(root, name))])
    )

    def run(*_args: object, started: Callable[[], None] | None = None, **_kwargs: object) -> int:
        assert started is not None
        started()
        executor.interrupted = True
        return 130

    def restore_ports(*_args: object) -> list[str]:
        assert release.wait(5)
        return []

    monkeypatch.setattr(executor, "run", run)
    monkeypatch.setattr(executor, "restore_ports", restore_ports)
    monkeypatch.setattr(threading, "Thread", RecordingThread)
    try:
        assert executor.execute(ExecOptions(command=["pytest"], no_sync=True), cwd=root) == 130
    finally:
        release.set()
    assert joins == [execute.INTERRUPTED_PORT_RESTORE_JOIN_SECONDS]
    assert "Port forwards are still being restored" in capsys.readouterr().err


@pytest.mark.subprocess
def test_preflight_script_stops_when_the_install_probe_cannot_enter_the_worktree(tmp_path: Path) -> None:
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("#!/bin/sh\nprintf '[]\\n'\n")
    (bin_dir / "jq").write_text("#!/bin/sh\nprintf '{}\\n'\n")
    for tool in ("docker", "jq"):
        (bin_dir / tool).chmod(0o755)
    missing = tmp_path / "missing" / "work"
    result = subprocess.run(
        ["sh", "-s", "--", "env", str(tmp_path), "offbox-env", "1", "0", str(missing), str(tmp_path / "missing"), "lock"],
        input=PREFLIGHT_SCRIPT,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        timeout=30,
    )
    assert (result.returncode, result.stdout) == (3, "")
