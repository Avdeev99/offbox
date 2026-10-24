import json
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from fakes import FakeRunner, Script
from test_ports import inspect_script, master, setup, spec, swap_setup

from offbox.config import PortsConfig
from offbox.errors import PreconditionError
from offbox.ports import AUTO_FORWARD_LIMIT, PortContainer, PortManager, PortSnapshot, forwarding_command
from offbox.ssh import ssh_argv
from offbox.state import StateStore


def test_automatic_compose_forwards_are_capped_but_explicit_and_mapped_ports_are_kept(tmp_path: Path) -> None:
    paths, work, host = setup(tmp_path, mapping={20999: 20999})
    (work.root / "compose.yaml").write_text("services: {}\n")
    published = [20000 + index for index in range(AUTO_FORWARD_LIMIT + 8)]
    compose = json.dumps([{"Publishers": [{"PublishedPort": port, "Protocol": "tcp"} for port in [*published, 20999]]}])
    containers = [PortContainer.model_validate(item) for item in json.loads(inspect_script(work, host).stdout)]
    kept = sorted([20999, *published[: AUTO_FORWARD_LIMIT - 1]])
    runner = FakeRunner(
        [master(), *[Script(prefix=forwarding_command("forward", spec(work, local=port, remote=port))) for port in [9000, *kept]]]
    )
    warnings: list[str] = []
    report = PortManager(paths, runner, probe=lambda _: True).restore(
        work, host, PortsConfig(forward=[9000]), snapshot=PortSnapshot(containers=containers, compose=compose), warn=warnings.append
    )
    assert [item.remote for item in report.forwards] == sorted([9000, *kept])
    skipped = published[AUTO_FORWARD_LIMIT - 1 :]
    assert warnings == [
        f"Skipped {len(skipped)} Compose ports beyond the automatic limit of {AUTO_FORWARD_LIMIT}: "
        + ", ".join(str(port) for port in skipped)
        + ". Fix: list the ports you need in ports.forward in offbox.toml."
    ]
    assert all(call.argv[:2] == ["ssh", "-O"] for call in runner.calls)


def test_unchanged_master_and_active_forwards_restore_without_state_writes(tmp_path: Path) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000}, active=True)
    before = (paths.state / "state.json").stat().st_ino
    runner = FakeRunner([inspect_script(work, host), master()])
    report = PortManager(paths, runner).restore(work, host, PortsConfig(forward=[8000], auto=False))
    assert [(item.remote, item.active) for item in report.forwards] == [(8000, True)]
    assert (paths.state / "state.json").stat().st_ino == before
    assert not (paths.state / "state.json.bak").exists()


@pytest.mark.parametrize("stale_active", [False, True])
def test_swap_reassigns_the_mapping_of_a_stopped_owner(tmp_path: Path, stale_active: bool) -> None:
    paths, work, other, host, _scripts = swap_setup(tmp_path)
    with StateStore(paths.state).transaction() as state:
        idle = state.envs[other.name]
        idle.active_forwards = [8000] if stale_active else []
        idle.forward_ip = IPv4Address("10.89.0.3") if stale_active else None
    cancels = [spec(work, local=8001), *([spec(other, ip="10.89.0.3")] if stale_active else [])]
    runner = FakeRunner(
        [
            inspect_script(work, host),
            master(),
            inspect_script(other, host, "10.89.0.3", running=False),
            *(Script(prefix=forwarding_command("cancel", item)) for item in cancels),
            Script(prefix=forwarding_command("forward", spec(work))),
        ]
    )
    report = PortManager(paths, runner).swap(work, 8000)
    state = StateStore(paths.state).read()
    assert [(item.remote, item.local, item.active) for item in report.forwards] == [(8000, 8000, True)]
    assert state.envs[other.name].local_ports == {8000: 8001}
    assert state.envs[other.name].active_forwards == []
    assert runner.scripts == []
    assert runner.calls[-1].argv == forwarding_command("forward", spec(work))


def test_swap_with_a_removed_owner_container_still_reassigns_the_mapping(tmp_path: Path) -> None:
    paths, work, other, host, _scripts = swap_setup(tmp_path)
    with StateStore(paths.state).transaction() as state:
        state.envs[other.name].active_forwards = []
        state.envs[other.name].forward_ip = None
    missing = ssh_argv(host.name, ["docker", "container", "inspect", other.container])
    runner = FakeRunner(
        [
            inspect_script(work, host),
            master(),
            Script(prefix=missing, returncode=1, stderr="Error: No such container: " + other.container),
            Script(prefix=forwarding_command("cancel", spec(work, local=8001))),
            Script(prefix=forwarding_command("forward", spec(work))),
        ]
    )
    PortManager(paths, runner).swap(work, 8000)
    assert StateStore(paths.state).read().envs[other.name].local_ports == {8000: 8001}
    assert StateStore(paths.state).read().envs[work.name].local_ports == {8000: 8000}


def test_swap_still_requires_the_selected_environment_to_run(tmp_path: Path) -> None:
    paths, work, _other, host, _scripts = swap_setup(tmp_path)
    runner = FakeRunner([inspect_script(work, host, running=False)])
    with pytest.raises(PreconditionError, match="Environment is not running"):
        PortManager(paths, runner).swap(work, 8000)
