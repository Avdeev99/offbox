import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from offbox import DEFAULT_IMAGE
from offbox.errors import PreconditionError
from offbox.naming import Worktree, worktree_name
from offbox.paths import Paths
from offbox.runner import FakeRunner, Script
from offbox.ssh import ssh_argv
from offbox.state import EnvState, HostState, State, StateStore
from offbox.status import Status, StatusCheck, StatusReport, compose_status, registry_config_digest

DIGEST = "sha256:" + "a" * 64


def context(root: Path) -> tuple[Paths, Worktree, HostState]:
    paths = Paths(root / "config", root / "state", root / "cache", root / "control")
    project = root / "project"
    project.mkdir()
    work = Worktree(root=project, name=worktree_name(project))
    (project / "offbox.toml").write_text("[ports]\nauto=false\n")
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=root / "known_hosts",
    )
    env = EnvState(name=work.name, host=host.name, worktree_path=work.root, created_at=datetime.now(UTC), image_digest=DIGEST)
    StateStore(paths.state).write(State(hosts={host.name: host}, envs={env.name: env}, default_host=host.name))
    return paths, work, host


def remote(host: HostState, argv: list[str], stdout: str = "", *, returncode: int = 0, stderr: str = "") -> Script:
    return Script(prefix=ssh_argv(host.name, argv), stdout=stdout, returncode=returncode, stderr=stderr)


def scripts(work: Worktree, host: HostState, *, disk: int = 25) -> list[Script]:
    sync = [
        {
            "identifier": "sync_owned",
            "version": 1,
            "creationTime": "2026-10-23T12:00:00Z",
            "creatingVersion": "0.18.1",
            "name": f"offbox-{work.name}",
            "labels": {"offbox-env": work.name},
            "paused": False,
            "status": "watching",
            "alpha": {"protocol": "local", "path": str(work.root), "connected": True},
            "beta": {"protocol": "ssh", "host": "offbox-main", "path": work.work, "connected": True},
        }
    ]
    container = [
        {
            "Name": "/" + work.container,
            "Image": DIGEST,
            "State": {"Running": True},
            "Config": {"Image": DEFAULT_IMAGE, "Labels": {"offbox.env": work.name, "offbox.worktree": str(work.root)}},
        }
    ]
    manifest = {"Descriptor": {"platform": {"os": "linux", "architecture": "amd64"}}, "OCIManifest": {"config": {"digest": DIGEST}}}
    return [
        Script(prefix=["git"], stdout=str(work.root)),
        remote(host, ["cat", "/var/lib/offbox/version"], "0.0.1\n"),
        remote(
            host, ["df", "-P", "/srv/offbox"], f"Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 100 25 75 {disk}% /\n"
        ),
        remote(host, ["docker", "info", "--format", '{"cpus":{{.NCPU}},"memory":{{.MemTotal}}}'], '{"cpus":4,"memory":1000}'),
        remote(host, ["docker", "ps", "-q", "--filter", "label=offbox.env"], ""),
        remote(host, ["docker", "container", "inspect", work.container], json.dumps(container)),
        remote(
            host,
            [
                "docker",
                "exec",
                work.container,
                "sh",
                "-c",
                "test -f /run/offbox/ready && docker --host=unix:///var/run/docker.sock info >/dev/null 2>&1",
            ],
        ),
        Script(prefix=["mutagen", "sync", "list"], stdout=json.dumps(sync)),
        remote(host, ["docker", "manifest", "inspect", "--verbose", DEFAULT_IMAGE], json.dumps(manifest)),
    ]


def test_real_oci_manifest_selects_config_digest_and_ignores_attestation() -> None:
    text = (Path(__file__).parents[1] / "fixtures/docker/manifest-verbose-oci.json").read_text()
    assert registry_config_digest(text) == "sha256:ae1bbc2d51221c530d5efbd66c17fa3c7d5c78d6732da54fc16e4309f7cbc0de"


@pytest.mark.parametrize("field", ["OCIManifest", "SchemaV2Manifest"])
def test_single_manifest_and_docker_schema_are_supported(field: str) -> None:
    assert registry_config_digest(json.dumps({"Descriptor": {}, field: {"config": {"digest": DIGEST}}})) == DIGEST


@pytest.mark.parametrize("text", ["{}", "[]", "invalid", '{"Descriptor":{},"OCIManifest":{"config":{"digest":"invalid"}}}'])
def test_unknown_registry_metadata_is_not_claimed_current(text: str) -> None:
    with pytest.raises(PreconditionError, match="Cannot determine"):
        registry_config_digest(text)


@pytest.mark.parametrize("percent,level,code", [(79, "ok", 0), (80, "warning", 0), (94, "warning", 0), (95, "error", 3), (100, "error", 3)])
def test_status_disk_thresholds_and_exit_contract(tmp_path: Path, percent: int, level: str, code: int) -> None:
    paths, work, host = context(tmp_path)
    runner = FakeRunner(scripts(work, host, disk=percent))
    report = Status(paths, runner).diagnose(cwd=work.root)
    check = next(check for check in report.checks if check.name == "disk")
    assert check.level == level
    assert check.fix == (None if level == "ok" else "Run offbox gc or offbox rm in unused worktrees.")
    assert report.exit_code == code
    assert runner.scripts == []


@pytest.mark.parametrize("phase", ["host", "disk", "resources", "environment", "sync", "image", "ports", "stack"])
def test_status_aggregates_each_probe_failure_with_actionable_fix(tmp_path: Path, phase: str) -> None:
    paths, work, host = context(tmp_path)
    entries = scripts(work, host)
    if phase in {"host", "disk", "resources", "environment", "sync", "image"}:
        index = {"host": 1, "disk": 2, "resources": 3, "environment": 6, "sync": 7, "image": 8}[phase]
        entries[index] = entries[index].model_copy(update={"returncode": 255 if phase == "host" else 1, "stderr": "synthetic failure"})
    elif phase == "ports":
        (work.root / "offbox.toml").write_text("[ports]\nforward=[8000]\nauto=false\n")
        entries.append(remote(host, ["docker", "container", "inspect", work.container], returncode=1, stderr="synthetic failure"))
    else:
        (work.root / "compose.yaml").write_text("services: {}\n")
        entries.extend(
            [
                remote(
                    host,
                    ["docker", "exec", "-u", "dev", "-w", "/work", work.container, "docker", "compose", "ps", "--format", "json"],
                    "[]",
                ),
                remote(
                    host,
                    ["docker", "exec", "-u", "dev", "-w", "/work", work.container, "docker", "compose", "ps", "--all", "--format", "json"],
                    returncode=1,
                    stderr="synthetic failure",
                ),
            ]
        )
    report = Status(paths, FakeRunner(entries)).diagnose(cwd=work.root)
    check = next(item for item in report.checks if item.name == phase)
    assert check.level == ("warning" if phase == "image" else "error")
    assert check.fix and "Fix" not in check.fix
    assert report.exit_code == (0 if phase == "image" else 3)
    if phase == "host":
        assert [item.name for item in report.checks] == ["host"]


def test_changed_remote_config_digest_warns_without_pulling_layers(tmp_path: Path) -> None:
    paths, work, host = context(tmp_path)
    entries = scripts(work, host)
    entries[-1].stdout = entries[-1].stdout.replace(DIGEST, "sha256:" + "b" * 64)
    runner = FakeRunner(entries)
    report = Status(paths, runner).diagnose(cwd=work.root)
    image = next(check for check in report.checks if check.name == "image")
    assert image.level == "warning"
    assert image.message == "Newer image available"
    assert image.fix == "Run offbox up --fresh."
    assert report.exit_code == 0
    assert not any("pull" in call.argv for call in runner.calls)


@pytest.mark.parametrize("limits,warning", [([400, 500], False), ([750, 750], True), ([0], True)])
def test_running_memory_limits_come_from_live_containers(tmp_path: Path, limits: list[int], warning: bool) -> None:
    paths, work, host = context(tmp_path)
    entries = scripts(work, host)
    identifiers = [f"{index:012x}" for index in range(len(limits))]
    entries[4].stdout = "\n".join(identifiers)
    entries.insert(
        5,
        remote(host, ["docker", "container", "inspect", *identifiers], json.dumps([{"HostConfig": {"Memory": value}} for value in limits])),
    )
    report = Status(paths, FakeRunner(entries)).diagnose(cwd=work.root)
    check = next(item for item in report.checks if item.name == "resources")
    assert check.level == ("warning" if warning else "ok")
    assert report.exit_code == 0


def test_compose_health_parses_ndjson_and_nullable_health() -> None:
    services = compose_status(
        '{"Service":"web","State":"running","Health":"healthy"}\n{"Service":"worker","State":"exited","Health":null}\n'
    )
    assert [(item.name, item.state, item.health) for item in services] == [("web", "running", "healthy"), ("worker", "exited", None)]


def test_status_json_schema_and_problem_fix_are_public_contracts() -> None:
    expected = json.loads((Path(__file__).parents[1] / "fixtures/status-schema.json").read_text())
    assert StatusReport.model_json_schema() == expected
    with pytest.raises(ValidationError, match="requires a fix"):
        StatusCheck(name="disk", level="error", message="full")


def test_custom_image_advisory_resolves_configured_dockerfile_tag_instead_of_running_tag(tmp_path: Path) -> None:
    import hashlib

    paths, work, host = context(tmp_path)
    (work.root / "offbox.toml").write_text('[env]\ndockerfile="Dockerfile.new"\n[ports]\nauto=false\n')
    (work.root / "Dockerfile.new").write_text("FROM ubuntu:24.04\n")
    digest = hashlib.sha256((work.root / "Dockerfile.new").read_bytes()).hexdigest()
    combined = hashlib.sha256(f"{digest}  Dockerfile.new\n".encode()).hexdigest()
    tag = f"offbox-local/{hashlib.sha256(str(work.root).encode()).hexdigest()[:16]}:{digest}"
    entries = scripts(work, host)[:-1]
    entries.extend(
        [
            remote(host, ["sh", "-s", "--", work.work, "Dockerfile.new"], combined + "  -\n"),
            remote(host, ["docker", "image", "inspect", "--format", "{{.Id}}", tag], "sha256:" + "b" * 64),
        ]
    )
    runner = FakeRunner(entries)
    report = Status(paths, runner).diagnose(cwd=work.root)
    image = next(item for item in report.checks if item.name == "image")
    assert image.level == "warning"
    assert image.fix == "Run offbox up --fresh."
    assert image.message == "Newer image available"
    assert runner.scripts == []
    assert not any("manifest inspect" in call.argv[-1] or "docker build" in (call.input or "") for call in runner.calls)


def test_custom_unsynchronized_dockerfile_is_explicit_unknown_advisory(tmp_path: Path) -> None:
    paths, work, host = context(tmp_path)
    (work.root / "offbox.toml").write_text('[env]\ndockerfile="Dockerfile"\n[ports]\nauto=false\n')
    (work.root / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    entries = scripts(work, host)[:-1]
    entries.append(remote(host, ["sh", "-s", "--", work.work, "Dockerfile"], "c" * 64 + "  -\n"))
    report = Status(paths, FakeRunner(entries)).diagnose(cwd=work.root)
    image = next(item for item in report.checks if item.name == "image")
    assert image.level == "warning"
    assert "not synchronized" in image.message
    assert image.fix == "Run offbox up --fresh after source sync completes."
    assert report.exit_code == 0


def test_stale_ready_marker_with_dead_inner_daemon_fails_status(tmp_path: Path) -> None:
    paths, work, host = context(tmp_path)
    entries = scripts(work, host)
    entries[6].returncode = 1
    report = Status(paths, FakeRunner(entries)).diagnose(cwd=work.root)
    environment = next(check for check in report.checks if check.name == "environment")
    assert environment.message == "Environment Docker daemon is not ready"
    assert environment.fix == "Run offbox up and inspect offbox logs if readiness fails."
    assert report.exit_code == 3
    assert "docker --host=unix:///var/run/docker.sock info" in entries[6].prefix[-1]


def test_status_restores_saved_port_mapping_without_flushing_source(tmp_path: Path) -> None:
    from ipaddress import IPv4Address

    from offbox.ports import PortForward

    paths, work, host = context(tmp_path)
    (work.root / "offbox.toml").write_text("[ports]\nforward=[8000]\nauto=false\n")
    with StateStore(paths.state).transaction() as state:
        state.envs[work.name].local_ports = {8000: 18000}
    entries = scripts(work, host)
    container = {
        "Name": "/" + work.container,
        "State": {"Running": True},
        "Config": {"Labels": {"offbox.env": work.name, "offbox.worktree": str(work.root)}},
        "NetworkSettings": {"Networks": {"offbox": {"IPAddress": "10.89.0.2"}}},
    }
    entries.extend(
        [
            remote(host, ["docker", "container", "inspect", work.container], json.dumps([container])),
            Script(prefix=["ssh", "-O", "check"], stderr="Master running (pid=100)"),
            Script(prefix=["ssh", "-O", "forward"]),
        ]
    )
    runner = FakeRunner(entries)
    report = Status(paths, runner).diagnose(cwd=work.root)
    assert report.exit_code == 0
    assert report.ports.forwards == [
        PortForward(env=work.name, host=host.name, remote=8000, local=18000, ip=IPv4Address("10.89.0.2"), active=True)
    ]
    assert not any(call.argv[:3] == ["mutagen", "sync", "flush"] for call in runner.calls)
    assert runner.scripts == []


@pytest.mark.parametrize("state,health", [("exited", None), ("running", "unhealthy")])
def test_stack_failed_service_is_error_with_log_fix(tmp_path: Path, state: str, health: str | None) -> None:
    paths, work, host = context(tmp_path)
    (work.root / "compose.yaml").write_text("services: {}\n")
    entries = scripts(work, host)
    entries.extend(
        [
            remote(
                host, ["docker", "exec", "-u", "dev", "-w", "/work", work.container, "docker", "compose", "ps", "--format", "json"], "[]"
            ),
            remote(
                host,
                ["docker", "exec", "-u", "dev", "-w", "/work", work.container, "docker", "compose", "ps", "--all", "--format", "json"],
                json.dumps([{"Service": "web", "State": state, "Health": health}]),
            ),
        ]
    )
    report = Status(paths, FakeRunner(entries)).diagnose(cwd=work.root)
    stack = next(item for item in report.checks if item.name == "stack")
    assert stack.level == "error"
    assert stack.message == "Stack: web not healthy"
    assert stack.fix == "Run offbox logs --stack and offbox up."
    assert report.exit_code == 3
