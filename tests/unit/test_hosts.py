import shlex
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.errors import ConnectionError_, OffboxError, PreconditionError
from offbox.host.ready import READY_PROBE
from offbox.paths import Paths
from offbox.runner import Runner
from offbox.state import HostState


def paths_at(root: Path) -> Paths:
    return Paths(root / "config", root / "state", root / "cache", root / "control")


def test_provider_override_and_path_rejection(tmp_path: Path) -> None:
    from offbox.providers import discover_provider

    paths = paths_at(tmp_path)
    directory = paths.config / "providers/hetzner"
    directory.mkdir(parents=True)
    (directory / "provider.toml").write_text('name="hetzner"\ndescription="override"\ndriver="tofu"\n')
    assert discover_provider("hetzner", paths).manifest.description == "override"
    with pytest.raises(PreconditionError):
        discover_provider("../outside", paths)


def test_tofu_exact_commands_and_output(tmp_path: Path) -> None:
    from offbox.drivers.base import HostVariables
    from offbox.drivers.tofu import TofuDriver

    runner = FakeRunner(
        [
            Script(prefix=["tofu", "init"]),
            Script(prefix=["tofu", "apply"]),
            Script(
                prefix=["tofu", "output"],
                stdout='{"ip":{"value":"192.0.2.1","type":"string","sensitive":false},"ssh_user":{"value":"offbox","type":"string","sensitive":false}}',
            ),
            Script(prefix=["tofu", "init"]),
            Script(prefix=["tofu", "destroy"]),
        ]
    )
    workspace = tmp_path / "tofu"
    workspace.mkdir()
    driver = TofuDriver(runner, workspace, tmp_path / "cache", {"HCLOUD_TOKEN": "synthetic-test-token"}, Path("tofu"))
    result = driver.create(
        HostVariables(
            name="offbox-main",
            size="ccx33",
            region="fsn1",
            image="ubuntu-24.04",
            ssh_public_key="public",
            user_data="cloud-init",
            labels={"offbox": "1", "offbox-host": "main"},
        )
    )
    assert result.ip == "192.0.2.1"
    assert result.ssh_user == "offbox"
    driver.destroy()
    assert [call.argv for call in runner.calls] == [
        ["tofu", "init", "-input=false"],
        ["tofu", "apply", "-auto-approve", "-input=false"],
        ["tofu", "output", "-json"],
        ["tofu", "init", "-input=false"],
        ["tofu", "destroy", "-auto-approve", "-input=false"],
    ]
    assert all(call.interruptible for call in runner.calls)
    assert all(call.cwd == workspace for call in runner.calls)
    assert runner.calls[0].env == {
        "HCLOUD_TOKEN": "synthetic-test-token",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "TF_PLUGIN_CACHE_DIR": str(tmp_path / "cache/tofu-plugins"),
        "TF_CLI_ARGS": "-no-color",
    }
    assert (workspace / "offbox.auto.tfvars.json").stat().st_mode & 0o777 == 0o600


def test_missing_provider_credential(tmp_path: Path) -> None:
    from offbox.providers import ProviderManifest, credentials_env

    manifest = ProviderManifest(name="custom", description="custom", driver="tofu", credentials_env=["CUSTOM_TOKEN"])
    with pytest.raises(PreconditionError, match="CUSTOM_TOKEN") as caught:
        credentials_env(manifest, paths_at(tmp_path), {})
    assert caught.value.exit_code == 3
    assert caught.value.fix == "Set CUSTOM_TOKEN before retrying."


def test_ssh_include_idempotent_and_preserves_user_config(tmp_path: Path) -> None:
    from offbox.ssh import ensure_include

    paths = paths_at(tmp_path)
    config = tmp_path / "home/.ssh/config"
    config.parent.mkdir(parents=True)
    config.write_text("Host personal\n  HostName example.com\n")
    config.chmod(0o640)
    ensure_include(paths, config)
    first = config.read_text()
    ensure_include(paths, config)
    assert config.read_text() == first
    assert first.endswith("Host personal\n  HostName example.com\n")
    assert first.count("Include ") == 1
    assert config.stat().st_mode & 0o777 == 0o640


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def now(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def test_readiness_timeout_uses_clock_and_collects_diagnostics(tmp_path: Path) -> None:
    from offbox.host.ready import wait_ready

    runner = FakeRunner([Script(prefix=["ssh"], stdout="bootstrap failed")])
    clock = FakeClock()
    with pytest.raises(ConnectionError_) as caught:
        wait_ready(
            "main",
            "192.0.2.1",
            22,
            runner,
            tmp_path / "logs/run.log",
            clock=clock,
            probe=lambda _address, _port, _timeout: False,
            tcp_timeout=6,
        )
    assert clock.elapsed == 6
    assert "offbox host ssh main" in (caught.value.fix or "")
    assert runner.calls[-1].argv[-1] == "sudo -n tail -n 40 /var/log/cloud-init-output.log"


def test_cloud_init_snapshot() -> None:
    from offbox.host.cloudinit import render_cloud_init

    rendered = render_cloud_init("ssh-ed25519 AAAA user", "PRIVATE\nKEY\n", "ssh-ed25519 BBBB host", script="#!/bin/sh\nset -eu\n")
    assert rendered == (Path(__file__).parents[1] / "fixtures/cloud-init.yaml").read_text()
    assert "OFFBOX_HARDEN=1" in rendered
    assert "OFFBOX_VERSION=0.1.0" in rendered


def test_failed_apply_is_retained_for_destroy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.host.create import CreateOptions, create_host
    from offbox.state import StateStore

    paths = paths_at(tmp_path)
    provider = paths.config / "providers/custom"
    provider.mkdir(parents=True)
    (provider / "provider.toml").write_text(
        'name="custom"\ndescription="custom"\ndriver="tofu"\n[defaults]\nsize="small"\nregion="test"\nimage="ubuntu"\n'
    )
    (provider / "main.tf").write_text("original provider")
    key = tmp_path / "identity"
    key.write_text("client private key")
    key.with_name("identity.pub").write_text("ssh-ed25519 AAAA client")

    def generated_key(_runner: Runner, _path: Path) -> tuple[str, str]:
        return "host private key", "ssh-ed25519 BBBB host"

    monkeypatch.setattr("offbox.host.create.generate_host_key", generated_key)
    runner = FakeRunner(
        [
            Script(prefix=["ssh-add"], returncode=1),
            Script(prefix=["ssh-keygen", "-y"], stdout="ssh-ed25519 AAAA"),
            Script(prefix=["tofu", "init"]),
            Script(prefix=["tofu", "apply"], returncode=1, stderr="apply failed"),
        ]
    )
    with pytest.raises(OffboxError, match="Creating host main failed"):
        create_host(
            paths,
            runner,
            CreateOptions(name="main", provider="custom", ssh_key=key),
            executable=Path("tofu"),
            ssh_config=tmp_path / "ssh/config",
        )
    state = StateStore(paths.state).read()
    assert state.hosts["main"].phase == "creating"
    assert state.default_host is None
    assert state.hosts["main"].provider_files_hash
    assert (paths.state / "hosts/main/tofu/main.tf").read_text() == "original provider"
    assert (paths.state / "hosts/main/tofu/offbox.auto.tfvars.json").exists()


def test_remote_argv_quotes_shell_metacharacters() -> None:
    from offbox.ssh import ssh_argv

    assert ssh_argv("main", ["printf", "%s", "a; touch /tmp/unwanted"]) == [
        "ssh",
        "offbox-main",
        "--",
        "printf %s 'a; touch /tmp/unwanted'",
    ]
    with pytest.raises(PreconditionError):
        ssh_argv("-oProxyCommand=bad", ["true"])


def test_destroy_retains_workspace_and_state_when_provider_fails(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from offbox.host.destroy import destroy_host
    from offbox.state import HostState, State, StateStore

    paths = paths_at(tmp_path)
    workspace = paths.state / "hosts/main/tofu"
    workspace.mkdir(parents=True)
    (workspace / "provider.toml").write_text('name="custom"\ndescription="test"\ndriver="tofu"\n')
    host = HostState(
        name="main",
        provider="custom",
        address="",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        provider_dir=workspace,
        known_hosts_path=workspace.parent / "known_hosts",
        phase="creating",
    )
    StateStore(paths.state).write(State(hosts={"main": host}))
    runner = FakeRunner([Script(prefix=["tofu", "init"]), Script(prefix=["tofu", "destroy"], returncode=1, stderr="provider offline")])
    with pytest.raises(OffboxError):
        destroy_host(paths, runner, "main", executable=Path("tofu"))
    assert StateStore(paths.state).read().hosts["main"] == host
    assert workspace.is_dir()


def test_existing_host_requires_explicit_host_key_acceptance(tmp_path: Path) -> None:
    from offbox.host.init import InitOptions, init_host

    runner = FakeRunner([Script(prefix=["ssh-keyscan"], stdout="192.0.2.1 ssh-ed25519 AAAA\n")])
    with pytest.raises(PreconditionError, match="accept-host-key"):
        init_host(paths_at(tmp_path), runner, InitOptions(target="dev@192.0.2.1"))
    assert not runner.calls


def test_existing_host_bootstrap_is_nonhardening_and_pinned(tmp_path: Path) -> None:
    from offbox.host.init import InitOptions, init_host
    from offbox.state import StateStore

    paths = paths_at(tmp_path)
    key = tmp_path / "identity"
    key.write_text("client private")
    key.with_name("identity.pub").write_text("ssh-ed25519 AAAA client")
    runner = FakeRunner(
        [
            Script(prefix=["ssh-keyscan"], stdout="192.0.2.1 ssh-ed25519 AAAA\n"),
            Script(prefix=["ssh-add"], returncode=1),
            Script(prefix=["ssh-keygen", "-y"], stdout="ssh-ed25519 AAAA"),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
        ]
    )
    result = init_host(
        paths,
        runner,
        InitOptions(target="dev@192.0.2.1", ssh_key=key, accept_host_key=True),
        ssh_config=tmp_path / "ssh/config",
        clock=FakeClock(),
        probe=lambda _a, _p, _t: True,
    )
    assert result.phase == "ready"
    assert StateStore(paths.state).read().default_host == "main"
    bootstrap = runner.calls[-2]
    assert "OFFBOX_HARDEN=0" in bootstrap.argv[-1]
    assert "OFFBOX_VERSION=0.1.0" in bootstrap.argv[-1]
    assert "sudo -n env" in bootstrap.argv[-1]
    assert "StrictHostKeyChecking=yes" in bootstrap.argv
    assert bootstrap.input is not None and "OFFBOX_HARDEN" in bootstrap.input
    assert (paths.state / "hosts/main/known_hosts").read_text() == "offbox-main ssh-ed25519 AAAA\n"


def test_host_list_and_use_keep_user_config_readonly(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from offbox.cli.host import host_report, use_host
    from offbox.state import HostState, State, StateStore

    paths = paths_at(tmp_path)
    paths.config.mkdir(parents=True)
    paths.user_config.write_text('default_host="unused"\n')
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=tmp_path / "known_hosts",
    )
    StateStore(paths.state).write(State(hosts={"main": host}))
    runner = FakeRunner([Script(prefix=["ssh"], returncode=0)])
    report = host_report(paths, runner)
    assert report.hosts[0].reachable is True
    assert report.hosts[0].envs == 0
    use_host(paths, "main")
    assert StateStore(paths.state).read().default_host == "main"
    assert paths.user_config.read_text() == 'default_host="unused"\n'


def test_host_destroy_noninteractive_requires_yes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from typer.testing import CliRunner

    from offbox.cli import app
    from offbox.state import HostState, State, StateStore

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    paths = Paths.discover()
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=tmp_path / "known_hosts",
    )
    StateStore(paths.state).write(State(hosts={"main": host}, default_host="main"))
    result = CliRunner().invoke(app, ["host", "destroy"])
    assert result.exit_code == 3
    assert "--yes" in result.stderr
    assert "Fix:" in result.stderr
    assert StateStore(paths.state).read().hosts["main"] == host


def test_public_key_rejects_stale_sidecar_before_provisioning(tmp_path: Path) -> None:
    from offbox.host import public_key

    key = tmp_path / "identity"
    key.write_text("private")
    key.with_name("identity.pub").write_text("ssh-ed25519 WRONG stale")
    runner = FakeRunner([Script(prefix=["ssh-keygen", "-y"], stdout="ssh-ed25519 CORRECT\n")])
    with pytest.raises(PreconditionError, match="does not match"):
        public_key(runner, key)
    assert runner.calls[0].argv == ["ssh-keygen", "-y", "-P", "", "-f", str(key)]


@pytest.mark.parametrize("docker_exit", [0, 1])
def test_create_refreshes_bootstrap_login_and_checks_docker_before_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docker_exit: int
) -> None:
    from offbox.host.create import CreateOptions, create_host
    from offbox.host.destroy import destroy_host
    from offbox.state import HostState, StateStore

    paths = paths_at(tmp_path)
    key = tmp_path / "identity"
    key.write_text("private")
    key.with_name("identity.pub").write_text("ssh-ed25519 AAAA")

    def generated_key(_runner: Runner, path: Path) -> tuple[str, str]:
        path.write_text("PRIVATE HOST KEY")
        path.with_name(path.name + ".pub").write_text("ssh-ed25519 BBBB")
        return "PRIVATE HOST KEY", "ssh-ed25519 BBBB"

    monkeypatch.setattr("offbox.host.create.generate_host_key", generated_key)
    monkeypatch.setenv("HCLOUD_TOKEN", "synthetic-host-create-token")
    runner = FakeRunner(
        [
            Script(prefix=["ssh-keygen", "-y"], stdout="ssh-ed25519 AAAA"),
            Script(prefix=["tofu", "init"]),
            Script(prefix=["tofu", "apply"]),
            Script(prefix=["tofu", "output"], stdout='{"ip":{"value":"192.0.2.1"},"ssh_user":{"value":"offbox"}}'),
            Script(prefix=["ssh", "-o"], stdout=""),
            Script(prefix=["ssh", "-O", "check"], stderr="Master running (pid=12345)"),
            Script(prefix=["ssh", "-O", "exit"]),
            Script(
                prefix=["ssh", "offbox-main", "--", "docker info --format '{{.ServerVersion}}'"],
                stdout="29.9.2",
                returncode=docker_exit,
                stderr="permission denied" if docker_exit else "",
            ),
            Script(prefix=["ssh", "-O", "check"], returncode=255, stderr="Control socket connect(/tmp/control): No such file or directory"),
            Script(prefix=["tofu", "init"]),
            Script(prefix=["tofu", "destroy"]),
        ]
    )

    def provision() -> HostState:
        return create_host(
            paths,
            runner,
            CreateOptions(ssh_key=key),
            executable=Path("tofu"),
            clock=FakeClock(),
            probe=lambda _a, _p, _t: True,
            ssh_config=tmp_path / "ssh/config",
        )

    if docker_exit:
        with pytest.raises(OffboxError, match="permission denied"):
            provision()
        host = StateStore(paths.state).read().hosts["main"]
    else:
        host = provision()
    assert host.phase == ("initializing" if docker_exit else "ready")
    assert host.address == "192.0.2.1"
    assert StateStore(paths.state).read().default_host == (None if docker_exit else "main")
    assert host.known_hosts_path.read_text() == "offbox-main ssh-ed25519 BBBB\n"
    assert "StrictHostKeyChecking yes" in (paths.config / "ssh_config").read_text()
    assert not (host.known_hosts_path.parent / "ssh_host_ed25519_key").exists()
    assert not (host.known_hosts_path.parent / "ssh_host_ed25519_key.pub").exists()
    # Bootstrap grants Docker group membership after the readiness connection has opened.
    # Retire that connection and verify Docker through a fresh login before declaring ready.
    assert [call.argv for call in runner.calls][-4:] == [
        ["ssh", "-o", "ConnectTimeout=3", "offbox-main", "--", shlex.join(["sh", "-c", READY_PROBE])],
        ["ssh", "-O", "check", "offbox-main"],
        ["ssh", "-O", "exit", "offbox-main"],
        ["ssh", "offbox-main", "--", "docker info --format '{{.ServerVersion}}'"],
    ]
    destroy_host(paths, runner, "main", executable=Path("tofu"))
    assert StateStore(paths.state).read().hosts == {}
    assert StateStore(paths.state).read().default_host is None
    assert not (paths.state / "hosts/main").exists()
    assert (paths.config / "ssh_config").read_text() == ""


def test_none_driver_removes_only_offbox_resources_in_order() -> None:
    import json
    from datetime import UTC, datetime

    from offbox.drivers.none import NoneDriver
    from offbox.state import HostState

    labels = {"offbox.env": "feature", "offbox.worktree": "/worktree"}
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], stdout="abcdef012345\n"),
            Script(prefix=["ssh"], stdout=json.dumps([{"Id": "abcdef012345", "Name": "/offbox-feature", "Config": {"Labels": labels}}])),
            Script(prefix=["ssh"], stdout="offbox-feature-docker\n"),
            Script(prefix=["ssh"], stdout=json.dumps([{"Name": "offbox-feature-docker", "Labels": labels}])),
            Script(prefix=["ssh"], stdout='[{"Name":"offbox","Labels":{"offbox.managed":"1"}}]'),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
        ]
    )
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=Path("known_hosts"),
    )
    NoneDriver(runner, host).destroy()
    assert [call.argv[-1] for call in runner.calls] == [
        "docker ps -aq --no-trunc --filter label=offbox.env",
        "docker container inspect abcdef012345",
        "docker volume ls -q --filter label=offbox.env",
        "docker volume inspect offbox-feature-docker",
        "docker network inspect offbox",
        "docker ps -aq --no-trunc --filter network=offbox",
        "docker rm -f abcdef012345",
        "docker volume rm offbox-feature-docker",
        "docker network rm offbox",
        "sudo -n rm -rf /srv/offbox /var/lib/offbox",
        "sudo -n rm -f /etc/sudoers.d/offbox /home/offbox/.ssh/authorized_keys",
    ]


def test_existing_host_retry_updates_validated_identity(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from offbox.host.init import InitOptions, init_host
    from offbox.state import HostState, State, StateStore

    paths = paths_at(tmp_path)
    directory = paths.state / "hosts/main"
    directory.mkdir(parents=True)
    known = directory / "known_hosts"
    known.write_text("offbox-main ssh-ed25519 AAAA\n")
    previous = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=known,
        phase="initializing",
        ssh_key=tmp_path / "old-key",
    )
    StateStore(paths.state).write(State(hosts={"main": previous}))
    key = tmp_path / "new-key"
    key.write_text("private")
    runner = FakeRunner(
        [
            Script(prefix=["ssh-keyscan"], stdout="192.0.2.1 ssh-ed25519 AAAA\n"),
            Script(prefix=["ssh-keygen", "-y"], stdout="ssh-ed25519 BBBB"),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
        ]
    )
    host = init_host(
        paths,
        runner,
        InitOptions(target="dev@192.0.2.1", ssh_key=key, accept_host_key=True),
        ssh_config=tmp_path / "ssh/config",
        clock=FakeClock(),
        probe=lambda _a, _p, _t: True,
    )
    assert host.ssh_key == key
    assert f'IdentityFile "{key}"' in (paths.config / "ssh_config").read_text()
    assert StateStore(paths.state).read().hosts["main"].ssh_key == key


def test_agent_only_identity_is_supported(tmp_path: Path) -> None:
    from offbox.host import public_key

    key = tmp_path / "hardware-key"
    key.with_name(key.name + ".pub").write_text("ssh-ed25519 AAAA hardware")
    runner = FakeRunner(
        [
            Script(prefix=["ssh-keygen", "-y"], returncode=1),
            Script(prefix=["ssh-keygen", "-lf"], stdout="256 SHA256:known hardware (ED25519)"),
            Script(prefix=["ssh-add", "-l"], stdout="256 SHA256:known hardware (ED25519)"),
        ]
    )
    assert public_key(runner, key) == "ssh-ed25519 AAAA hardware"


def test_host_operation_refuses_concurrent_lifecycle(tmp_path: Path) -> None:
    from offbox.host import host_operation

    paths = paths_at(tmp_path)
    with host_operation(paths, "main"), pytest.raises(PreconditionError, match="already running"), host_operation(paths, "main"):
        pytest.fail("Two lifecycle operations must not hold the same host lock")


def test_destroy_accepts_full_length_digit_started_env_names(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from offbox.host.destroy import destroy_host
    from offbox.state import EnvState, HostState, State, StateStore

    paths = paths_at(tmp_path)
    workspace = paths.state / "hosts/main/tofu"
    workspace.mkdir(parents=True)
    (workspace / "provider.toml").write_text('name="custom"\ndescription="test"\ndriver="tofu"\n')
    host = HostState(
        name="main",
        provider="custom",
        address="",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        provider_dir=workspace,
        known_hosts_path=workspace.parent / "known_hosts",
        phase="creating",
    )
    env_name = "1" + "a" * 31 + "-abcd"
    env = EnvState(name=env_name, host="main", worktree_path=tmp_path / "repo", created_at=datetime.now(UTC))
    StateStore(paths.state).write(State(hosts={"main": host}, envs={env_name: env}))
    runner = FakeRunner(
        [Script(prefix=["mutagen", "sync", "list"], stdout="[]"), Script(prefix=["tofu", "init"]), Script(prefix=["tofu", "destroy"])]
    )
    destroy_host(paths, runner, "main", executable=Path("tofu"))
    assert [call.argv for call in runner.calls] == [
        ["mutagen", "sync", "list", "--label-selector", f"offbox-env={env_name}", "--template", "{{ json . }}"],
        ["tofu", "init", "-input=false"],
        ["tofu", "destroy", "-auto-approve", "-input=false"],
    ]
    assert StateStore(paths.state).read().envs == {}


def test_cli_missing_credential_is_actionable_exit_three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from offbox.cli import app

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("HCLOUD_TOKEN", raising=False)
    result = CliRunner().invoke(app, ["host", "create"])
    assert result.exit_code == 3
    assert "HCLOUD_TOKEN" in result.stderr
    assert "Fix:" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("name_arguments", [[], ["main"]])
def test_host_ssh_separator_preserves_exact_remote_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name_arguments: list[str]
) -> None:
    from collections.abc import Mapping, Sequence
    from datetime import UTC, datetime

    from typer.testing import CliRunner

    from offbox.cli import app
    from offbox.runner import SubprocessRunner
    from offbox.state import HostState, State, StateStore

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    paths = Paths.discover()
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=tmp_path / "known_hosts",
    )
    StateStore(paths.state).write(State(hosts={"main": host}, default_host="main"))
    calls: list[list[str]] = []

    def stream(
        _self: SubprocessRunner, argv: Sequence[str], *, env: Mapping[str, str] | None = None, stdin: bool = False, tty: bool = False
    ) -> int:
        calls.append(list(argv))
        return 42

    monkeypatch.setattr(SubprocessRunner, "stream", stream)
    result = CliRunner().invoke(app, ["host", "ssh", *name_arguments, "--", "printf", "%s", "a; touch /tmp/unwanted"])
    assert result.exit_code == 42
    assert calls == [["ssh", "offbox-main", "--", "printf %s 'a; touch /tmp/unwanted'"]]


def test_missing_mutagen_does_not_strand_host_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    from offbox.naming import Worktree
    from offbox.runner import SubprocessRunner
    from offbox.sync import SyncManager

    def missing(_argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", "mutagen")

    monkeypatch.setattr("offbox.runner.subprocess.run", missing)

    SyncManager(SubprocessRunner(), cache=tmp_path).terminate(Worktree(root=Path("/src/project"), name="project-abcd"))
    assert "Mutagen is not installed" in capsys.readouterr().err


def test_mutagen_termination_failure_is_not_hidden() -> None:
    from offbox.errors import CommandFailed
    from offbox.naming import Worktree
    from offbox.sync import SyncManager

    runner = FakeRunner([Script(prefix=["mutagen"], returncode=1, stderr="daemon permission denied")])
    with pytest.raises(CommandFailed, match="daemon permission denied"):
        SyncManager(runner).terminate(Worktree(root=Path("/src/project"), name="project-abcd"))


@pytest.mark.parametrize("failure", ["sync", "check", "exit"])
def test_cloud_destroy_runs_even_when_local_sync_or_master_cleanup_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    from datetime import UTC, datetime
    from ipaddress import IPv4Address

    from offbox.host.destroy import destroy_host
    from offbox.state import EnvState, HostState, State, StateStore

    paths = paths_at(tmp_path)
    workspace = paths.state / "hosts/main/tofu"
    workspace.mkdir(parents=True)
    (workspace / "provider.toml").write_text('name="custom"\ndescription="test"\ndriver="tofu"\n')
    host = HostState(
        name="main",
        provider="custom",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        provider_dir=workspace,
        known_hosts_path=workspace.parent / "known_hosts",
        control_master_pid=100,
    )
    env = EnvState(
        name="app-abcd",
        host="main",
        worktree_path=tmp_path / "repo",
        created_at=datetime.now(UTC),
        local_ports={8000: 8000},
        active_forwards=[8000],
        forward_ip=IPv4Address("10.89.0.2"),
    )
    StateStore(paths.state).write(State(hosts={"main": host}, envs={env.name: env}))
    runner = FakeRunner(
        [
            Script(
                prefix=["mutagen", "sync", "list"],
                returncode=1 if failure == "sync" else 0,
                stdout="[]",
                stderr="mutagen daemon unavailable",
            ),
            Script(
                prefix=["ssh", "-O", "check"],
                returncode=255 if failure == "check" else 0,
                stderr="Control socket connect(/tmp/control): Permission denied" if failure == "check" else "Master running (pid=100)",
            ),
            Script(
                prefix=["ssh", "-O", "exit"],
                returncode=255 if failure == "exit" else 0,
                stderr="Master refused termination request" if failure == "exit" else "",
            ),
            Script(prefix=["tofu", "init"]),
            Script(prefix=["tofu", "destroy"]),
        ]
    )
    destroy_host(paths, runner, "main", executable=Path("tofu"))
    assert [call.argv[:2] for call in runner.calls][-2:] == [["tofu", "init"], ["tofu", "destroy"]]
    state = StateStore(paths.state).read()
    assert state.hosts == {}
    assert state.envs == {}
    assert not workspace.exists()
    err = " ".join(capsys.readouterr().err.split())
    if failure == "sync":
        assert "Cannot stop source sync for app-abcd; afterwards run mutagen sync list and terminate offbox-app-abcd" in err
    else:
        assert "Cannot close the SSH master for offbox-main; stop that local ssh process manually" in err
    assert "offbox up" not in err
    assert "Destroyed host main" in err


def test_destroy_releases_global_state_lock_and_preserves_other_hosts_concurrent_update(tmp_path: Path) -> None:
    import fcntl
    from collections.abc import Mapping, Sequence
    from datetime import UTC, datetime

    from offbox.host.destroy import destroy_host
    from offbox.runner import Result
    from offbox.state import HostState, State, StateStore

    paths = paths_at(tmp_path)
    workspace = paths.state / "hosts/main/tofu"
    workspace.mkdir(parents=True)
    (workspace / "provider.toml").write_text('name="custom"\ndescription="test"\ndriver="tofu"\n')
    host = HostState(
        name="main",
        provider="custom",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        provider_dir=workspace,
        known_hosts_path=workspace.parent / "known_hosts",
    )
    other = host.model_copy(update={"name": "other", "address": "192.0.2.2"})
    store = StateStore(paths.state)
    store.write(State(hosts={"main": host, "other": other}, default_host="main"))

    class ConcurrentRunner(FakeRunner):
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
            with store.lock_path.open("a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            if argv[0] == "tofu":
                with store.transaction() as state:
                    state.hosts["other"].address = "192.0.2.3"
                    state.default_host = "other"
            return super().run(
                argv, check=check, capture=capture, input=input, env=env, cwd=cwd, timeout=timeout, interruptible=interruptible
            )

    runner = ConcurrentRunner(
        [
            Script(prefix=["ssh", "-O", "check"], stderr="Master running (pid=100)"),
            Script(prefix=["ssh", "-O", "exit"], stderr="Exit request sent."),
            Script(prefix=["tofu", "init"]),
            Script(prefix=["tofu", "destroy"]),
        ]
    )
    destroy_host(paths, runner, "main", executable=Path("tofu"))
    state = store.read()
    assert set(state.hosts) == {"other"}
    assert state.hosts["other"].address == "192.0.2.3"
    assert state.default_host == "other"
    assert [call.argv for call in runner.calls][:2] == [["ssh", "-O", "check", "offbox-main"], ["ssh", "-O", "exit", "offbox-main"]]


def _existing_host(tmp_path: Path) -> tuple[Paths, HostState]:
    from datetime import UTC, datetime

    from offbox.state import EnvState, State, StateStore

    paths = paths_at(tmp_path)
    workspace = paths.state / "hosts/main/provider"
    workspace.mkdir(parents=True)
    (workspace / "provider.toml").write_text('name="ssh"\ndescription="existing"\ndriver="none"\n')
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        provider_dir=workspace,
        known_hosts_path=workspace.parent / "known_hosts",
    )
    env = EnvState(name="app-abcd", host="main", worktree_path=tmp_path / "repo", created_at=datetime.now(UTC))
    StateStore(paths.state).write(State(hosts={"main": host}, envs={env.name: env}, default_host="main"))
    return paths, host


def _cloud_host(tmp_path: Path) -> tuple[Paths, HostState]:
    from datetime import UTC, datetime

    from offbox.state import State, StateStore

    paths = paths_at(tmp_path)
    workspace = paths.state / "hosts/main/tofu"
    workspace.mkdir(parents=True)
    (workspace / "provider.toml").write_text('name="custom"\ndescription="test"\ndriver="tofu"\n')
    host = HostState(
        name="main",
        provider="custom",
        address="",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        provider_dir=workspace,
        known_hosts_path=workspace.parent / "known_hosts",
        phase="creating",
    )
    StateStore(paths.state).write(State(hosts={"main": host}))
    return paths, host


def test_existing_host_destroy_removes_record_after_remote_cleanup_when_master_cannot_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from offbox.host.destroy import destroy_host
    from offbox.state import StateStore

    paths, _host = _existing_host(tmp_path)
    runner = FakeRunner(
        [
            Script(prefix=["mutagen", "sync", "list"], stdout="[]"),
            Script(prefix=["ssh", "-O", "check"], returncode=255, stderr="Control socket connect(/tmp/control): No such file or directory"),
            Script(prefix=["ssh", "offbox-main"]),
            Script(prefix=["ssh", "offbox-main"]),
            Script(prefix=["ssh", "offbox-main"], returncode=1, stderr="network offbox not found"),
            Script(prefix=["ssh", "offbox-main"]),
            Script(prefix=["ssh", "offbox-main"]),
            Script(prefix=["ssh", "-O", "check"], stderr="Master running (pid=200)"),
            Script(prefix=["ssh", "-O", "exit"], returncode=255, stderr="Master refused termination request"),
        ]
    )
    destroy_host(paths, runner, "main")
    assert StateStore(paths.state).read().hosts == {}
    assert not (paths.state / "hosts/main").exists()
    assert runner.scripts == []
    assert "Cannot close the SSH master for offbox-main" in " ".join(capsys.readouterr().err.split())


def test_existing_host_destroy_stays_strict_before_remote_cleanup(tmp_path: Path) -> None:
    from offbox.host.destroy import destroy_host
    from offbox.state import StateStore

    paths, host = _existing_host(tmp_path)
    runner = FakeRunner([Script(prefix=["mutagen", "sync", "list"], returncode=1, stderr="mutagen daemon unavailable")])
    with pytest.raises(OffboxError, match="mutagen daemon unavailable"):
        destroy_host(paths, runner, "main")
    assert StateStore(paths.state).read().hosts["main"] == host
    assert not any(call.argv[0] == "ssh" for call in runner.calls)


def test_forget_removes_only_local_records_for_existing_hosts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from offbox.host.destroy import destroy_host
    from offbox.state import StateStore

    paths, _host = _existing_host(tmp_path)
    runner = FakeRunner(
        [
            Script(prefix=["mutagen", "sync", "list"], returncode=1, stderr="mutagen daemon unavailable"),
            Script(prefix=["ssh", "-O", "check"], returncode=255, stderr="Control socket connect(/tmp/control): Connection refused"),
        ]
    )
    destroy_host(paths, runner, "main", forget=True)
    state = StateStore(paths.state).read()
    assert (state.hosts, state.envs, state.default_host) == ({}, {}, None)
    assert not (paths.state / "hosts/main").exists()
    assert [call.argv[:3] for call in runner.calls] == [["mutagen", "sync", "list"], ["ssh", "-O", "check"]]
    err = " ".join(capsys.readouterr().err.split())
    assert "Forgot host main without contacting 192.0.2.1" in err
    assert "/etc/sudoers.d/offbox" in err
    assert "/home/offbox/.ssh/authorized_keys" in err


def test_forget_is_rejected_for_cloud_hosts(tmp_path: Path) -> None:
    from offbox.errors import UsageError
    from offbox.host.destroy import destroy_host
    from offbox.state import StateStore

    paths, host = _cloud_host(tmp_path)
    runner = FakeRunner()
    with pytest.raises(UsageError, match="cloud host") as caught:
        destroy_host(paths, runner, "main", executable=Path("tofu"), forget=True)
    assert caught.value.fix == "Run offbox host destroy main --yes to delete it."
    assert StateStore(paths.state).read().hosts["main"] == host
    assert runner.calls == []


def test_cli_forget_passes_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from offbox.cli import app

    paths, _host = _existing_host(tmp_path)
    seen: list[tuple[str | None, bool]] = []

    def destroy(_paths: Paths, _runner: Runner, name: str | None = None, *, forget: bool = False) -> None:
        seen.append((name, forget))

    monkeypatch.setattr("offbox.cli.host.Paths.discover", lambda: paths)
    monkeypatch.setattr("offbox.cli.host.destroy_host", destroy)
    result = CliRunner().invoke(app, ["host", "destroy", "main", "--forget", "--yes"])
    assert result.exit_code == 0
    assert seen == [("main", True)]
    assert "Forgetting main" in result.stderr


def test_destroy_removes_record_when_state_changes_after_cloud_destroy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from collections.abc import Mapping, Sequence
    from datetime import UTC, datetime

    from offbox.host.destroy import destroy_host
    from offbox.runner import Result
    from offbox.state import EnvState, StateStore

    paths, _host = _cloud_host(tmp_path)
    store = StateStore(paths.state)

    class AddsEnvDuringDestroy(FakeRunner):
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
            if list(argv[:2]) == ["tofu", "destroy"]:
                with store.transaction() as state:
                    state.envs["late-abcd"] = EnvState(
                        name="late-abcd", host="main", worktree_path=tmp_path / "late", created_at=datetime.now(UTC)
                    )
            return super().run(
                argv, check=check, capture=capture, input=input, env=env, cwd=cwd, timeout=timeout, interruptible=interruptible
            )

    runner = AddsEnvDuringDestroy([Script(prefix=["tofu", "init"]), Script(prefix=["tofu", "destroy"])])
    destroy_host(paths, runner, "main", executable=Path("tofu"))
    state = store.read()
    assert (state.hosts, state.envs) == ({}, {})
    assert not (paths.state / "hosts/main").exists()
    assert "changed while it was being destroyed" in " ".join(capsys.readouterr().err.split())


@pytest.mark.parametrize("killed", [False, True])
def test_interrupted_cloud_destroy_keeps_record_and_names_the_label(tmp_path: Path, killed: bool) -> None:
    from collections.abc import Mapping, Sequence

    from offbox.errors import ProviderInterrupted
    from offbox.host.destroy import destroy_host
    from offbox.runner import Result
    from offbox.state import StateStore

    paths, host = _cloud_host(tmp_path)

    class InterruptedDestroy(FakeRunner):
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
            if list(argv[:2]) == ["tofu", "destroy"]:
                raise ProviderInterrupted("Provider operation interrupted", "generic", killed=killed)
            return super().run(
                argv, check=check, capture=capture, input=input, env=env, cwd=cwd, timeout=timeout, interruptible=interruptible
            )

    with pytest.raises(ProviderInterrupted) as caught:
        destroy_host(paths, InterruptedDestroy([Script(prefix=["tofu", "init"])]), "main", executable=Path("tofu"))
    assert caught.value.exit_code == 130
    assert "offbox host destroy main" in (caught.value.fix or "")
    assert ("offbox-host=main" in (caught.value.fix or "")) is killed
    assert StateStore(paths.state).read().hosts["main"] == host


def _custom_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Paths, Path]:
    paths = paths_at(tmp_path)
    provider = paths.config / "providers/custom"
    provider.mkdir(parents=True)
    (provider / "provider.toml").write_text(
        'name="custom"\ndescription="custom"\ndriver="tofu"\n[defaults]\nsize="small"\nregion="test"\nimage="ubuntu"\n'
    )
    (provider / "main.tf").write_text("original provider")
    key = tmp_path / "identity"
    key.write_text("client private key")
    key.with_name("identity.pub").write_text("ssh-ed25519 AAAA client")

    def generated_key(_runner: Runner, path: Path) -> tuple[str, str]:
        path.write_text("host private key")
        path.with_name(path.name + ".pub").write_text("ssh-ed25519 BBBB host")
        return "host private key", "ssh-ed25519 BBBB host"

    monkeypatch.setattr("offbox.host.create.generate_host_key", generated_key)
    return paths, key


def test_interrupt_after_apply_reports_destroy_hint_and_exit_130(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.host.create import CreateOptions, create_host
    from offbox.state import StateStore

    paths, key = _custom_provider(tmp_path, monkeypatch)

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("offbox.host.create.wait_ready", interrupted)
    runner = FakeRunner(
        [
            Script(prefix=["ssh-keygen", "-y"], stdout="ssh-ed25519 AAAA"),
            Script(prefix=["tofu", "init"]),
            Script(prefix=["tofu", "apply"]),
            Script(prefix=["tofu", "output"], stdout='{"ip":{"value":"192.0.2.1"},"ssh_user":{"value":"offbox"}}'),
        ]
    )
    with pytest.raises(OffboxError) as caught:
        create_host(
            paths,
            runner,
            CreateOptions(name="main", provider="custom", ssh_key=key),
            executable=Path("tofu"),
            ssh_config=tmp_path / "ssh/config",
        )
    assert caught.value.exit_code == 130
    assert caught.value.message == "Creating host main was interrupted"
    assert caught.value.fix == "Run offbox host destroy main to clean up; its provider workspace was retained."
    host = StateStore(paths.state).read().hosts["main"]
    assert (host.phase, host.address) == ("initializing", "192.0.2.1")
    assert not (paths.state / "hosts/main/ssh_host_ed25519_key").exists()
    assert not (paths.state / "hosts/main/ssh_host_ed25519_key.pub").exists()
    assert "host private key" in (paths.state / "hosts/main/tofu/offbox.auto.tfvars.json").read_text()


@pytest.mark.parametrize("killed", [False, True])
def test_interrupted_apply_names_destroy_and_label_when_force_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, killed: bool
) -> None:
    from collections.abc import Mapping, Sequence

    from offbox.errors import ProviderInterrupted
    from offbox.host.create import CreateOptions, create_host
    from offbox.runner import Result
    from offbox.state import StateStore

    paths, key = _custom_provider(tmp_path, monkeypatch)

    class InterruptedApply(FakeRunner):
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
            if list(argv[:2]) == ["tofu", "apply"]:
                raise ProviderInterrupted("Provider operation interrupted", "generic", killed=killed)
            return super().run(
                argv, check=check, capture=capture, input=input, env=env, cwd=cwd, timeout=timeout, interruptible=interruptible
            )

    runner = InterruptedApply([Script(prefix=["ssh-keygen", "-y"], stdout="ssh-ed25519 AAAA"), Script(prefix=["tofu", "init"])])
    with pytest.raises(OffboxError) as caught:
        create_host(
            paths,
            runner,
            CreateOptions(name="main", provider="custom", ssh_key=key),
            executable=Path("tofu"),
            ssh_config=tmp_path / "ssh/config",
        )
    assert caught.value.exit_code == 130
    if killed:
        assert caught.value.fix == (
            "Run offbox host destroy main, then check the provider console for resources labelled offbox-host=main "
            "and delete any that remain."
        )
    else:
        assert caught.value.fix == "Run offbox host destroy main to clean up; its provider workspace was retained."
    assert StateStore(paths.state).read().hosts["main"].phase == "creating"
    assert not (paths.state / "hosts/main/ssh_host_ed25519_key").exists()


def test_provider_copy_failure_removes_the_unrecorded_host_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.host.create import CreateOptions, create_host
    from offbox.providers import Provider
    from offbox.state import StateStore

    paths, key = _custom_provider(tmp_path, monkeypatch)

    def failing_copy(_provider: Provider, workspace: Path) -> str:
        workspace.mkdir()
        (workspace / "partial.tf").write_text("partial")
        raise PreconditionError("Cannot copy provider", "Check directory permissions and free disk space.")

    monkeypatch.setattr("offbox.host.create.copy_provider", failing_copy)
    runner = FakeRunner([Script(prefix=["ssh-keygen", "-y"], stdout="ssh-ed25519 AAAA")])
    with pytest.raises(PreconditionError, match="Cannot copy provider"):
        create_host(paths, runner, CreateOptions(name="main", provider="custom", ssh_key=key), executable=Path("tofu"))
    assert not (paths.state / "hosts/main").exists()
    assert StateStore(paths.state).read().hosts == {}


def bootstrap_runner(tmp_path: Path) -> tuple[FakeRunner, Path]:
    key = tmp_path / "identity"
    key.write_text("client private")
    key.with_name("identity.pub").write_text("ssh-ed25519 AAAA client")
    runner = FakeRunner(
        [
            Script(prefix=["ssh-keyscan"], stdout="192.0.2.1 ssh-ed25519 AAAA\n"),
            Script(prefix=["ssh-add"], returncode=1),
            Script(prefix=["ssh-keygen", "-y"], stdout="ssh-ed25519 AAAA"),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
        ]
    )
    return runner, key


def scanned_fingerprint() -> str:
    import base64
    import hashlib

    return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode("AAAA")).digest()).decode().rstrip("=")


def test_pinned_fingerprint_allows_noninteractive_init_with_hardened_bootstrap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from offbox.host.init import InitOptions, init_host

    root = tmp_path / "home dir"
    root.mkdir()
    runner, key = bootstrap_runner(root)
    host = init_host(
        paths_at(root),
        runner,
        InitOptions(target="dev@192.0.2.1", ssh_key=key, host_key_fingerprint=scanned_fingerprint()),
        ssh_config=root / "ssh/config",
        clock=FakeClock(),
        probe=lambda _a, _p, _t: True,
    )
    assert host.phase == "ready"
    bootstrap = runner.calls[-2].argv
    assert f'UserKnownHostsFile="{root}/state/hosts/main/known_hosts"' in bootstrap
    for option in ("IdentitiesOnly=yes", "ForwardAgent=no", "ForwardX11=no"):
        assert bootstrap[bootstrap.index(option) - 1] == "-o"
    assert "without verification" not in capsys.readouterr().err


def test_fingerprint_mismatch_stops_before_registration(tmp_path: Path) -> None:
    from offbox.host.init import InitOptions, init_host

    runner, key = bootstrap_runner(tmp_path)
    expected = "SHA256:" + "A" * 43
    with pytest.raises(PreconditionError, match="fingerprint mismatch") as caught:
        init_host(paths_at(tmp_path), runner, InitOptions(target="dev@192.0.2.1", ssh_key=key, host_key_fingerprint=expected))
    assert expected in caught.value.message and scanned_fingerprint() in caught.value.message
    assert [call.argv[0] for call in runner.calls] == ["ssh-keyscan"]
    assert not (tmp_path / "state/hosts").exists()


def test_blind_host_key_acceptance_recommends_fingerprint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from offbox.host.init import InitOptions, init_host

    runner, key = bootstrap_runner(tmp_path)
    init_host(
        paths_at(tmp_path),
        runner,
        InitOptions(target="dev@192.0.2.1", ssh_key=key, accept_host_key=True),
        ssh_config=tmp_path / "ssh/config",
        clock=FakeClock(),
        probe=lambda _a, _p, _t: True,
    )
    assert f"prefer --host-key-fingerprint {scanned_fingerprint()}" in capsys.readouterr().err


def test_malformed_fingerprint_is_a_usage_error() -> None:
    from offbox.errors import UsageError
    from offbox.host.init import InitOptions

    with pytest.raises(UsageError, match="Invalid host key fingerprint"):
        InitOptions(target="dev@192.0.2.1", host_key_fingerprint="MD5:aa:bb")
