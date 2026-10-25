from pathlib import Path

import pytest

from offbox.errors import PreconditionError
from offbox.state import State, StateStore


def test_each_write_keeps_the_previous_state_as_private_backup(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.write(State(default_host="first"))
    assert not store.backup_path.exists()
    with store.transaction() as state:
        state.default_host = "second"
    assert State.model_validate_json(store.backup_path.read_text()).default_host == "first"
    assert store.read().default_host == "second"
    assert store.backup_path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".state-*")) == []


def test_unreadable_state_names_backup_and_manual_tofu_destroy_per_workspace(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.write(State(default_host="first"))
    store.write(State(default_host="second"))
    for name in ("beta", "alpha"):
        (tmp_path / "hosts" / name / "tofu").mkdir(parents=True)
    (tmp_path / "hosts/existing/provider").mkdir(parents=True)
    store.path.write_text("{broken")
    with pytest.raises(PreconditionError) as caught:
        store.read()
    assert caught.value.fix == (
        f"Restore the previous state with cp {store.backup_path} {store.path}, or repair it; "
        f"to delete cloud hosts without offbox, run tofu -chdir={tmp_path}/hosts/alpha/tofu destroy; "
        f"tofu -chdir={tmp_path}/hosts/beta/tofu destroy."
    )


def test_unreadable_state_without_backup_or_workspaces_asks_for_repair(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.path.write_text('{"version":2}')
    with pytest.raises(PreconditionError) as caught:
        store.read()
    assert caught.value.fix == "Repair the state file."


def test_oserror_inside_a_transaction_is_not_reported_as_a_lock_failure(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    with pytest.raises(PermissionError, match="caller failure"), store.transaction():
        raise PermissionError("caller failure")
    assert not store.path.exists()


def test_lock_open_failure_is_reported_as_lock_error(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.lock_path.mkdir()
    with pytest.raises(PreconditionError, match="Cannot lock state"):
        store.read()
