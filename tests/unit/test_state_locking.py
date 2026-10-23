from datetime import UTC, datetime
from multiprocessing import get_context
from multiprocessing.synchronize import Event
from pathlib import Path

from offbox.state import HostState, StateStore


def _write_host(directory: Path, name: str, attempted: Event, acquired: Event, release: Event | None) -> None:
    attempted.set()
    with StateStore(directory).transaction() as state:
        acquired.set()
        if release is not None and not release.wait(15):
            raise AssertionError("The parent did not release the first transaction")
        state.hosts[name] = HostState(
            name=name,
            provider="ssh",
            address=f"{name}.example.test",
            ssh_user="offbox",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            known_hosts_path=directory / name / "known_hosts",
        )


def test_state_transactions_lock_across_processes_and_preserve_both_updates(tmp_path: Path) -> None:
    context = get_context("spawn")
    first_attempted = context.Event()
    first_acquired = context.Event()
    second_attempted = context.Event()
    second_acquired = context.Event()
    release = context.Event()
    first = context.Process(target=_write_host, args=(tmp_path, "first", first_attempted, first_acquired, release))
    second = context.Process(target=_write_host, args=(tmp_path, "second", second_attempted, second_acquired, None))
    first.start()
    try:
        assert first_acquired.wait(15), "The first writer never acquired the state lock"
        second.start()
        assert second_attempted.wait(15), "The second writer never attempted a transaction"
        assert not second_acquired.wait(0.25), "The second writer entered while the first still held the exclusive lock"
        release.set()
        first.join(15)
        second.join(15)
        assert first.exitcode == 0
        assert second.exitcode == 0
        assert second_acquired.is_set()
        state = StateStore(tmp_path).read()
        assert {name: host.address for name, host in state.hosts.items()} == {
            "first": "first.example.test",
            "second": "second.example.test",
        }
        assert state.version == 1
    finally:
        release.set()
        for process in (first, second):
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(15)
