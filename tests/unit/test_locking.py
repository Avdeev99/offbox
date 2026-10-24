import fcntl
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from offbox.errors import PreconditionError
from offbox.host import host_operation
from offbox.naming import Worktree
from offbox.paths import Paths
from offbox.ports import PortManager
from offbox.runner import FakeRunner
from offbox.state import EnvState, HostState, State, StateStore


class ReleaseClock:
    def __init__(self, release: Callable[[], None] | None) -> None:
        self.current = 0.0
        self.release = release

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds
        if self.release is not None and self.current >= 0.1:
            self.release()
            self.release = None


@pytest.mark.parametrize("released", [True, False])
def test_command_lock_waits_boundedly_without_weakening_exclusion(tmp_path: Path, released: bool) -> None:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    lock = paths.state / "host-locks/main.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        clock = ReleaseClock((lambda: fcntl.flock(holder.fileno(), fcntl.LOCK_UN)) if released else None)
        waiting = host_operation(paths, "main", wait_timeout=1, clock=clock)
        if released:
            with waiting, lock.open("a") as contender, pytest.raises(BlockingIOError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert clock.current == pytest.approx(0.1)
        else:
            with pytest.raises(PreconditionError, match="after waiting"), waiting:
                pytest.fail("Contended lock was entered")
            assert clock.current == pytest.approx(1)


@pytest.mark.parametrize("released", [True, False])
def test_ports_cancel_waits_for_global_lock(tmp_path: Path, released: bool) -> None:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    work = Worktree(root=tmp_path / "work", name="work-abcd")
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=tmp_path / "known",
    )
    env = EnvState(name=work.name, host=host.name, worktree_path=work.root, created_at=datetime.now(UTC))
    StateStore(paths.state).write(State(hosts={host.name: host}, envs={work.name: env}))
    with (paths.state / "ports.lock").open("a") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        clock = ReleaseClock((lambda: fcntl.flock(holder.fileno(), fcntl.LOCK_UN)) if released else None)
        manager = PortManager(paths, FakeRunner(), clock=clock)
        if released:
            manager.cancel(work, host)
            assert clock.current == pytest.approx(0.1)
        else:
            with pytest.raises(PreconditionError, match="after waiting 30s"):
                manager.cancel(work, host)
            assert clock.current == pytest.approx(30)


def test_swap_never_waits_for_host_lock_while_holding_ports_lock(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    work = Worktree(root=tmp_path / "work", name="work-abcd")
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=tmp_path / "known",
    )
    env = EnvState(name=work.name, host=host.name, worktree_path=work.root, created_at=datetime.now(UTC), local_ports={8000: 8001})
    store = StateStore(paths.state)
    store.write(State(hosts={host.name: host}, envs={work.name: env}))
    clock = ReleaseClock(None)
    runner = FakeRunner()
    with host_operation(paths, host.name), pytest.raises(PreconditionError, match="already running"):
        PortManager(paths, runner, clock=clock).swap(work, 8000)
    assert clock.current == 0
    assert runner.calls == []
    assert store.read().envs[work.name] == env
