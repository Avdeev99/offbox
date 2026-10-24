import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeRunner, Script
from pydantic import TypeAdapter

from offbox.errors import CommandFailed, ConnectionError_, PreconditionError
from offbox.logging import configure
from offbox.paths import Paths
from offbox.ssh import include_line, remove_include
from offbox.state import HostState, State, StateStore
from offbox.uninstall import uninstall


def context(root: Path, *, host: bool = True) -> tuple[Paths, Path]:
    paths = Paths(root / "config", root / "state", root / "cache", root / "control")
    for path in (paths.config, paths.state, paths.cache, paths.control):
        path.mkdir()
    for name in ("config.toml", "credentials.toml", "ssh_config"):
        (paths.config / name).write_text(name)
    (paths.config / "providers/custom").mkdir(parents=True)
    (paths.config / "providers/custom/provider.toml").write_text("user provider")
    ssh_config = root / "ssh-config"
    ssh_config.write_bytes(
        b"Host user\r\n  HostName example.com\r\n"
        + include_line(paths).encode()
        + b"\r\n# offbox stays in a user comment\nInclude other-offbox-config\n"
    )
    hosts: dict[str, HostState] = {}
    if host:
        hosts["main"] = HostState(
            name="main",
            provider="ssh",
            address="192.0.2.1",
            ssh_user="offbox",
            created_at=datetime.now(UTC),
            known_hosts_path=root / "known_hosts",
        )
    StateStore(paths.state).write(State(hosts=hosts))
    return paths, ssh_config


def session(root: Path) -> dict[str, object]:
    return {
        "identifier": "sync_orphan",
        "version": 1,
        "creationTime": "2026-10-23T12:00:00Z",
        "creatingVersion": "0.18.1",
        "name": "offbox-orphan-abcd",
        "labels": {"offbox-env": "orphan-abcd"},
        "paused": False,
        "status": "watching",
        "alpha": {"protocol": "local", "path": str(root / "orphan"), "connected": True},
        "beta": {"protocol": "ssh", "host": "offbox-deleted", "path": "/srv/offbox/envs/orphan-abcd/work", "connected": True},
    }


def test_uninstall_requires_explicit_yes_when_hosts_remain(tmp_path: Path) -> None:
    paths, ssh_config = context(tmp_path)
    runner = FakeRunner()
    with pytest.raises(PreconditionError) as caught:
        uninstall(paths, runner, ssh_config=ssh_config)
    assert caught.value.fix == "Run offbox host destroy main first, or offbox uninstall --yes to retain the hosts."
    assert StateStore(paths.state).read().hosts["main"].address == "192.0.2.1"
    assert include_line(paths).encode() in ssh_config.read_bytes()
    assert runner.calls == []


def test_uninstall_removes_owned_local_resources_only_and_terminates_orphan_by_identifier(tmp_path: Path) -> None:
    paths, ssh_config = context(tmp_path)
    configure(paths, ["offbox", "uninstall", "--yes"])
    foreign = session(tmp_path)
    foreign.update({"identifier": "sync_foreign", "name": "offbox-user-session", "labels": {"offbox-env": "unrelated-abcd"}})
    runner = FakeRunner(
        [
            Script(prefix=["mutagen", "sync", "list"], stdout=json.dumps([session(tmp_path), foreign])),
            Script(prefix=["mutagen", "sync", "terminate", "sync_orphan"]),
            Script(prefix=["ssh", "-O", "check"], stderr="Master running (pid=100)"),
            Script(prefix=["ssh", "-O", "exit"], stderr="Exit request sent."),
        ]
    )
    uninstall(paths, runner, yes=True, ssh_config=ssh_config)
    assert not paths.state.exists() and not paths.cache.exists() and not paths.control.exists()
    assert not (paths.config / "ssh_config").exists()
    assert paths.user_config.read_text() == "config.toml"
    assert paths.credentials.read_text() == "credentials.toml"
    assert (paths.config / "providers/custom/provider.toml").read_text() == "user provider"
    assert (
        ssh_config.read_bytes() == b"Host user\r\n  HostName example.com\r\n# offbox stays in a user comment\nInclude other-offbox-config\n"
    )
    assert [call.argv for call in runner.calls] == [
        ["mutagen", "sync", "list", "--label-selector", "offbox-env", "--template", "{{ json . }}"],
        ["mutagen", "sync", "terminate", "sync_orphan"],
        ["ssh", "-O", "check", "offbox-main"],
        ["ssh", "-O", "exit", "offbox-main"],
    ]


@pytest.mark.parametrize("phase", ["sync", "check", "exit"])
def test_cleanup_failure_preserves_local_recovery_resources(tmp_path: Path, phase: str) -> None:
    paths, ssh_config = context(tmp_path)
    entries = [Script(prefix=["mutagen", "sync", "list"], stdout="[]")]
    if phase == "sync":
        entries[0].returncode, entries[0].stderr = 1, "RPC failure"
    else:
        entries.append(
            Script(
                prefix=["ssh", "-O", "check"],
                returncode=255 if phase == "check" else 0,
                stderr="Control socket connect(/tmp/control): Permission denied" if phase == "check" else "Master running (pid=100)",
            )
        )
        if phase == "exit":
            entries.append(Script(prefix=["ssh", "-O", "exit"], returncode=255, stderr="Master refused termination request"))
    with pytest.raises(CommandFailed if phase == "sync" else ConnectionError_):
        uninstall(paths, FakeRunner(entries), yes=True, ssh_config=ssh_config)
    assert paths.state.is_dir() and paths.cache.is_dir() and paths.control.is_dir()
    assert (paths.config / "ssh_config").read_text() == "ssh_config"
    assert include_line(paths).encode() in ssh_config.read_bytes()


@pytest.mark.parametrize(
    "endpoint,field,value",
    [
        ("alpha", "host", "foreign"),
        ("alpha", "user", "root"),
        ("alpha", "parameters", {"x": "y"}),
        ("beta", "user", "root"),
        ("beta", "port", 2222),
        ("beta", "environment", {"SSH": "foreign"}),
        ("beta", "host", "offbox-INVALID"),
    ],
)
def test_uninstall_does_not_terminate_labelled_sessions_with_foreign_endpoint_overrides(
    tmp_path: Path, endpoint: str, field: str, value: object
) -> None:
    paths, ssh_config = context(tmp_path, host=False)
    candidate = session(tmp_path)
    candidate[endpoint] = {**TypeAdapter(dict[str, object]).validate_python(candidate[endpoint]), field: value}
    runner = FakeRunner([Script(prefix=["mutagen", "sync", "list"], stdout=json.dumps([candidate]))])
    with pytest.raises(PreconditionError):
        uninstall(paths, runner, ssh_config=ssh_config)
    assert paths.state.exists()
    assert len(runner.calls) == 1


@pytest.mark.parametrize("kind", ["symlink", "config-inside-state", "cache-inside-config"])
def test_uninstall_refuses_unsafe_or_overlapping_owned_directories(tmp_path: Path, kind: str) -> None:
    paths, ssh_config = context(tmp_path, host=False)
    if kind == "symlink":
        paths.cache.rmdir()
        paths.cache.symlink_to(paths.config, target_is_directory=True)
    elif kind == "config-inside-state":
        paths = Paths(paths.state / "config", paths.state, paths.cache, paths.control)
    else:
        paths = Paths(paths.config, paths.state, paths.config / "providers", paths.control)
    runner = FakeRunner()
    with pytest.raises(PreconditionError):
        uninstall(paths, runner, ssh_config=ssh_config)
    assert runner.calls == []
    assert (tmp_path / "config/credentials.toml").read_text() == "credentials.toml"


def test_remove_include_preserves_other_lines_and_exact_bytes(tmp_path: Path) -> None:
    paths = Paths(tmp_path / 'a "quoted" % config', tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    config = tmp_path / "ssh-config"
    owned = include_line(paths).encode()
    others = b"Include user\r\n  " + owned + b"\n# " + owned + b"\r\nHost no-final-newline"
    config.write_bytes(owned + b"\r\n" + others)
    remove_include(paths, config)
    assert config.read_bytes() == others


def test_uninstall_keeps_resources_when_state_changes_during_network_cleanup(tmp_path: Path) -> None:
    import fcntl
    from collections.abc import Mapping, Sequence

    from offbox.runner import Result

    paths, ssh_config = context(tmp_path, host=False)
    store = StateStore(paths.state)

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
            with store.transaction() as state:
                state.default_host = "concurrent-change"
            return super().run(
                argv, check=check, capture=capture, input=input, env=env, cwd=cwd, timeout=timeout, interruptible=interruptible
            )

    runner = ConcurrentRunner([Script(prefix=["mutagen", "sync", "list"], stdout="[]")])
    with pytest.raises(PreconditionError, match="Local state changed"):
        uninstall(paths, runner, ssh_config=ssh_config)
    assert paths.cache.is_dir() and paths.control.is_dir()
    assert store.read().default_host == "concurrent-change"
    assert include_line(paths).encode() in ssh_config.read_bytes()


def test_directory_cleanup_failure_preserves_alias_context_for_successful_second_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil
    from collections.abc import Mapping, Sequence

    from offbox.runner import Result

    paths, ssh_config = context(tmp_path)
    runner = FakeRunner(
        [
            Script(prefix=["mutagen", "sync", "list"], stdout="[]"),
            Script(prefix=["ssh", "-O", "check"], stderr="Master running (pid=100)"),
            Script(prefix=["ssh", "-O", "exit"]),
            Script(prefix=["mutagen", "sync", "list"], stdout="[]"),
            Script(prefix=["ssh", "-O", "check"], returncode=255, stderr="Control socket connect(/tmp/control): No such file or directory"),
        ]
    )
    original_run = runner.run

    def run(
        argv: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Result:
        if argv[:3] == ["ssh", "-O", "check"] and (
            include_line(paths).encode() not in ssh_config.read_bytes() or not (paths.config / "ssh_config").exists()
        ):
            return Result(argv=list(argv), returncode=255, stderr="No ControlPath specified for -O command")
        return original_run(argv, check=check, capture=capture, input=input, env=env, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(runner, "run", run)
    original_rmtree = shutil.rmtree
    blocked = [True]

    def remove(path: Path) -> None:
        if path == paths.cache and blocked[0]:
            raise PermissionError("synthetic cache permission failure")
        original_rmtree(path)

    monkeypatch.setattr("offbox.uninstall.shutil.rmtree", remove)
    with pytest.raises(PreconditionError, match="Cannot remove offbox local resources"):
        uninstall(paths, runner, yes=True, ssh_config=ssh_config)
    assert StateStore(paths.state).read().hosts["main"].address == "192.0.2.1"
    assert include_line(paths).encode() in ssh_config.read_bytes()
    assert (paths.config / "ssh_config").read_text() == "ssh_config"
    blocked[0] = False
    uninstall(paths, runner, yes=True, ssh_config=ssh_config)
    assert not paths.state.exists() and not paths.cache.exists() and not paths.control.exists()
    assert include_line(paths).encode() not in ssh_config.read_bytes()
    assert runner.scripts == []


def test_ssh_include_write_failure_after_directory_cleanup_can_be_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.ssh import write_private

    paths, ssh_config = context(tmp_path)
    runner = FakeRunner(
        [
            Script(prefix=["mutagen", "sync", "list"], stdout="[]"),
            Script(prefix=["ssh", "-O", "check"], stderr="Master running (pid=100)"),
            Script(prefix=["ssh", "-O", "exit"]),
            Script(prefix=["mutagen", "sync", "list"], stdout="[]"),
        ]
    )
    blocked = [True]

    def write(path: Path, content: str) -> None:
        if blocked[0]:
            raise PreconditionError("Cannot write SSH configuration", "Correct its permissions and retry uninstall.")
        write_private(path, content)

    monkeypatch.setattr("offbox.ssh.write_private", write)
    with pytest.raises(PreconditionError, match="Cannot write SSH configuration"):
        uninstall(paths, runner, yes=True, ssh_config=ssh_config)
    assert not paths.state.exists()
    assert include_line(paths).encode() in ssh_config.read_bytes()
    assert (paths.config / "ssh_config").exists()
    blocked[0] = False
    uninstall(paths, runner, ssh_config=ssh_config)
    assert not paths.state.exists()
    assert include_line(paths).encode() not in ssh_config.read_bytes()
    assert not (paths.config / "ssh_config").exists()
    assert runner.scripts == []


def _add_cloud_host(paths: Paths, *, address: str = "198.51.100.7") -> HostState:
    workspace = paths.state / "hosts/cloud/tofu"
    workspace.mkdir(parents=True)
    (workspace / "provider.toml").write_text('name="hetzner"\ndescription="cloud"\ndriver="tofu"\n')
    host = HostState(
        name="cloud",
        provider="hetzner",
        address=address,
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        provider_dir=workspace,
        known_hosts_path=workspace.parent / "known_hosts",
    )
    with StateStore(paths.state).transaction() as state:
        state.hosts[host.name] = host
    return host


def test_uninstall_yes_refuses_while_cloud_hosts_remain(tmp_path: Path) -> None:
    paths, ssh_config = context(tmp_path)
    host = _add_cloud_host(paths)
    runner = FakeRunner()
    with pytest.raises(PreconditionError) as caught:
        uninstall(paths, runner, yes=True, ssh_config=ssh_config)
    assert caught.value.message == (
        "Cloud hosts remain and keep billing after uninstall: cloud (hetzner, 198.51.100.7, label offbox-host=cloud)"
    )
    assert caught.value.fix == (
        "Run offbox host destroy cloud --yes first, or add --abandon-cloud-hosts to keep them running without local records."
    )
    assert StateStore(paths.state).read().hosts["cloud"] == host
    assert paths.cache.is_dir()
    assert runner.calls == []


def test_uninstall_without_yes_points_cloud_users_to_the_abandon_flag(tmp_path: Path) -> None:
    paths, ssh_config = context(tmp_path, host=False)
    _add_cloud_host(paths, address="")
    with pytest.raises(PreconditionError) as caught:
        uninstall(paths, FakeRunner(), ssh_config=ssh_config)
    assert caught.value.fix == "Run offbox host destroy cloud first, or offbox uninstall --yes --abandon-cloud-hosts to retain the hosts."


def test_uninstall_abandons_cloud_hosts_only_with_explicit_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths, ssh_config = context(tmp_path, host=False)
    _add_cloud_host(paths)
    runner = FakeRunner(
        [
            Script(prefix=["mutagen", "sync", "list"], stdout="[]"),
            Script(prefix=["ssh", "-O", "check"], returncode=255, stderr="Control socket connect(/tmp/control): No such file or directory"),
        ]
    )
    uninstall(paths, runner, yes=True, abandon_cloud_hosts=True, ssh_config=ssh_config)
    assert not paths.state.exists()
    err = " ".join(capsys.readouterr().err.split())
    assert "Abandoning cloud host cloud (hetzner, 198.51.100.7, label offbox-host=cloud)" in err


def test_uninstall_cli_passes_the_abandon_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from offbox.cli import app
    from offbox.runner import Runner

    paths, _ssh_config = context(tmp_path, host=False)
    seen: list[tuple[bool, bool]] = []

    def run_uninstall(_paths: Paths, _runner: Runner, *, yes: bool = False, abandon_cloud_hosts: bool = False) -> None:
        seen.append((yes, abandon_cloud_hosts))

    monkeypatch.setattr("offbox.cli.misc.Paths.discover", lambda: paths)
    monkeypatch.setattr("offbox.cli.misc.run_uninstall", run_uninstall)
    result = CliRunner().invoke(app, ["uninstall", "--yes", "--abandon-cloud-hosts"])
    assert result.exit_code == 0
    assert seen == [(True, True)]
