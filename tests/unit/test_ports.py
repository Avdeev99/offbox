import json
import os
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.config import PortsConfig
from offbox.errors import CommandFailed, ConnectionError_, OffboxError, PreconditionError
from offbox.naming import Worktree, worktree_name
from offbox.paths import Paths
from offbox.ports import ForwardSpec, PortManager, PortsReport, allocate_port, forwarding_command, parse_compose
from offbox.runner import Runner
from offbox.ssh import ssh_argv
from offbox.state import EnvState, HostState, State, StateStore


def setup(root: Path, *, mapping: dict[int, int] | None = None, active: bool = False) -> tuple[Paths, Worktree, HostState]:
    paths = Paths(root / "config", root / "state", root / "cache", root / "control")
    work = Worktree(root=root / "one", name=worktree_name(root / "one"))
    work.root.mkdir()
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
        known_hosts_path=root / "known_hosts",
        control_master_pid=100,
    )
    ports = mapping or {}
    env = EnvState(
        name=work.name,
        host=host.name,
        worktree_path=work.root,
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
        local_ports=ports,
        active_forwards=list(ports) if active else [],
        forward_ip=IPv4Address("10.89.0.2") if ports else None,
    )
    StateStore(paths.state).write(State(hosts={host.name: host}, envs={env.name: env}, default_host=host.name))
    return paths, work, host


def inspect_script(work: Worktree, host: HostState, ip: str = "10.89.0.2", *, running: bool = True) -> Script:
    return Script(
        prefix=ssh_argv(host.name, ["docker", "container", "inspect", work.container]),
        stdout=json.dumps(
            [
                {
                    "Name": "/" + work.container,
                    "State": {"Running": running},
                    "Config": {"Labels": {"offbox.env": work.name, "offbox.worktree": str(work.root)}},
                    "NetworkSettings": {"Networks": {"offbox": {"IPAddress": ip}}},
                }
            ]
        ),
    )


def master(host: str = "main", pid: int = 100, *, absent: bool = False) -> Script:
    return Script(
        prefix=["ssh", "-O", "check", f"offbox-{host}"],
        returncode=255 if absent else 0,
        stderr="Control socket connect(/tmp/offbox/control): No such file or directory" if absent else f"Master running (pid={pid})\r\n",
    )


def spec(work: Worktree, *, local: int = 8000, remote: int = 8000, ip: str = "10.89.0.2", host: str = "main") -> ForwardSpec:
    return ForwardSpec(env=work.name, host=host, remote=remote, local=local, ip=IPv4Address(ip))


@pytest.mark.parametrize("fixture", ["ports-array.json", "ports-ndjson.json"])
def test_compose_published_tcp_ports_support_both_real_envelopes(fixture: str) -> None:
    data = (Path(__file__).parents[1] / "fixtures/compose" / fixture).read_text()
    assert parse_compose(data) == [3000, 8000]


@pytest.mark.parametrize("data", ["", "  \n", "[]", "{}", '{"Publishers":null}'])
def test_empty_compose_ports(data: str) -> None:
    assert parse_compose(data) == []


@pytest.mark.parametrize(
    "data",
    [
        '{"Publishers":[{"PublishedPort":-1,"Protocol":"tcp"}]}',
        '[{"Publishers":[{"PublishedPort":65536,"Protocol":"tcp"}]}]',
        '{"Publishers":[{"Protocol":"tcp"}]}',
        "not-json",
        '[{"Publishers":42}]',
    ],
)
def test_invalid_compose_output_is_not_silently_empty(data: str) -> None:
    with pytest.raises(PreconditionError, match="Invalid Compose"):
        parse_compose(data)


@pytest.mark.parametrize("fixture", ["ports-array.json", "ports-ndjson.json"])
def test_automatic_discovery_uses_one_fresh_owned_snapshot_and_restores_all_ports(tmp_path: Path, fixture: str) -> None:
    paths, work, host = setup(tmp_path)
    (work.root / "compose.yaml").write_text("services: {}\n")
    compose = (Path(__file__).parents[1] / "fixtures/compose" / fixture).read_text()
    response = json.dumps({"containers": json.loads(inspect_script(work, host).stdout), "compose": compose})
    query = ssh_argv(host.name, ["sh", "-s", "--", work.name, str(work.root), work.container])
    forwards = [spec(work, local=port, remote=port) for port in (3000, 8000, 9000)]
    runner = FakeRunner(
        [Script(prefix=query, stdout=response), master(), *[Script(prefix=forwarding_command("forward", item)) for item in forwards]]
    )
    report = PortManager(paths, runner, probe=lambda _: True).restore(work, host, PortsConfig(forward=[9000]))
    assert [(item.remote, item.local, item.active) for item in report.forwards] == [
        (3000, 3000, True),
        (8000, 8000, True),
        (9000, 9000, True),
    ]
    assert runner.calls[0].argv == query
    assert len([call for call in runner.calls if "-O" not in call.argv]) == 1
    assert runner.scripts == []


@pytest.mark.parametrize("invalid", ["name", "labels", "stopped", "ip", "extra-container", "compose", "schema"])
def test_invalid_combined_port_snapshot_never_changes_forward_ownership(tmp_path: Path, invalid: str) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000}, active=True)
    (work.root / "compose.yaml").write_text("services: {}\n")
    containers = json.loads(inspect_script(work, host).stdout)
    compose = "[]"
    if invalid == "name":
        containers[0]["Name"] = "/unowned"
    elif invalid == "labels":
        containers[0]["Config"]["Labels"] = {"offbox.env": "unowned"}
    elif invalid == "stopped":
        containers[0]["State"]["Running"] = False
    elif invalid == "ip":
        containers[0]["NetworkSettings"]["Networks"]["offbox"]["IPAddress"] = "bad-ip"
    elif invalid == "extra-container":
        containers.append(containers[0])
    elif invalid == "compose":
        compose = "bad-json"
    response = "{}" if invalid == "schema" else json.dumps({"containers": containers, "compose": compose})
    runner = FakeRunner([Script(prefix=["ssh"], stdout=response)])
    before = StateStore(paths.state).read()
    with pytest.raises(PreconditionError):
        PortManager(paths, runner).restore(work, host, PortsConfig())
    assert len(runner.calls) == 1
    assert StateStore(paths.state).read() == before


@pytest.mark.parametrize("status", [1, 255])
def test_combined_discovery_failure_keeps_mappings_and_connection_error_contract(tmp_path: Path, status: int) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000}, active=True)
    (work.root / "compose.yaml").write_text("services: {}\n")
    runner = FakeRunner([Script(prefix=["ssh"], returncode=status, stderr="probe failed")])
    before = StateStore(paths.state).read()
    with pytest.raises(ConnectionError_ if status == 255 else CommandFailed):
        PortManager(paths, runner).restore(work, host, PortsConfig())
    assert StateStore(paths.state).read() == before


def test_allocation_prefers_remote_then_skips_reserved_and_busy() -> None:
    probes: list[int] = []

    def available(port: int) -> bool:
        probes.append(port)
        return port != 8001

    assert allocate_port(8000, {8000}, available) == 8002
    assert probes == [8001, 8002]
    assert allocate_port(3000, set(), lambda _: True) == 3000
    with pytest.raises(PreconditionError, match="No free local port"):
        allocate_port(65535, {65535}, lambda _: True)


def test_two_envs_restore_distinct_ports_and_persist_exact_specs(tmp_path: Path) -> None:
    paths, first, host = setup(tmp_path)
    second = Worktree(root=tmp_path / "two", name="two-abcd")
    second.root.mkdir()
    with StateStore(paths.state).transaction() as state:
        state.envs[second.name] = EnvState(
            name=second.name, host=host.name, worktree_path=second.root, created_at=datetime(2026, 10, 23, tzinfo=UTC)
        )
    first_spec = spec(first)
    second_spec = spec(second, local=8001, ip="10.89.0.3")
    runner = FakeRunner(
        [
            inspect_script(first, host),
            master(),
            Script(prefix=forwarding_command("forward", first_spec)),
            inspect_script(second, host, "10.89.0.3"),
            master(),
            Script(prefix=forwarding_command("forward", second_spec)),
        ]
    )
    manager = PortManager(paths, runner, probe=lambda _: True)
    one = manager.restore(first, host, PortsConfig(forward=[8000], auto=False))
    two = manager.restore(second, host, PortsConfig(forward=[8000], auto=False))
    assert [(item.env, item.remote, item.local, item.active) for item in [*one.forwards, *two.forwards]] == [
        (first.name, 8000, 8000, True),
        (second.name, 8000, 8001, True),
    ]
    assert [call.argv for call in runner.calls if "forward" in call.argv] == [
        ["ssh", "-O", "forward", "-L", "127.0.0.1:8000:10.89.0.2:8000", "offbox-main"],
        ["ssh", "-O", "forward", "-L", "127.0.0.1:8001:10.89.0.3:8000", "offbox-main"],
    ]
    assert PortsReport.model_validate_json(two.model_dump_json()) == two
    assert StateStore(paths.state).read().envs[second.name].local_ports == {8000: 8001}


def test_persisted_inactive_forward_replayed_before_busy_probe(tmp_path: Path) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8001})

    def forbidden_probe(_port: int) -> bool:
        raise AssertionError("An exact existing mux forward must be replayed before bind probing")

    runner = FakeRunner([inspect_script(work, host), master(), Script(prefix=forwarding_command("forward", spec(work, local=8001)))])
    report = PortManager(paths, runner, probe=forbidden_probe).restore(work, host, PortsConfig(forward=[8000], auto=False))
    assert report.forwards[0].local == 8001
    assert report.forwards[0].active is True


def test_master_replacement_clears_only_its_hosts_active_flags(tmp_path: Path) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000}, active=True)
    with StateStore(paths.state).transaction() as state:
        state.envs["same-abcd"] = state.envs[work.name].model_copy(
            update={"name": "same-abcd", "local_ports": {8000: 8001}, "worktree_path": tmp_path / "same"}
        )
        state.hosts["other"] = host.model_copy(update={"name": "other", "control_master_pid": 99})
        state.envs["other-abcd"] = state.envs[work.name].model_copy(
            update={"name": "other-abcd", "host": "other", "local_ports": {8000: 8002}, "worktree_path": tmp_path / "other"}
        )
    runner = FakeRunner(
        [
            inspect_script(work, host),
            master(absent=True),
            Script(prefix=["ssh", "-fN", "offbox-main"]),
            master(pid=200),
            Script(prefix=forwarding_command("forward", spec(work))),
        ]
    )
    PortManager(paths, runner, probe=lambda _: True).restore(work, host, PortsConfig(forward=[8000], auto=False))
    state = StateStore(paths.state).read()
    assert state.hosts[host.name].control_master_pid == 200
    assert state.envs["same-abcd"].active_forwards == []
    assert state.envs["other-abcd"].active_forwards == [8000]
    assert state.envs[work.name].active_forwards == [8000]
    assert all(call.env == {"LC_ALL": "C"} for call in runner.calls if "-O" in call.argv or "-fN" in call.argv)


def test_implicitly_recreated_master_is_detected_by_changed_pid(tmp_path: Path) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000}, active=True)
    runner = FakeRunner([inspect_script(work, host), master(pid=201), Script(prefix=forwarding_command("forward", spec(work)))])
    PortManager(paths, runner).restore(work, host, PortsConfig(forward=[8000], auto=False))
    assert runner.calls[-1].argv == forwarding_command("forward", spec(work))
    assert StateStore(paths.state).read().hosts[host.name].control_master_pid == 201


def test_changed_container_ip_cancels_identical_old_spec_before_new_forward(tmp_path: Path) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000}, active=True)
    old, new = spec(work), spec(work, ip="10.89.0.9")
    runner = FakeRunner(
        [
            inspect_script(work, host, "10.89.0.9"),
            master(),
            Script(prefix=forwarding_command("cancel", old)),
            Script(prefix=forwarding_command("forward", new)),
        ]
    )
    PortManager(paths, runner).restore(work, host, PortsConfig(forward=[8000], auto=False))
    assert [call.argv for call in runner.calls][-2:] == [forwarding_command("cancel", old), forwarding_command("forward", new)]
    assert StateStore(paths.state).read().envs[work.name].forward_ip == IPv4Address("10.89.0.9")


def test_definitive_bind_conflict_reallocates_but_is_never_marked_active(tmp_path: Path) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000})
    runner = FakeRunner(
        [
            inspect_script(work, host),
            master(),
            Script(
                prefix=forwarding_command("forward", spec(work)),
                returncode=255,
                stderr="mux_client_forward: forwarding request failed: Port forwarding failed",
            ),
            Script(prefix=forwarding_command("forward", spec(work, local=8001))),
        ]
    )
    report = PortManager(paths, runner, probe=lambda port: port != 8000).restore(work, host, PortsConfig(forward=[8000], auto=False))
    assert report.forwards[0].local == 8001
    assert report.forwards[0].active is True
    assert StateStore(paths.state).read().envs[work.name].local_ports == {8000: 8001}


def test_uncertain_forward_failure_retains_mapping_inactive(tmp_path: Path) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000})
    runner = FakeRunner(
        [
            inspect_script(work, host),
            master(),
            Script(prefix=forwarding_command("forward", spec(work)), returncode=255, stderr="Control master disconnected"),
        ]
    )
    with pytest.raises(CommandFailed, match="Control master disconnected"):
        PortManager(paths, runner, probe=lambda _: False).restore(work, host, PortsConfig(forward=[8000], auto=False))
    state = StateStore(paths.state).read().envs[work.name]
    assert state.local_ports == {8000: 8000}
    assert state.active_forwards == []


def test_cancel_failure_retains_target_and_mapping_for_retry(tmp_path: Path) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000}, active=True)
    runner = FakeRunner([master(), Script(prefix=forwarding_command("cancel", spec(work)), returncode=255, stderr="permission denied")])
    with pytest.raises(CommandFailed, match="permission denied"):
        PortManager(paths, runner).cancel(work, host)
    state = StateStore(paths.state).read().envs[work.name]
    assert state.forward_ip == IPv4Address("10.89.0.2")
    assert state.local_ports == {8000: 8000}


@pytest.mark.parametrize("start", [False, True])
@pytest.mark.parametrize(
    "diagnostic",
    [
        "Control socket connect(/tmp/offbox/control): Permission denied",
        "muxclient: master hello exchange failed",
        "Bad configuration option: unknownoption",
    ],
)
def test_failed_master_check_preserves_ownership_without_starting_another_master(tmp_path: Path, start: bool, diagnostic: str) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000}, active=True)
    runner = FakeRunner(
        [
            *([inspect_script(work, host)] if start else []),
            Script(prefix=["ssh", "-O", "check", "offbox-main"], returncode=255, stderr=diagnostic),
        ]
    )
    manager = PortManager(paths, runner)
    with pytest.raises(ConnectionError_, match="Cannot check SSH master"):
        if start:
            manager.restore(work, host, PortsConfig(forward=[8000], auto=False))
        else:
            manager.cancel(work, host)
    state = StateStore(paths.state).read()
    assert state.hosts[host.name].control_master_pid == 100
    assert state.envs[work.name].active_forwards == [8000]
    assert state.envs[work.name].forward_ip == IPv4Address("10.89.0.2")
    assert [call.argv for call in runner.calls] == [
        *([ssh_argv(host.name, ["docker", "container", "inspect", work.container])] if start else []),
        ["ssh", "-O", "check", "offbox-main"],
    ]


@pytest.mark.parametrize("reason", ["No such file or directory", "Connection refused"])
def test_cancel_confirmed_missing_master_clears_stale_active_ownership(tmp_path: Path, reason: str) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000}, active=True)
    runner = FakeRunner(
        [Script(prefix=["ssh", "-O", "check", "offbox-main"], returncode=255, stderr=f"Control socket connect(/tmp/control): {reason}")]
    )
    PortManager(paths, runner).cancel(work, host)
    state = StateStore(paths.state).read()
    assert state.hosts[host.name].control_master_pid is None
    assert state.envs[work.name].local_ports == {8000: 8000}
    assert state.envs[work.name].active_forwards == []
    assert state.envs[work.name].forward_ip is None


@pytest.mark.parametrize(
    "diagnostic",
    [
        "Master refused forwarding request: Permission denied\nmuxclient: master cancel forward request failed\n",
        "mux_client_forward: forwarding request failed: port not forwarded\nPermission denied\n",
    ],
)
def test_openssh8_cancel_zero_exit_with_failure_diagnostic_retains_ownership(tmp_path: Path, diagnostic: str) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000}, active=True)
    runner = FakeRunner(
        [
            master(),
            Script(
                prefix=forwarding_command("cancel", spec(work)),
                stderr=diagnostic,
            ),
        ]
    )
    with pytest.raises(PreconditionError, match="Cannot confirm SSH forward cancellation"):
        PortManager(paths, runner).cancel(work, host)
    state = StateStore(paths.state).read().envs[work.name]
    assert state.local_ports == {8000: 8000}
    assert state.active_forwards == [8000]
    assert state.forward_ip == IPv4Address("10.89.0.2")


@pytest.mark.parametrize("returncode", [0, 255])
def test_cancel_absent_exact_spec_is_idempotent(tmp_path: Path, returncode: int) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000}, active=True)
    runner = FakeRunner(
        [
            master(),
            Script(
                prefix=forwarding_command("cancel", spec(work)),
                returncode=returncode,
                stderr="mux_client_forward: forwarding request failed: port not forwarded\nmuxclient: master cancel forward request failed\n",
            ),
        ]
    )
    PortManager(paths, runner).cancel(work, host)
    state = StateStore(paths.state).read().envs[work.name]
    assert state.forward_ip is None
    assert state.local_ports == {8000: 8000}
    assert state.active_forwards == []


def swap_setup(root: Path) -> tuple[Paths, Worktree, Worktree, HostState, list[Script]]:
    paths, work, host = setup(root, mapping={8000: 8001}, active=True)
    other = Worktree(root=root / "two", name="two-abcd")
    other.root.mkdir()
    with StateStore(paths.state).transaction() as state:
        state.envs[other.name] = EnvState(
            name=other.name,
            host=host.name,
            worktree_path=other.root,
            created_at=datetime(2026, 10, 23, tzinfo=UTC),
            local_ports={8000: 8000},
            active_forwards=[8000],
            forward_ip=IPv4Address("10.89.0.3"),
        )
    return paths, work, other, host, [inspect_script(work, host), master(), inspect_script(other, host, "10.89.0.3"), master()]


def test_swap_changes_both_mapping_entries_atomically(tmp_path: Path) -> None:
    paths, work, other, _host, scripts = swap_setup(tmp_path)
    old = [spec(work, local=8001), spec(other, ip="10.89.0.3")]
    new = [spec(work), spec(other, local=8001, ip="10.89.0.3")]
    runner = FakeRunner(
        [
            *scripts,
            *(Script(prefix=forwarding_command("cancel", item)) for item in old),
            *(Script(prefix=forwarding_command("forward", item)) for item in new),
        ]
    )
    report = PortManager(paths, runner).swap(work, 8000)
    state = StateStore(paths.state).read()
    assert report.forwards[0].local == 8000
    assert state.envs[work.name].local_ports == {8000: 8000}
    assert state.envs[other.name].local_ports == {8000: 8001}
    assert state.envs[other.name].active_forwards == [8000]
    assert [call.argv for call in runner.calls][-4:] == [
        *(forwarding_command("cancel", item) for item in old),
        *(forwarding_command("forward", item) for item in new),
    ]


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_swap_second_install_failure_restores_original_maps_and_truthful_flags(tmp_path: Path, rollback_fails: bool) -> None:
    paths, work, other, _host, scripts = swap_setup(tmp_path)
    old = [spec(work, local=8001), spec(other, ip="10.89.0.3")]
    new = [spec(work), spec(other, local=8001, ip="10.89.0.3")]
    runner = FakeRunner(
        [
            *scripts,
            *(Script(prefix=forwarding_command("cancel", item)) for item in old),
            Script(prefix=forwarding_command("forward", new[0])),
            Script(prefix=forwarding_command("forward", new[1]), returncode=255, stderr="new listener failed"),
            Script(prefix=forwarding_command("cancel", new[0])),
            Script(
                prefix=forwarding_command("forward", old[0]),
                returncode=255 if rollback_fails else 0,
                stderr="recovery listener failed" if rollback_fails else "",
            ),
            Script(prefix=forwarding_command("forward", old[1])),
        ]
    )
    with pytest.raises(OffboxError, match="Recovery incomplete" if rollback_fails else "Original mappings restored") as caught:
        PortManager(paths, runner).swap(work, 8000)
    assert caught.value.fix == (
        "Run ssh -O exit offbox-main, then run offbox ports in the affected worktrees before retrying --swap."
        if rollback_fails
        else "Run offbox ports in the affected worktrees before retrying --swap."
    )
    state = StateStore(paths.state).read()
    assert state.envs[work.name].local_ports == {8000: 8001}
    assert state.envs[other.name].local_ports == {8000: 8000}
    assert state.envs[work.name].active_forwards == ([] if rollback_fails else [8000])
    assert state.envs[other.name].active_forwards == [8000]


@pytest.mark.parametrize("other_host", ["main", "other"])
def test_swap_uncancelled_new_listener_requires_closing_each_affected_master(tmp_path: Path, other_host: str) -> None:
    paths, work, other, host, _scripts = swap_setup(tmp_path)
    with StateStore(paths.state).transaction() as state:
        state.hosts[other_host] = host.model_copy(update={"name": other_host})
        state.envs[other.name].host = other_host
    old = [spec(work, local=8001), spec(other, ip="10.89.0.3", host=other_host)]
    new = [spec(work), spec(other, local=8001, ip="10.89.0.3", host=other_host)]
    runner = FakeRunner(
        [
            inspect_script(work, host),
            master(),
            inspect_script(other, host.model_copy(update={"name": other_host}), "10.89.0.3"),
            master(other_host),
            *(Script(prefix=forwarding_command("cancel", item)) for item in old),
            Script(prefix=forwarding_command("forward", new[0])),
            Script(prefix=forwarding_command("forward", new[1]), returncode=255, stderr="second installation failed"),
            Script(
                prefix=forwarding_command("cancel", new[0]),
                stderr="mux_client_forward: forwarding request failed: cancellation denied\nmuxclient: master cancel forward request failed",
            ),
            Script(prefix=forwarding_command("forward", old[0])),
            Script(prefix=forwarding_command("forward", old[1]), returncode=255, stderr="new listener still occupies this port"),
        ]
    )
    with pytest.raises(OffboxError, match="Recovery incomplete") as caught:
        PortManager(paths, runner).swap(work, 8000)
    commands = "ssh -O exit offbox-main" + ("; ssh -O exit offbox-other" if other_host == "other" else "")
    assert caught.value.fix == f"Run {commands}, then run offbox ports in the affected worktrees before retrying --swap."
    state = StateStore(paths.state).read()
    assert state.envs[work.name].local_ports == {8000: 8001}
    assert state.envs[other.name].local_ports == {8000: 8000}
    assert state.envs[work.name].active_forwards == [8000]
    assert state.envs[other.name].active_forwards == []


def test_ip_change_with_removed_port_cancels_all_old_targets_including_unconfirmed(tmp_path: Path) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8000, 9000: 9000})
    old = [spec(work, local=9000, remote=9000), spec(work)]
    new = spec(work, ip="10.89.0.9")
    runner = FakeRunner(
        [
            inspect_script(work, host, "10.89.0.9"),
            master(),
            *(Script(prefix=forwarding_command("cancel", item)) for item in old),
            Script(prefix=forwarding_command("forward", new)),
        ]
    )
    PortManager(paths, runner).restore(work, host, PortsConfig(forward=[8000], auto=False))
    assert [call.argv for call in runner.calls][-3:] == [
        *(forwarding_command("cancel", item) for item in old),
        forwarding_command("forward", new),
    ]
    assert StateStore(paths.state).read().envs[work.name].local_ports == {8000: 8000}


def test_swap_commit_failure_rolls_back_new_listeners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, work, other, _host, scripts = swap_setup(tmp_path)
    old = [spec(work, local=8001), spec(other, ip="10.89.0.3")]
    new = [spec(work), spec(other, local=8001, ip="10.89.0.3")]
    runner = FakeRunner(
        [
            *scripts,
            *(Script(prefix=forwarding_command("cancel", item)) for item in old),
            *(Script(prefix=forwarding_command("forward", item)) for item in new),
            *(Script(prefix=forwarding_command("cancel", item)) for item in new),
            *(Script(prefix=forwarding_command("forward", item)) for item in old),
        ]
    )
    manager = PortManager(paths, runner)
    original = os.replace

    def reject_new_maps(source: Path, target: Path) -> None:
        state = State.model_validate_json(source.read_text())
        if state.envs[work.name].local_ports[8000] == 8000:
            raise OSError("synthetic state write failure")
        original(source, target)

    monkeypatch.setattr("offbox.state.os.replace", reject_new_maps)
    with pytest.raises(OffboxError, match="Original mappings restored"):
        manager.swap(work, 8000)
    state = StateStore(paths.state).read()
    assert state.envs[work.name].local_ports == {8000: 8001}
    assert state.envs[work.name].active_forwards == [8000]
    assert state.envs[other.name].local_ports == {8000: 8000}


def test_cross_host_reservations_and_swap_preserve_remote_ownership(tmp_path: Path) -> None:
    paths, work, host = setup(tmp_path, mapping={8000: 8001}, active=True)
    other = Worktree(root=tmp_path / "other", name="other-abcd")
    other.root.mkdir()
    other_host = host.model_copy(update={"name": "other", "control_master_pid": 200})
    with StateStore(paths.state).transaction() as state:
        state.hosts[other_host.name] = other_host
        state.envs[other.name] = EnvState(
            name=other.name,
            host=other_host.name,
            worktree_path=other.root,
            created_at=datetime(2026, 10, 23, tzinfo=UTC),
            local_ports={3000: 8000},
            active_forwards=[3000],
            forward_ip=IPv4Address("10.89.0.4"),
        )
    old = [spec(work, local=8001), spec(other, local=8000, remote=3000, ip="10.89.0.4", host="other")]
    new = [spec(work), spec(other, local=8001, remote=3000, ip="10.89.0.4", host="other")]
    runner = FakeRunner(
        [
            inspect_script(work, host),
            master(),
            inspect_script(other, other_host, "10.89.0.4"),
            master("other", 200),
            *(Script(prefix=forwarding_command("cancel", item)) for item in old),
            *(Script(prefix=forwarding_command("forward", item)) for item in new),
        ]
    )
    PortManager(paths, runner).swap(work, 8000)
    state = StateStore(paths.state).read()
    assert state.envs[work.name].local_ports == {8000: 8000}
    assert state.envs[other.name].local_ports == {3000: 8001}
    assert runner.calls[-1].argv == ["ssh", "-O", "forward", "-L", "127.0.0.1:8001:10.89.0.4:3000", "offbox-other"]


@pytest.mark.parametrize("json_output", [False, True])
def test_ports_cli_restores_and_emits_only_requested_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_output: bool) -> None:
    from typer.testing import CliRunner

    from offbox.cli import app
    from offbox.runtime import Runtime

    paths, work, host = setup(tmp_path)
    (work.root / "offbox.toml").write_text("[ports]\nforward=[8000]\nauto=false\n")
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(work.root)),
            inspect_script(work, host),
            master(),
            Script(prefix=forwarding_command("forward", spec(work))),
        ]
    )
    runtime = Runtime(paths, runner)
    runtime.ports = PortManager(paths, runner, probe=lambda _port: True)

    def selected_runtime(_paths: Paths, _runner: Runner) -> Runtime:
        return runtime

    monkeypatch.setattr("offbox.cli.ports.Paths.discover", lambda: paths)
    monkeypatch.setattr("offbox.cli.ports.Runtime", selected_runtime)
    result = CliRunner().invoke(app, ["ports", *(["--json"] if json_output else [])])
    assert result.exit_code == 0
    if json_output:
        assert PortsReport.model_validate_json(result.stdout) == runtime.ports.report(StateStore(paths.state).read().envs[work.name])
    else:
        assert result.stdout == f"localhost:8000 → {work.name}:8000\n"
    assert result.stderr == ""
    assert runner.scripts == []


@pytest.mark.parametrize("configured", [False, True])
def test_ports_cli_swap_reconciles_desired_ports_before_acquiring_its_own_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    from typer.testing import CliRunner

    from offbox.cli import app
    from offbox.host import host_operation
    from offbox.runtime import Runtime

    paths, work, host = setup(tmp_path, mapping=None if configured else {8000: 8000}, active=not configured)
    (work.root / "offbox.toml").write_text(f"[ports]\nforward={'[8000]' if configured else '[]'}\nauto=false\n")
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(work.root)),
            inspect_script(work, host),
            master(),
            Script(prefix=forwarding_command("forward" if configured else "cancel", spec(work))),
        ]
    )
    runtime = Runtime(paths, runner)
    runtime.ports = PortManager(paths, runner, probe=lambda _port: True)

    def selected_runtime(_paths: Paths, _runner: Runner) -> Runtime:
        return runtime

    monkeypatch.setattr("offbox.cli.ports.Paths.discover", lambda: paths)
    monkeypatch.setattr("offbox.cli.ports.Runtime", selected_runtime)
    if configured:

        def swap(self: PortManager, selected: Worktree, remote: int) -> PortsReport:
            assert remote == 8000
            with host_operation(paths, host.name):
                env = StateStore(paths.state).read().envs[selected.name]
                assert env.local_ports == {8000: 8000}
                assert env.active_forwards == [8000]
                return self.report(env)

        monkeypatch.setattr(PortManager, "swap", swap)
    result = CliRunner().invoke(app, ["ports", "--swap", "8000"])
    assert result.exit_code == (0 if configured else 3)
    assert StateStore(paths.state).read().envs[work.name].local_ports == ({8000: 8000} if configured else {})
    if not configured:
        assert "Remote port is not mapped" in result.stderr
        assert result.stdout == ""
    assert runner.scripts == []
