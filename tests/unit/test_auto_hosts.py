import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeRunner, Script
from pydantic import ValidationError

from offbox.config import EnvConfig, HostPreset, duration_seconds
from offbox.errors import OffboxError, PreconditionError
from offbox.host import auto
from offbox.host.activity import forget, last_activity, touch, worktree_gone
from offbox.host.auto import (
    ReapOutcome,
    clear_lost,
    ensure_host,
    lost_environment_hint,
    read_history,
    reap,
    reap_decision,
    release_if_unused,
)
from offbox.host.create import CreateOptions
from offbox.host.usage import UsageHolder, exclusive_usage, holders, shared_usage
from offbox.host.warnings import dismiss_warning, read_warnings
from offbox.paths import Paths
from offbox.runner import Runner
from offbox.state import EnvState, HostState, State, StateStore

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def paths_at(root: Path) -> Paths:
    paths = Paths(root / "config", root / "state", root / "cache", root / "control")
    paths.config.mkdir(parents=True, exist_ok=True)
    return paths


def host(
    name: str = "agg", *, auto_managed: bool = True, idle: int | None = 7200, phase: str = "ready", created: datetime = NOW
) -> HostState:
    return HostState.model_validate(
        {
            "name": name,
            "provider": "hetzner",
            "address": "192.0.2.1" if phase == "ready" else "",
            "ssh_user": "offbox",
            "created_at": created,
            "known_hosts_path": Path("/state/known_hosts"),
            "phase": phase,
            "auto": auto_managed,
            "idle_seconds": idle,
        }
    )


def env(name: str, worktree: Path, host_name: str = "agg", created: datetime = NOW) -> EnvState:
    return EnvState(name=name, host=host_name, worktree_path=worktree, created_at=created)


def write_state(paths: Paths, hosts: list[HostState], envs: list[EnvState] | None = None, default: str | None = None) -> StateStore:
    store = StateStore(paths.state)
    store.write(State(hosts={item.name: item for item in hosts}, envs={item.name: item for item in envs or []}, default_host=default))
    return store


def test_durations_parse_and_lifecycle_presets_validate() -> None:
    assert [duration_seconds(value) for value in ("30m", "2h", "1d", "off")] == [1800, 7200, 86400, None]
    for invalid in ("0h", "2", "2w", "1.5h", ""):
        with pytest.raises(ValueError, match="duration"):
            duration_seconds(invalid)
    with pytest.raises(ValidationError, match="existing SSH servers"):
        HostPreset(provider="ssh", auto=True)
    with pytest.raises(ValidationError, match="duration"):
        HostPreset(auto=True, idle="soon")
    assert HostPreset().auto is False
    assert EnvConfig(host="dev-host").host == "dev-host"
    with pytest.raises(ValidationError):
        EnvConfig(host="Bad Name")


@pytest.mark.parametrize(
    ("record", "activity", "free", "destroy", "reason"),
    [
        (host(auto_managed=False), NOW - timedelta(days=9), True, False, "manual host"),
        (host(idle=None), NOW - timedelta(days=9), True, False, "idle reaping is off"),
        (host(), NOW - timedelta(hours=3), True, True, "idle for 3h00m"),
        (host(), NOW - timedelta(minutes=10), True, False, "last used 10m ago; reaped after 2h00m idle"),
        (host(phase="creating", created=NOW - timedelta(hours=1)), NOW, False, False, "creation in progress"),
        (host(phase="creating", created=NOW - timedelta(hours=1)), NOW, True, True, "creation did not finish"),
        (host(phase="initializing", created=NOW - timedelta(minutes=5)), NOW, True, False, "creation started recently"),
    ],
)
def test_reap_decision(record: HostState, activity: datetime, free: bool, destroy: bool, reason: str) -> None:
    decision = reap_decision(record, activity, NOW, operation_free=free)
    assert (decision.destroy, decision.reason) == (destroy, reason)


def test_idle_reaping_ignores_worktrees_that_still_exist_and_uses_the_host_snapshot(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    worktree = tmp_path / "repo"
    worktree.mkdir()
    paths.user_config.write_text('[hosts.agg]\nauto = true\nidle = "30d"\n')
    old = NOW - timedelta(hours=2)
    state = State(hosts={"agg": host(idle=3600, created=old)}, envs={"repo-abcd": env("repo-abcd", worktree, created=old)})
    activity = auto.host_activity(paths, state, state.hosts["agg"])
    assert reap_decision(state.hosts["agg"], activity, NOW, operation_free=True).destroy is True


def test_activity_files_track_use_without_touching_state(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    record = env("repo-abcd", tmp_path, created=NOW - timedelta(days=1))
    assert last_activity(paths, record) == record.created_at
    touch(paths, "repo-abcd", NOW)
    assert last_activity(paths, record) == NOW
    assert not (paths.state / "state.json").exists()
    forget(paths, "repo-abcd")
    assert last_activity(paths, record) == record.created_at
    with pytest.raises(PreconditionError, match="Invalid environment name"):
        touch(paths, "../escape", NOW)


def test_only_removed_worktrees_count_as_gone(tmp_path: Path) -> None:
    present = tmp_path / "present"
    present.mkdir()
    assert worktree_gone(present) is False
    assert worktree_gone(tmp_path / "removed") is True
    assert worktree_gone(tmp_path / "unmounted-volume" / "repo") is False


def test_shared_holders_coexist_and_block_exclusive_destroy(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    with shared_usage(paths, "agg", "offbox exec", now=lambda: NOW), shared_usage(paths, "agg", "offbox shell", now=lambda: NOW):
        assert sorted(holder.command for holder in holders(paths, "agg")) == ["offbox exec", "offbox shell"]
        with exclusive_usage(paths, "agg") as free:
            assert free is False
    assert holders(paths, "agg") == []
    with exclusive_usage(paths, "agg") as free:
        assert free is True
        with (
            pytest.raises(PreconditionError, match="Host agg is being destroyed after waiting 1s"),
            shared_usage(paths, "agg", "offbox exec", wait=1, clock=FakeClock()),
        ):
            pass


def test_dead_and_unreadable_holders_are_skipped(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    directory = paths.state / "host-locks" / "agg.users"
    directory.mkdir(parents=True)
    (directory / "dead.json").write_text(UsageHolder(pid=2**22 + 12345, command="gone", started_at=NOW).model_dump_json())
    (directory / "bad.json").write_text("{")
    (directory / "live.json").write_text(UsageHolder(pid=os.getpid(), command="offbox up", started_at=NOW).model_dump_json())
    assert [holder.command for holder in holders(paths, "agg")] == ["offbox up"]
    assert not (directory / "dead.json").exists()


def recorded_create(paths: Paths, calls: list[CreateOptions]) -> object:
    def create(paths_: Paths, runner: Runner, options: CreateOptions, **_: object) -> HostState:
        calls.append(options)
        created = host(options.name, idle=options.idle_seconds)
        with StateStore(paths.state).transaction() as state:
            state.hosts[created.name] = created
        return created

    return create


def test_ensure_host_creates_an_auto_preset_once_without_making_it_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = paths_at(tmp_path)
    paths.user_config.write_text('[hosts.agg]\nauto = true\nidle = "90m"\n')
    calls: list[CreateOptions] = []
    monkeypatch.setattr("offbox.host.create.create_host_locked", recorded_create(paths, calls))
    first = ensure_host(paths, FakeRunner(), "agg", clock=FakeClock(), timer_installed=lambda _paths: False)
    second = ensure_host(paths, FakeRunner(), "agg", clock=FakeClock())
    assert first == second
    assert [(call.name, call.auto, call.idle_seconds) for call in calls] == [("agg", True, 5400)]
    err = " ".join(capsys.readouterr().err.split())
    assert "Creating auto-managed host agg: billable until destroyed; destroyed after 1h30m idle" in err
    assert "Idle reaping needs the timer: offbox host reap --install-timer" in err


def test_ensure_host_joins_a_host_another_process_created_while_it_waited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = paths_at(tmp_path)
    paths.user_config.write_text("[hosts.agg]\nauto = true\n")
    calls: list[CreateOptions] = []
    monkeypatch.setattr("offbox.host.create.create_host_locked", recorded_create(paths, calls))

    @contextmanager
    def raced(paths_: Paths, name: str, **_: object) -> Generator[None]:
        write_state(paths, [host(name)])
        yield

    monkeypatch.setattr(auto, "host_operation", raced)
    assert ensure_host(paths, FakeRunner(), "agg").name == "agg"
    assert calls == []


@pytest.mark.parametrize(
    ("preset", "registered", "message"),
    [("", None, "Unknown host: agg"), ("[hosts.agg]\nauto = true\n", "creating", "did not finish creating")],
)
def test_ensure_host_refuses_unknown_hosts_and_crashed_creations(tmp_path: Path, preset: str, registered: str | None, message: str) -> None:
    paths = paths_at(tmp_path)
    paths.user_config.write_text(preset)
    if registered:
        write_state(paths, [host(phase=registered)])
    with pytest.raises(PreconditionError, match=message):
        ensure_host(paths, FakeRunner(), "agg", clock=FakeClock())


def recorded_destroy(paths: Paths, destroyed: list[str], *, fail: set[str] | None = None) -> object:
    def destroy(paths_: Paths, runner: Runner, name: str, **_: object) -> None:
        if name in (fail or set()):
            raise OffboxError("tofu destroy failed", "Retry offbox host reap.")
        destroyed.append(name)
        with StateStore(paths.state).transaction() as state:
            state.hosts.pop(name, None)
            state.envs = {key: value for key, value in state.envs.items() if value.host != name}

    return destroy


def test_last_rm_destroys_only_auto_hosts_without_live_worktrees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = paths_at(tmp_path)
    live = tmp_path / "live"
    live.mkdir()
    destroyed: list[str] = []
    monkeypatch.setattr(auto, "destroy_host_locked", recorded_destroy(paths, destroyed))
    write_state(paths, [host("agg"), host("manual", auto_managed=False)], [env("gone-abcd", tmp_path / "removed")])
    assert release_if_unused(paths, FakeRunner(), "manual") is False
    assert release_if_unused(paths, FakeRunner(), "agg", clock=FakeClock()) is True
    assert destroyed == ["agg"]
    write_state(paths, [host("agg")], [env("live-abcd", live)])
    assert release_if_unused(paths, FakeRunner(), "agg", clock=FakeClock()) is False
    assert destroyed == ["agg"]


def test_busy_last_rm_waits_then_names_holders_and_fixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = paths_at(tmp_path)
    destroyed: list[str] = []
    monkeypatch.setattr(auto, "destroy_host_locked", recorded_destroy(paths, destroyed))
    write_state(paths, [host()])
    clock = FakeClock()
    with shared_usage(paths, "agg", "offbox logs --stack", now=lambda: NOW):
        assert release_if_unused(paths, FakeRunner(), "agg", clock=clock, timer_installed=lambda _paths: False) is False
    assert destroyed == []
    assert clock.value >= auto.RELEASE_WAIT_SECONDS
    err = " ".join(capsys.readouterr().err.split())
    assert f"Host agg is still in use by: offbox logs --stack (pid {os.getpid()})" in err
    assert "Fix: run offbox host reap agg once they finish, or install the idle reaper with offbox host reap --install-timer." in err


def test_reap_continues_past_failures_records_by_source_and_explains_lost_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = paths_at(tmp_path)
    destroyed: list[str] = []
    monkeypatch.setattr(auto, "destroy_host_locked", recorded_destroy(paths, destroyed, fail={"aaa"}))
    old = NOW - timedelta(hours=5)
    (tmp_path / "repo").mkdir()
    write_state(
        paths,
        [host("aaa", created=old), host("bbb", created=old), host("ccc"), host("manual", auto_managed=False, created=old)],
        [env("repo-abcd", tmp_path / "repo", "bbb", created=old)],
    )
    planned = reap(paths, FakeRunner(), dry_run=True, now=lambda: NOW, clock=FakeClock())
    assert [(item.name, item.action) for item in planned.hosts] == [("aaa", "destroy"), ("bbb", "destroy"), ("ccc", "destroy")]
    assert destroyed == [] and read_history(paths).manual is None
    report = reap(paths, FakeRunner(), source="timer", now=lambda: NOW, clock=FakeClock())
    assert [(item.name, item.action) for item in report.hosts] == [("aaa", "failed"), ("bbb", "destroyed"), ("ccc", "destroyed")]
    assert report.hosts[0].error == "tofu destroy failed Fix: Retry offbox host reap."
    assert report.hosts[1] == ReapOutcome(name="bbb", action="destroyed", reason="idle for 5h00m", lost_environments=["repo-abcd"])
    assert report.exit_code == 1
    history = read_history(paths)
    assert history.manual is None and history.timer is not None and history.timer.report == report
    assert "host bbb was reaped" in (lost_environment_hint(paths, "repo-abcd") or "")
    assert auto.timer_status(paths)[1] == "aaa: tofu destroy failed Fix: Retry offbox host reap."
    retry = reap(paths, FakeRunner(), "manual", now=lambda: NOW, clock=FakeClock())
    assert [(item.action, item.reason) for item in retry.hosts] == [("skipped", "manual host")]


def test_reap_destroys_empty_auto_hosts_even_with_idle_reaping_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = paths_at(tmp_path)
    destroyed: list[str] = []
    monkeypatch.setattr(auto, "destroy_host_locked", recorded_destroy(paths, destroyed))
    write_state(paths, [host("empty", idle=None), host("busy", idle=None)], [env("live-abcd", tmp_path, "busy")])
    report = reap(paths, FakeRunner(), now=lambda: NOW, clock=FakeClock())
    assert [(item.name, item.action, item.reason) for item in report.hosts] == [
        ("busy", "kept", "idle reaping is off"),
        ("empty", "destroyed", "no environments use it"),
    ]
    assert destroyed == ["empty"]


def test_reaped_hint_is_cleared_once_the_environment_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = paths_at(tmp_path)
    monkeypatch.setattr(auto, "destroy_host_locked", recorded_destroy(paths, []))
    write_state(paths, [host(created=NOW - timedelta(days=1))], [env("repo-abcd", tmp_path, created=NOW - timedelta(days=1))])
    reap(paths, FakeRunner(), now=lambda: NOW, clock=FakeClock())
    assert lost_environment_hint(paths, "repo-abcd") is not None
    clear_lost(paths, "repo-abcd")
    assert lost_environment_hint(paths, "repo-abcd") is None


def test_rm_destroy_is_recorded_for_later_hints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = paths_at(tmp_path)
    monkeypatch.setattr(auto, "destroy_host_locked", recorded_destroy(paths, []))
    write_state(
        paths,
        [host("old", created=NOW - timedelta(days=1)), host()],
        [env("old-abcd", tmp_path, "old", created=NOW - timedelta(days=1)), env("gone-abcd", tmp_path / "removed")],
    )
    reap(paths, FakeRunner(), "old", now=lambda: NOW, clock=FakeClock())
    assert release_if_unused(paths, FakeRunner(), "agg", clock=FakeClock()) is True
    assert "(last environment removed)" in (lost_environment_hint(paths, "gone-abcd") or "")
    assert "host old was reaped" in (lost_environment_hint(paths, "old-abcd") or "")


def forgettable_host(paths: Paths, *, interrupted: bool = False) -> HostState:
    workspace = paths.state / "hosts/main/provider"
    workspace.mkdir(parents=True)
    (workspace / "provider.toml").write_text('name="ssh"\ndescription="existing"\ndriver="none"\n')
    return HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=NOW,
        provider_dir=workspace,
        known_hosts_path=workspace.parent / "known_hosts",
        create_interrupted=interrupted,
    )


def forget_runner() -> FakeRunner:
    return FakeRunner([Script(prefix=["ssh", "-O", "check"], returncode=255, stderr="Control socket connect(/tmp/control): No such file")])


def test_auto_hosts_never_become_the_default(tmp_path: Path) -> None:
    from offbox.cli.host import use_host
    from offbox.host.destroy import destroy_host

    paths = paths_at(tmp_path)
    write_state(paths, [host("agg"), forgettable_host(paths)], default="main")
    with pytest.raises(PreconditionError, match="auto-managed and cannot be the default") as caught:
        use_host(paths, "agg")
    assert caught.value.fix == "Select it per project with env.host instead."
    destroy_host(paths, forget_runner(), "main", forget=True)
    state = StateStore(paths.state).read()
    assert (sorted(state.hosts), state.default_host) == (["agg"], None)


def test_destroying_an_interrupted_creation_keeps_a_dismissable_warning(tmp_path: Path) -> None:
    from offbox.host.destroy import destroy_host

    paths = paths_at(tmp_path)
    write_state(paths, [forgettable_host(paths, interrupted=True)])
    destroy_host(paths, forget_runner(), "main", forget=True)
    assert [warning.message for warning in read_warnings(paths)] == [
        "Creation of main was interrupted; check the provider console for resources labelled offbox-host=main"
    ]


def test_host_destroy_refuses_a_host_in_use(tmp_path: Path) -> None:
    from offbox.host.destroy import destroy_host

    paths = paths_at(tmp_path)
    write_state(paths, [host("main", auto_managed=False)])
    with (
        shared_usage(paths, "main", "offbox exec", now=lambda: NOW),
        pytest.raises(PreconditionError, match=r"Host main is in use by: offbox exec \(pid ") as caught,
    ):
        destroy_host(paths, FakeRunner(), "main")
    assert caught.value.fix == "Wait for those commands to finish, then retry."
    assert "main" in StateStore(paths.state).read().hosts


def test_exec_after_a_destroy_finished_during_the_wait_fails_without_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths = paths_at(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    name = worktree_name(root)
    write_state(paths, [host()], [env(name, root)])

    @contextmanager
    def destroyed_while_waiting(paths_: Paths, host_name: str, command: str, **_: object) -> Generator[None]:
        write_state(paths, [])
        yield

    monkeypatch.setattr("offbox.runtime.shared_usage", destroyed_while_waiting)
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root))])
    with (
        pytest.raises(PreconditionError, match="Environment is not registered"),
        Runtime(paths, runner, FakeClock()).using("offbox exec", root),
    ):
        pass
    assert [call.argv[0] for call in runner.calls] == ["git"]


def test_env_host_mismatch_message() -> None:
    from offbox.runtime import env_host_mismatch

    recorded = env("repo-abcd", Path("/repo"), "main")
    assert env_host_mismatch(None, "agg") is None
    assert env_host_mismatch(recorded, None) is None
    assert env_host_mismatch(recorded, "main") is None
    assert (
        env_host_mismatch(recorded, "agg") == "This environment runs on main; env.host says agg. Run offbox rm, then offbox up to move it."
    )


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_doctor_reports_a_timer_whose_executable_is_gone(tmp_path: Path, system: str) -> None:
    from offbox.host import timer

    paths = paths_at(tmp_path)
    files = timer.unit_files(paths, tmp_path, system)
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
    gone = tmp_path / "gone/offbox"
    if system == "Darwin":
        files[0].write_text(timer.render_launchd(gone, {"HOME": str(tmp_path)}, tmp_path / "log"))
    else:
        service, unit = timer.render_systemd(gone, {"HOME": str(tmp_path)})
        files[0].write_text(service)
        files[1].write_text(unit)
    assert timer.unit_problem(paths, home=tmp_path, system=system) == f"The idle reaper runs {gone}, which no longer exists"
    gone.parent.mkdir()
    gone.write_text("")
    assert timer.unit_problem(paths, home=tmp_path, system=system) is None


def test_systemd_values_escape_specifiers() -> None:
    from offbox.host.timer import render_systemd

    service, _ = render_systemd(Path("/opt/100%/offbox"), {"PATH": "/a$b"})
    assert 'Environment="PATH=/a$$b"' in service
    assert 'ExecStart="/opt/100%%/offbox" host reap --from-timer' in service


def test_reap_skips_hosts_in_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = paths_at(tmp_path)
    destroyed: list[str] = []
    monkeypatch.setattr(auto, "destroy_host_locked", recorded_destroy(paths, destroyed))
    write_state(paths, [host(created=NOW - timedelta(days=1))])
    with shared_usage(paths, "agg", "offbox exec", now=lambda: NOW):
        report = reap(paths, FakeRunner(), now=lambda: NOW, clock=FakeClock())
    assert report.hosts[0].action == "skipped"
    assert report.hosts[0].reason.startswith("in use by offbox exec (pid ")
    assert destroyed == []


def test_interrupted_creations_are_reaped_and_warnings_dismissed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.host.warnings import warn_interrupted

    paths = paths_at(tmp_path)
    destroyed: list[str] = []
    monkeypatch.setattr(auto, "destroy_host_locked", recorded_destroy(paths, destroyed))
    interrupted = host(phase="creating", created=NOW - timedelta(hours=2)).model_copy(update={"create_interrupted": True})
    write_state(paths, [interrupted])
    report = reap(paths, FakeRunner(), now=lambda: NOW, clock=FakeClock())
    assert (report.hosts[0].action, destroyed) == ("destroyed", ["agg"])
    warn_interrupted(paths, "agg")
    warn_interrupted(paths, "agg")
    assert [warning.host for warning in read_warnings(paths)] == ["agg"]
    assert dismiss_warning(paths, "agg") is True
    assert read_warnings(paths) == []
    assert dismiss_warning(paths, "agg") is False


def test_name_collisions_in_the_cloud_get_a_specific_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.host.create import create_host

    paths = paths_at(tmp_path)
    key = tmp_path / "identity"
    key.write_text("private")
    key.with_name("identity.pub").write_text("ssh-ed25519 AAAA")

    def generated_key(_runner: Runner, path: Path) -> tuple[str, str]:
        path.write_text("PRIVATE")
        return "PRIVATE", "ssh-ed25519 BBBB"

    monkeypatch.setattr("offbox.host.create.generate_host_key", generated_key)
    monkeypatch.setenv("HCLOUD_TOKEN", "synthetic-token")
    runner = FakeRunner(
        [
            Script(prefix=["ssh-keygen", "-y"], stdout="ssh-ed25519 AAAA"),
            Script(prefix=["tofu", "init"]),
            Script(prefix=["tofu", "apply"], returncode=1, stderr="Error: name is already used (uniqueness_error)"),
        ]
    )
    with pytest.raises(OffboxError) as caught:
        create_host(
            paths, runner, CreateOptions(name="agg", ssh_key=key, auto=True, idle_seconds=60), executable=Path("tofu"), clock=FakeClock()
        )
    assert caught.value.message == "Creating host agg failed: Cloud resources named offbox-agg already exist in the provider project"
    recorded = StateStore(paths.state).read().hosts["agg"]
    assert (recorded.auto, recorded.idle_seconds, recorded.phase) == (True, 60, "creating")


def test_timer_units_carry_paths_but_never_credentials(tmp_path: Path) -> None:
    from offbox.host.timer import render_launchd, render_systemd

    environment = {"HOME": "/home/me", "PATH": "/opt/bin:/usr/bin", "XDG_STATE_HOME": "/home/me/state & co"}
    plist = render_launchd(Path("/opt/bin/offbox"), environment, Path("/home/me/reaper.log"))
    assert "<string>/opt/bin/offbox</string><string>host</string><string>reap</string><string>--from-timer</string>" in plist
    assert "<key>XDG_STATE_HOME</key><string>/home/me/state &amp; co</string>" in plist
    assert "<key>StartInterval</key><integer>900</integer>" in plist
    service, timer = render_systemd(Path("/opt/bin/offbox"), environment)
    assert service == (
        "[Unit]\nDescription=Destroy idle offbox hosts\n\n[Service]\nType=oneshot\n"
        'Environment="HOME=/home/me"\nEnvironment="PATH=/opt/bin:/usr/bin"\nEnvironment="XDG_STATE_HOME=/home/me/state & co"\n'
        'ExecStart="/opt/bin/offbox" host reap --from-timer\n'
    )
    assert "OnCalendar=*:0/15\nPersistent=true\n" in timer
    assert "TOKEN" not in plist + service


def test_timer_preflight_refuses_a_token_that_exists_only_in_the_shell(tmp_path: Path) -> None:
    from offbox.host.timer import preflight

    paths = paths_at(tmp_path)
    paths.user_config.write_text("[hosts.agg]\nauto = true\n")
    with pytest.raises(PreconditionError, match="cannot read credentials for host agg") as caught:
        preflight(paths, {"HOME": str(tmp_path), "PATH": "/usr/bin"}, tmp_path)
    assert caught.value.fix == f"Store the Hetzner token in {paths.credentials} (mode 0600) instead of only exporting HCLOUD_TOKEN."
    paths.credentials.write_text('[hetzner]\ntoken = "synthetic"\n')
    paths.credentials.chmod(0o600)
    preflight(paths, {"HOME": str(tmp_path), "PATH": "/usr/bin"}, tmp_path)


def test_timer_refuses_ephemeral_executables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    from offbox.host import timer

    ephemeral = tmp_path / "offbox"
    ephemeral.write_text("")
    assert ephemeral.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())

    def which(name: str, path: str | None = None) -> str:
        return str(ephemeral)

    monkeypatch.setattr("offbox.host.timer.shutil.which", which)
    with pytest.raises(PreconditionError, match="needs an installed offbox"):
        timer.executable_path()


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_timer_install_and_remove_use_the_platform_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str) -> None:
    from offbox.host import timer

    paths = paths_at(tmp_path)
    executable = tmp_path / "bin/offbox"
    executable.parent.mkdir()
    executable.write_text("")
    monkeypatch.setattr(timer, "executable_path", lambda: executable)
    calls = (
        [Script(prefix=["launchctl", "bootout"]), Script(prefix=["launchctl", "bootstrap"]), Script(prefix=["launchctl", "bootout"])]
        if system == "Darwin"
        else [
            Script(prefix=["systemctl", "--user", "daemon-reload"]),
            Script(prefix=["systemctl", "--user", "enable"]),
            Script(prefix=["systemctl", "--user", "disable"]),
            Script(prefix=["systemctl", "--user", "daemon-reload"]),
        ]
    )
    runner = FakeRunner(calls)
    files = timer.install(paths, runner, home=tmp_path, system=system, environ={"PATH": "/usr/bin"})
    assert all(path.is_file() for path in files)
    assert timer.installed(paths, home=tmp_path, system=system) is True
    assert timer.remove(paths, runner, home=tmp_path, system=system) is True
    assert not any(path.exists() for path in files)
    assert runner.scripts == []
    assert timer.remove(paths, FakeRunner(), home=tmp_path, system=system) is False


def test_timer_health_reports_failures_and_stale_runs(tmp_path: Path) -> None:
    from offbox.host import timer

    paths = paths_at(tmp_path)
    assert timer.health(paths, NOW, None, None, home=tmp_path) is None
    for path in timer.unit_files(paths, tmp_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unit")
    assert timer.health(paths, NOW, NOW - timedelta(minutes=10), None, home=tmp_path) is None
    assert "has not run for over an hour" in (timer.health(paths, NOW, NOW - timedelta(hours=2), None, home=tmp_path) or "")
    assert timer.health(paths, NOW, NOW, "agg: boom", home=tmp_path) == "The last scheduled reap failed: agg: boom"


def test_uninstall_removes_the_timer_before_local_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox import uninstall

    paths = paths_at(tmp_path)
    write_state(paths, [])
    seen: list[bool] = []

    def remove(paths_: Paths, runner: Runner) -> bool:
        seen.append((paths.state / "state.json").exists())
        return True

    monkeypatch.setattr(uninstall, "remove_reaper", remove)

    def remove_include(paths_: Paths, ssh_config: Path | None = None) -> None:
        return None

    monkeypatch.setattr(uninstall, "remove_include", remove_include)
    uninstall.uninstall(paths, FakeRunner([Script(prefix=["mutagen"], stdout="[]")]), yes=True, ssh_config=tmp_path / "ssh_config")
    assert seen == [True]
    assert not paths.state.exists()


def test_using_retries_when_the_host_changes_and_records_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.runtime import Runtime

    paths = paths_at(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    from offbox.naming import worktree_name

    name = worktree_name(root)
    write_state(paths, [host("agg"), host("other")], [env(name, root, "agg")])
    runtime = Runtime(paths, FakeRunner([Script(prefix=["git"], stdout=str(root))]), FakeClock())
    with runtime.using("offbox exec", root) as (work, recorded, selected):
        assert (work.name, recorded.host, selected.name) == (name, "agg", "agg")
        assert [holder.command for holder in holders(paths, "agg")] == ["offbox exec"]
    assert holders(paths, "agg") == []
    assert (paths.state / "activity" / name).is_file()

    flips = iter(["agg", "other", "agg", "other"])
    original = Runtime.registered

    def moving(self: Runtime, work: object) -> EnvState:
        recorded = original(self, work)  # type: ignore[arg-type]  # the test passes the real worktree through
        return recorded.model_copy(update={"host": next(flips)})

    monkeypatch.setattr(Runtime, "registered", moving)
    runtime = Runtime(paths, FakeRunner([Script(prefix=["git"], stdout=str(root))]), FakeClock())
    with pytest.raises(PreconditionError, match="Host selection changed while starting"), runtime.using("offbox exec", root):
        pass


def test_unregistered_environment_explains_a_reaped_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths = paths_at(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    name = worktree_name(root)
    destroyed: list[str] = []
    monkeypatch.setattr(auto, "destroy_host_locked", recorded_destroy(paths, destroyed))
    write_state(paths, [host(created=NOW - timedelta(days=1))], [env(name, root, created=NOW - timedelta(days=1))])
    reap(paths, FakeRunner(), now=lambda: NOW, clock=FakeClock())
    with pytest.raises(PreconditionError) as caught:
        Runtime(paths, FakeRunner([Script(prefix=["git"], stdout=str(root))]), FakeClock()).current(root)
    assert (caught.value.fix or "").startswith("offbox up (host agg was reaped at ")
    assert (caught.value.fix or "").endswith("(idle for 24h00m); its Docker data is gone)")


def test_up_resolves_env_host_and_creates_auto_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from offbox.naming import Worktree, worktree_name
    from offbox.runtime import Runtime

    paths = paths_at(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "offbox.toml").write_text('[env]\nhost = "agg"\n')
    paths.user_config.write_text('default_host = "main"\n[hosts.agg]\nauto = true\n')
    work = Worktree(root=root, name=worktree_name(root))
    write_state(paths, [host("main", auto_managed=False)])
    runtime = Runtime(paths, FakeRunner(), FakeClock())
    assert runtime.up_target(work, None) == "agg"
    assert runtime.up_target(work, "main") == "main"
    created: list[str] = []

    def ensure(paths_: Paths, runner: Runner, name: str, **_: object) -> HostState:
        created.append(name)
        return host(name)

    monkeypatch.setattr("offbox.runtime.ensure_host", ensure)
    assert runtime.ready_host("agg").name == "agg"
    assert runtime.ready_host("main").name == "main"
    assert created == ["agg"]
    write_state(paths, [host("main", auto_managed=False)], [env(work.name, root, "main")])
    assert runtime.up_target(work, None) == "main"
    with pytest.raises(PreconditionError, match="registered on another host"):
        runtime.up_target(work, "agg")


def test_manual_ssh_hosts_survive_the_removal_of_their_last_environment(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    manual = host("main", auto_managed=False)
    write_state(paths, [manual])
    assert release_if_unused(paths, FakeRunner(), "main") is False
    report = reap(paths, FakeRunner(), "main", dry_run=True, now=lambda: NOW, clock=FakeClock())
    assert [(item.action, item.reason) for item in report.hosts] == [("skipped", "manual host")]
    assert StateStore(paths.state).read().hosts == {"main": manual}


def test_reap_cli_prints_decisions_and_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from offbox.cli import app
    from offbox.host.auto import ReapReport

    report = ReapReport(
        dry_run=True,
        hosts=[
            ReapOutcome(name="agg", action="destroy", reason="idle for 3h00m", lost_environments=["repo-abcd"]),
            ReapOutcome(name="old", action="failed", reason="destroy failed", error="boom"),
        ],
    )

    def reap_hosts(*args: object, **kwargs: object) -> ReapReport:
        return report

    monkeypatch.setattr("offbox.cli.host.reap_hosts", reap_hosts)
    monkeypatch.setattr("offbox.cli.host.Paths.discover", lambda: paths_at(tmp_path))
    result = CliRunner().invoke(app, ["host", "reap", "--dry-run"])
    assert result.exit_code == 1
    err = " ".join(result.stderr.split())
    assert "agg: would destroy (idle for 3h00m; environments lost: repo-abcd)" in err
    assert "old: failed (destroy failed)" in err
    json_result = CliRunner().invoke(app, ["host", "reap", "--json"])
    assert ReapReport.model_validate_json(json_result.stdout) == report
