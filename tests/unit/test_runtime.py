import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.config import EnvConfig
from offbox.errors import PreconditionError
from offbox.paths import Paths
from offbox.state import EnvState, HostState, State, StateStore
from offbox.sync import DEFAULT_IGNORES


@pytest.mark.parametrize(
    ("root", "prefix"),
    [("/src/My Repo", "my-repo-"), ("/src/123 PROJECT", "123-project-"), ("/src/☀", "env-"), ("/src/" + "a" * 50, "a" * 32 + "-")],
)
def test_worktree_names_are_stable_safe_and_path_specific(root: str, prefix: str) -> None:
    from offbox.naming import worktree_name

    name = worktree_name(Path(root))
    assert name.startswith(prefix)
    assert len(name) == len(prefix) + 4
    assert worktree_name(Path(root)) == name
    assert worktree_name(Path(root.replace("/src/", "/other/"))) != name


def test_worktree_detection_failure_has_actionable_fix(tmp_path: Path) -> None:
    from offbox.naming import find_worktree

    runner = FakeRunner([Script(prefix=["git", "rev-parse"], returncode=128)])
    with pytest.raises(PreconditionError) as caught:
        find_worktree(runner, tmp_path)
    assert caught.value.fix == "cd into a git worktree"


def test_exact_container_command_and_labelled_volume() -> None:
    from offbox.naming import Worktree
    from offbox.runtime import container_command, volume_command

    work = Worktree(root=Path("/src/project"), name="project-abcd")
    assert container_command(work, EnvConfig(image="registry/image:tag"), 8, 16000000000) == [
        "docker",
        "run",
        "-d",
        "--name",
        "offbox-project-abcd",
        "--hostname",
        "project-abcd",
        "--privileged",
        "--cgroupns=private",
        "--network",
        "offbox",
        "--cpus",
        "7",
        "--memory",
        "12000000000",
        "--shm-size",
        "2g",
        "--restart",
        "no",
        "-v",
        "/srv/offbox/envs/project-abcd/work:/work",
        "-v",
        "offbox-project-abcd-docker:/var/lib/docker",
        "--label",
        "offbox.env=project-abcd",
        "--label",
        "offbox.worktree=/src/project",
        "--label",
        "offbox.version=0.1.0",
        "registry/image:tag",
    ]
    assert volume_command(work) == [
        "docker",
        "volume",
        "create",
        "--label",
        "offbox.env=project-abcd",
        "--label",
        "offbox.worktree=/src/project",
        "--label",
        "offbox.version=0.1.0",
        "offbox-project-abcd-docker",
    ]


def test_missing_dockerfile_refuses_before_any_remote_mutation(tmp_path: Path) -> None:
    from offbox.paths import Paths
    from offbox.runtime import Runtime

    (tmp_path / "offbox.toml").write_text('[env]\ndockerfile="Dockerfile"\n')
    runner = FakeRunner([Script(prefix=["git", "rev-parse"], stdout=str(tmp_path))])
    with pytest.raises(PreconditionError, match="Dockerfile does not exist"):
        Runtime(Paths(tmp_path / "cfg", tmp_path / "state", tmp_path / "cache", tmp_path / "control"), runner).up(cwd=tmp_path, fresh=True)
    assert [call.argv for call in runner.calls] == [["git", "rev-parse", "--show-toplevel"]]


def runtime_setup(root: Path) -> tuple[Paths, HostState, Path]:
    paths = Paths(root / "cfg", root / "state", root / "cache", root / "control")
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=root / "known_hosts",
    )
    StateStore(paths.state).write(State(hosts={"main": host}, default_host="main"))
    workroot = root / "project"
    workroot.mkdir()
    return paths, host, workroot


def container_json(root: Path, name: str, *, running: bool = True, labels_root: str | None = None) -> str:
    return json.dumps(
        [
            {
                "Name": f"/offbox-{name}",
                "Image": "sha256:actual-image",
                "State": {"Running": running, "ExitCode": 0, "Status": "running" if running else "exited"},
                "Config": {"Labels": {"offbox.env": name, "offbox.worktree": labels_root or str(root), "offbox.version": "0.0.1"}},
                "NetworkSettings": {"Networks": {"offbox": {"IPAddress": "10.89.0.2"}}},
            }
        ]
    )


def sync_response(root: Path, name: str, *, paused: bool = False) -> str:
    return json.dumps(
        [
            {
                "identifier": "sync_owned",
                "version": 1,
                "creationTime": "2026-10-23T12:00:00Z",
                "creatingVersion": "0.18.1",
                "name": f"offbox-{name}",
                "labels": {"offbox-env": name},
                "paused": paused,
                "status": "disconnected" if paused else "watching",
                "mode": "two-way-safe",
                "ignore": {"vcs": True, "paths": list(DEFAULT_IGNORES)},
                "alpha": {"protocol": "local", "path": str(root), "connected": True},
                "beta": {
                    "protocol": "ssh",
                    "host": "offbox-main",
                    "path": f"/srv/offbox/envs/{name}/work",
                    "connected": not paused,
                    "permissions": {"defaultFileMode": "0644", "defaultDirectoryMode": "0755"},
                },
            }
        ]
    )


def sync_ready_scripts(root: Path, name: str, *, fresh: bool = False, custom: bool = False) -> list[Script]:
    result = sync_response(root, name)
    commands = [Script(prefix=["mutagen", "sync", "list"], stdout="[]"), Script(prefix=["mutagen", "sync", "create"])]
    if custom:
        commands.extend([Script(prefix=["mutagen", "sync", "flush"]), Script(prefix=["mutagen", "sync", "list"], stdout=result)])
    if fresh:
        commands.extend(
            [
                Script(prefix=["mutagen", "sync", "list"], stdout=result),
                Script(prefix=["mutagen", "sync", "pause"]),
                Script(prefix=["mutagen", "sync", "list"], stdout=sync_response(root, name, paused=True)),
                Script(prefix=["mutagen", "sync", "resume"]),
            ]
        )
    return [*commands, Script(prefix=["mutagen", "sync", "flush"]), Script(prefix=["mutagen", "sync", "list"], stdout=result)]


def test_remove_cleans_remote_resources_before_state(tmp_path: Path) -> None:
    from offbox.naming import Worktree, worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    work = Worktree(root=root, name=worktree_name(root))
    with StateStore(paths.state).transaction() as state:
        state.envs[work.name] = EnvState(name=work.name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root)), Script(prefix=["mutagen"], stdout="[]"), Script(prefix=["ssh"])])
    Runtime(paths, runner).remove(cwd=root)
    assert StateStore(paths.state).read().envs == {}
    assert runner.calls[-1].input is not None
    assert runner.calls[-1].argv[-1].startswith("flock /var/lib/offbox/lock sh -s -- ")


def test_stop_preserves_state_and_data(tmp_path: Path) -> None:
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    env = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = env
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root)), Script(prefix=["mutagen"], stdout="[]"), Script(prefix=["ssh"])])
    Runtime(paths, runner).stop(cwd=root)
    assert StateStore(paths.state).read().envs[name] == env
    assert root.exists()


def test_gc_selects_stale_state_and_orphan_volume(tmp_path: Path) -> None:
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    absent = tmp_path / "absent"
    stale = worktree_name(absent)
    with StateStore(paths.state).transaction() as state:
        state.envs[stale] = EnvState(name=stale, host="main", worktree_path=absent, created_at=datetime.now(UTC))
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], stdout=""),
            Script(prefix=["ssh"], stdout=f"offbox-{name}-docker\n"),
            Script(
                prefix=["ssh"],
                stdout=json.dumps([{"Name": f"offbox-{name}-docker", "Labels": {"offbox.env": name, "offbox.worktree": str(root)}}]),
            ),
        ]
    )
    plan = Runtime(paths, runner).gc_plan()
    assert {(item.worktree.name, item.registered) for item in plan.targets} == {(stale, True), (name, False)}
    assert all(call.input is None for call in runner.calls)
    assert StateStore(paths.state).read().envs[stale].worktree_path == absent


@pytest.mark.parametrize(("memory", "shm"), [("garbage", "2g"), ("8g", "--privileged"), ("0", "2g"), ("1b", "2g"), ("8g", "-1")])
def test_invalid_resource_values_refuse_before_destructive_fresh(memory: str, shm: str) -> None:
    from offbox.naming import Worktree
    from offbox.runtime import container_command

    with pytest.raises(PreconditionError):
        container_command(Worktree(root=Path("/src/project"), name="project-abcd"), EnvConfig(memory=memory, shm_size=shm), 8, 16000000000)


def test_cli_rm_requires_explicit_confirmation_without_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from offbox.cli import app

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = CliRunner().invoke(app, ["rm"])
    assert result.exit_code == 3
    assert "--yes" in result.stderr
    assert "Fix:" in result.stderr


def test_cli_list_empty_is_json_on_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from offbox.cli import app

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = CliRunner().invoke(app, ["list", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"envs": []}
    assert result.stderr == ""


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def now(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


@pytest.mark.parametrize("fresh", [False, True])
def test_up_records_actual_image_and_keeps_atomic_remote_lock(tmp_path: Path, fresh: bool) -> None:
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    old = container_json(root, name, running=False)
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh"], stdout='{"cpus":8,"memory":16000000000}'),
            Script(prefix=["ssh"], stdout=old),
            Script(prefix=["ssh"], stdout=container_json(root, name)),
            Script(prefix=["ssh"]),
        ]
    )
    runner.scripts.insert(3, Script(prefix=["ssh"]))
    runner.scripts.extend(sync_ready_scripts(root, name, fresh=fresh))
    env = Runtime(paths, runner, FakeClock()).up(cwd=root, fresh=fresh)
    assert env.image_digest == "sha256:actual-image"
    assert StateStore(paths.state).read().envs[name].worktree_path == root
    assert StateStore(paths.state).read().envs[name].host == "main"
    remote_calls = [call for call in runner.calls if call.argv[0] == "ssh"]
    mutation = remote_calls[-2]
    assert mutation.argv[-1].startswith("flock /var/lib/offbox/lock sh -s -- ")
    assert mutation.input is not None
    assert "docker pull " in mutation.input
    assert mutation.input.index("docker pull ") < mutation.input.index('docker rm -f "$container"')
    assert 'rm -rf -- "$directory"' not in mutation.input
    assert (
        remote_calls[-1].argv[-1]
        == f"docker exec offbox-{name} sh -c 'test -f /run/offbox/ready && docker --host=unix:///var/run/docker.sock info >/dev/null 2>&1'"
    )


def test_up_failed_creation_retains_recoverable_registration(tmp_path: Path) -> None:
    from offbox.errors import CommandFailed
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh"], stdout='{"cpus":8,"memory":16000000000}'),
            Script(prefix=["ssh"], returncode=1, stderr="Error: No such container"),
            Script(prefix=["ssh"], returncode=125, stderr="image pull failed"),
        ]
    )
    with pytest.raises(CommandFailed):
        Runtime(paths, runner).up(cwd=root)
    assert StateStore(paths.state).read().envs[name].image_digest is None
    assert StateStore(paths.state).read().envs[name].host == "main"


def test_up_existing_foreign_container_refuses_before_mutation(tmp_path: Path) -> None:
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh"], stdout='{"cpus":8,"memory":16000000000}'),
            Script(prefix=["ssh"], stdout=container_json(root, name, labels_root="/different/worktree")),
        ]
    )
    with pytest.raises(PreconditionError, match="conflicting ownership"):
        Runtime(paths, runner).up(cwd=root, fresh=True)
    assert all(call.input is None for call in runner.calls)
    assert StateStore(paths.state).read().envs == {}


def test_remove_remote_failure_retains_state(tmp_path: Path) -> None:
    from offbox.errors import CommandFailed
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["mutagen"], stdout="[]"),
            Script(prefix=["ssh"], returncode=1, stderr="volume busy"),
        ]
    )
    with pytest.raises(CommandFailed):
        Runtime(paths, runner).remove(cwd=root)
    assert name in StateStore(paths.state).read().envs


def test_readiness_timeout_uses_fake_clock_and_logs_diagnostics(tmp_path: Path) -> None:
    from offbox.naming import Worktree, worktree_name
    from offbox.runtime import ContainerInfo, Runtime

    paths, host, root = runtime_setup(tmp_path)
    work = Worktree(root=root, name=worktree_name(root))
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], returncode=1),
            Script(prefix=["ssh"], stdout=container_json(root, work.name)),
            Script(prefix=["ssh"], returncode=1),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"]),
        ]
    )
    clock = FakeClock()
    runtime = Runtime(paths, runner, clock)
    container = ContainerInfo.model_validate(json.loads(container_json(root, work.name))[0])
    with pytest.raises(PreconditionError, match="did not become ready"):
        runtime.wait_ready(host, work, container, timeout=0.4)
    assert clock.elapsed == pytest.approx(0.4)
    assert runner.calls[-2].argv[-1] == f"docker logs --tail 40 {work.container}"
    assert runner.calls[-1].argv[-1] == f"docker exec {work.container} tail -n 40 /var/log/dockerd.log"


def test_gc_refuses_stale_plan_when_worktree_returns(tmp_path: Path) -> None:
    from offbox.naming import Worktree, worktree_name
    from offbox.runtime import GcPlan, GcTarget, Runtime

    paths, _host, root = runtime_setup(tmp_path)
    work = Worktree(root=root, name=worktree_name(root))
    with StateStore(paths.state).transaction() as state:
        state.envs[work.name] = EnvState(name=work.name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    runner = FakeRunner()
    with pytest.raises(PreconditionError, match="exists again"):
        Runtime(paths, runner).collect(
            GcPlan(targets=[GcTarget(host="main", worktree=work, reason="worktree no longer exists", registered=True)])
        )
    assert runner.calls == []
    assert work.name in StateStore(paths.state).read().envs


def test_gc_removes_cross_host_orphan_without_removing_registered_env(tmp_path: Path) -> None:
    from offbox.naming import Worktree, worktree_name
    from offbox.runtime import GcPlan, GcTarget, Runtime

    paths, host, root = runtime_setup(tmp_path)
    work = Worktree(root=root, name=worktree_name(root))
    other = host.model_copy(update={"name": "other", "address": "192.0.2.2"})
    env = EnvState(name=work.name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    with StateStore(paths.state).transaction() as state:
        state.hosts["other"] = other
        state.envs[work.name] = env
    runner = FakeRunner([Script(prefix=["ssh", "offbox-other"])])
    Runtime(paths, runner).collect(GcPlan(targets=[GcTarget(host="other", worktree=work, reason="orphan container", registered=False)]))
    assert StateStore(paths.state).read().envs[work.name] == env
    assert len(runner.calls) == 1


def test_stopped_container_old_ready_marker_requires_live_daemon(tmp_path: Path) -> None:
    from offbox.naming import Worktree, worktree_name
    from offbox.runtime import ContainerInfo, Runtime

    paths, host, root = runtime_setup(tmp_path)
    work = Worktree(root=root, name=worktree_name(root))
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], returncode=1, stderr="daemon not ready"),
            Script(prefix=["ssh"], stdout=container_json(root, work.name)),
            Script(prefix=["ssh"]),
        ]
    )
    clock = FakeClock()
    Runtime(paths, runner, clock).wait_ready(host, work, ContainerInfo.model_validate(json.loads(container_json(root, work.name))[0]))
    assert clock.elapsed == pytest.approx(0.2)
    assert (
        runner.calls[0].argv[-1]
        == f"docker exec {work.container} sh -c 'test -f /run/offbox/ready && docker --host=unix:///var/run/docker.sock info >/dev/null 2>&1'"
    )


@pytest.mark.parametrize(
    ("root", "expected"), [("/src/My Repo", "my-repo-235c"), ("/src/123 PROJECT", "123-project-3a16"), ("/src/☀", "env-024c")]
)
def test_naming_uses_sha256_of_absolute_path(root: str, expected: str) -> None:
    from offbox.naming import worktree_name

    assert worktree_name(Path(root)) == expected


@pytest.mark.parametrize("size", ["8gb", "1.5GiB", "512 MB", "1e3m", "6m"])
def test_docker_byte_unit_spellings_remain_supported(size: str) -> None:
    from offbox.naming import Worktree
    from offbox.runtime import container_command

    command = container_command(Worktree(root=Path("/src/project"), name="project-abcd"), EnvConfig(memory=size), 8, 16000000000)
    assert command[command.index("--memory") + 1] == size


def test_shell_uses_recorded_host_and_exact_tty_command(tmp_path: Path) -> None:
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    with StateStore(paths.state).transaction() as state:
        state.hosts["other"] = host.model_copy(update={"name": "other"})
        state.default_host = "other"
        state.envs[name] = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh", "offbox-main"], stdout=container_json(root, name)),
            Script(prefix=["ssh", "-t", "offbox-main"], method="stream", returncode=42),
        ]
    )
    assert Runtime(paths, runner).shell(cwd=root) == 42
    assert runner.calls[-1].argv == ["ssh", "-t", "offbox-main", "--", f"docker exec -it -u dev -w /work offbox-{name} bash -l"]
    assert runner.calls[-1].stdin is True
    assert runner.calls[-1].tty is True


def test_list_reports_unreachable_host_without_losing_state(tmp_path: Path) -> None:
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    runner = FakeRunner([Script(prefix=["ssh"], returncode=255, stderr="unreachable")])
    report = Runtime(paths, runner).report()
    assert report.envs[0].status == "unreachable"
    assert report.envs[0].fix == "Run offbox host ssh main and check connectivity."
    assert name in StateStore(paths.state).read().envs


def test_readiness_inspection_timeout_stays_within_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    from collections.abc import Mapping, Sequence

    from offbox.naming import Worktree, worktree_name
    from offbox.runner import Result
    from offbox.runtime import ContainerInfo, Runtime

    paths, host, root = runtime_setup(tmp_path)
    work = Worktree(root=root, name=worktree_name(root))
    clock = FakeClock()
    runner = FakeRunner()
    calls: list[tuple[list[str], float | None]] = []

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
        calls.append((list(argv), timeout))
        if "container inspect" in argv[-1]:
            clock.elapsed += timeout or 0
            raise PreconditionError("Timed out", "retry") from subprocess.TimeoutExpired(list(argv), timeout or 0.0)
        return Result(argv=list(argv), returncode=1)

    monkeypatch.setattr(runner, "run", run)
    with pytest.raises(PreconditionError, match="did not become ready"):
        Runtime(paths, runner, clock).wait_ready(
            host, work, ContainerInfo.model_validate(json.loads(container_json(root, work.name))[0]), timeout=2
        )
    assert clock.elapsed == pytest.approx(2)
    inspection = next(call for call in calls if "container inspect" in call[0][-1])
    assert inspection[1] == pytest.approx(1.8)
    assert "docker logs --tail 40" in calls[-2][0][-1]


def test_invalid_fresh_settings_preserve_existing_registration(tmp_path: Path) -> None:
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    env = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC), image_digest="sha256:old-image")
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = env
    (root / "offbox.toml").write_text('[env]\nmemory="garbage"\n')
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh"], stdout='{"cpus":8,"memory":16000000000}'),
        ]
    )
    with pytest.raises(PreconditionError, match="memory"):
        Runtime(paths, runner).up(cwd=root, fresh=True)
    assert StateStore(paths.state).read().envs[name] == env
    assert all(call.input is None for call in runner.calls)


def test_gc_prunes_inside_owned_env_and_preserves_state(tmp_path: Path) -> None:
    from offbox.naming import Worktree, worktree_name
    from offbox.runtime import GcPlan, GcTarget, Runtime

    paths, _host, root = runtime_setup(tmp_path)
    work = Worktree(root=root, name=worktree_name(root))
    env = EnvState(name=work.name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    with StateStore(paths.state).transaction() as state:
        state.envs[work.name] = env
    runner = FakeRunner([Script(prefix=["ssh"])])
    Runtime(paths, runner).collect(
        GcPlan(running=[GcTarget(host="main", worktree=work, reason="prune unused Docker data", registered=True)])
    )
    assert StateStore(paths.state).read().envs[work.name] == env
    assert runner.calls[0].input is not None
    assert 'docker exec "$container" docker system prune -f >/dev/null' in runner.calls[0].input
    assert "--volumes" not in runner.calls[0].input


def test_cli_gc_dry_run_reports_without_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Mapping, Sequence

    from typer.testing import CliRunner

    from offbox.cli import app
    from offbox.naming import worktree_name
    from offbox.runner import Result, SubprocessRunner

    paths, _host, _root = runtime_setup(tmp_path)
    absent = tmp_path / "removed-worktree"
    name = worktree_name(absent)
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = EnvState(name=name, host="main", worktree_path=absent, created_at=datetime.now(UTC))
    monkeypatch.setattr("offbox.cli.env.Paths.discover", lambda: paths)
    runner = FakeRunner([Script(prefix=["ssh"]), Script(prefix=["ssh"])])

    def run(
        _self: SubprocessRunner,
        argv: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Result:
        return runner.run(argv, check=check, capture=capture, input=input, env=env, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(SubprocessRunner, "run", run)
    result = CliRunner().invoke(app, ["gc", "--dry-run"])
    assert result.exit_code == 0
    assert "Dry run" in result.stderr
    assert name in result.stderr
    assert name in StateStore(paths.state).read().envs
    assert all(call.input is None for call in runner.calls)


def test_gc_refuses_inaccessible_worktree_instead_of_deleting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    original_stat = Path.stat

    def inaccessible(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == root:
            raise PermissionError("directory temporarily inaccessible")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", inaccessible)
    runner = FakeRunner()
    with pytest.raises(PreconditionError, match="Cannot inspect worktree"):
        Runtime(paths, runner).gc_plan()
    assert runner.calls == []
    assert name in StateStore(paths.state).read().envs


def custom_up_runner(root: Path, name: str, *, fresh: bool = False, build_fails: bool = False) -> FakeRunner:
    tag = "offbox-local/" + hashlib.sha256(str(root).encode()).hexdigest()[:16] + ":" + "b" * 64
    return FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh"], stdout='{"cpus":8,"memory":16000000000}'),
            Script(prefix=["ssh"], stdout=container_json(root, name)),
            Script(prefix=["ssh"]),
            Script(
                prefix=["ssh"],
                returncode=1 if build_fails else 0,
                stderr="synthetic compiler failure" if build_fails else "",
                stdout=json.dumps({"tag": tag, "digest": "sha256:" + "a" * 64}),
            ),
            Script(prefix=["ssh"], stdout=container_json(root, name)),
            Script(prefix=["ssh"]),
            *sync_ready_scripts(root, name, fresh=fresh, custom=True),
        ]
    )


@pytest.mark.parametrize("fresh", [False, True])
def test_custom_dockerfile_flushes_before_build_and_preserves_actual_container_digest(
    tmp_path: Path, fresh: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    (root / "Docker file").write_text("FROM ubuntu:24.04\n")
    (root / "offbox.toml").write_text('[env]\ndockerfile="Docker file"\n')
    name = worktree_name(root)
    runner = custom_up_runner(root, name, fresh=fresh)
    env = Runtime(paths, runner, FakeClock()).up(cwd=root, fresh=fresh)
    assert env.image_digest == "sha256:actual-image"
    assert "run offbox up --fresh" in capsys.readouterr().err
    build = next(call for call in runner.calls if call.input and 'docker build -t "$tag"' in call.input)
    mutation = next(call for call in runner.calls if call.input and 'docker container inspect "$container"\n' in call.input)
    flushes = [index for index, call in enumerate(runner.calls) if call.argv[:3] == ["mutagen", "sync", "flush"]]
    assert flushes[0] < runner.calls.index(build) < runner.calls.index(mutation) < flushes[-1]
    assert build.argv[-1].endswith("'Docker file' " + hashlib.sha256(str(root).encode()).hexdigest()[:16])
    assert build.timeout == 1800
    assert build.input and 'sha256sum < "$target"' in build.input
    assert mutation.input and "docker pull" not in mutation.input
    assert StateStore(paths.state).read().envs[name].image_digest == "sha256:actual-image"
    if fresh:
        pause = next(index for index, call in enumerate(runner.calls) if call.argv[:3] == ["mutagen", "sync", "pause"])
        resume = next(index for index, call in enumerate(runner.calls) if call.argv[:3] == ["mutagen", "sync", "resume"])
        assert runner.calls.index(build) < pause < runner.calls.index(mutation) < resume < flushes[-1]


def test_custom_build_failure_does_not_destroy_existing_container_or_volume(tmp_path: Path) -> None:
    from offbox.errors import CommandFailed
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    (root / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    (root / "offbox.toml").write_text('[env]\ndockerfile="Dockerfile"\n')
    name = worktree_name(root)
    old = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC), image_digest="old-image")
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = old
    runner = custom_up_runner(root, name, fresh=True, build_fails=True)
    with pytest.raises(CommandFailed, match="synthetic compiler failure"):
        Runtime(paths, runner).up(cwd=root, fresh=True)
    assert StateStore(paths.state).read().envs[name] == old
    assert not any(call.input and "docker rm -f" in call.input for call in runner.calls)
    assert not any(call.argv[:3] == ["mutagen", "sync", "pause"] for call in runner.calls)


@pytest.mark.parametrize("configured", ["../Dockerfile", "/outside/Dockerfile", "link/Dockerfile"])
def test_dockerfile_escape_or_symlink_refused_before_remote_work(tmp_path: Path, configured: str) -> None:
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "offbox.toml").write_text('[env]\ndockerfile="' + configured + '"\n')
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root))])
    with pytest.raises(PreconditionError, match="outside|symlink"):
        Runtime(paths, runner).up(cwd=root, fresh=True)
    assert [call.argv[0] for call in runner.calls] == ["git"]


def test_fresh_sync_preflight_failure_preserves_existing_resources(tmp_path: Path) -> None:
    from offbox.errors import CommandFailed
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    old = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC), image_digest="old-image")
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = old
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh"], stdout='{"cpus":8,"memory":16000000000}'),
            Script(prefix=["ssh"], stdout=container_json(root, name)),
            Script(prefix=["ssh"]),
            Script(prefix=["mutagen"], returncode=1, stderr="daemon failed"),
        ]
    )
    with pytest.raises(CommandFailed, match="daemon failed"):
        Runtime(paths, runner).up(cwd=root, fresh=True)
    assert not any(call.input and "docker rm -f" in call.input for call in runner.calls)
    assert StateStore(paths.state).read().envs[name] == old


@pytest.mark.parametrize("action", ["stop", "remove"])
def test_sync_cleanup_failure_preserves_environment_and_remote_resources(tmp_path: Path, action: str) -> None:
    from offbox.errors import CommandFailed
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    old = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = old
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root)), Script(prefix=["mutagen"], returncode=1, stderr="RPC failure")])
    runtime = Runtime(paths, runner)
    with pytest.raises(CommandFailed, match="RPC failure"):
        (runtime.stop if action == "stop" else runtime.remove)(cwd=root)
    assert not any(call.argv[0] == "ssh" for call in runner.calls)
    assert StateStore(paths.state).read().envs[name] == old


@pytest.mark.parametrize("fresh", [False, True])
def test_up_restores_ports_after_stack_and_fresh_cancels_before_replacement(
    tmp_path: Path, fresh: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    from ipaddress import IPv4Address

    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    (root / "offbox.toml").write_text('[ports]\nforward=[8000]\nauto=false\n[stack]\nup="start-stack"\n')
    with StateStore(paths.state).transaction() as state:
        state.hosts[host.name].control_master_pid = 100
        state.envs[name] = EnvState(
            name=name,
            host=host.name,
            worktree_path=root,
            created_at=datetime.now(UTC),
            local_ports={8000: 18000},
            active_forwards=[8000],
            forward_ip=IPv4Address("10.89.0.2"),
        )
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh"], stdout='{"cpus":8,"memory":16000000000}'),
            Script(prefix=["ssh"], stdout=container_json(root, name)),
            Script(prefix=["ssh"]),
            *(
                [
                    Script(prefix=["ssh", "-O", "check"], stderr="Master running (pid=100)"),
                    Script(prefix=["ssh", "-O", "cancel"]),
                ]
                if fresh
                else []
            ),
            Script(prefix=["ssh"], stdout=container_json(root, name)),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], method="start_stream"),
            Script(prefix=["ssh"], stdout=container_json(root, name)),
            Script(prefix=["ssh", "-O", "check"], stderr="Master running (pid=200)"),
            Script(prefix=["ssh", "-O", "forward"]),
            *sync_ready_scripts(root, name, fresh=fresh),
        ]
    )
    env = Runtime(paths, runner, FakeClock()).up(cwd=root, fresh=fresh)
    assert env.local_ports == {8000: 18000}
    assert env.active_forwards == [8000]
    assert StateStore(paths.state).read().hosts[host.name].control_master_pid == 200
    calls = runner.calls
    stack = next(index for index, call in enumerate(calls) if call.method == "start_stream")
    forward = next(index for index, call in enumerate(calls) if call.argv[:3] == ["ssh", "-O", "forward"])
    assert stack < forward
    assert calls[forward].argv == ["ssh", "-O", "forward", "-L", "127.0.0.1:18000:10.89.0.2:8000", "offbox-main"]
    if fresh:
        cancel = next(index for index, call in enumerate(calls) if call.argv[:3] == ["ssh", "-O", "cancel"])
        replacement = next(index for index, call in enumerate(calls) if call.input and 'docker rm -f "$container"' in call.input)
        assert cancel < replacement
    output = capsys.readouterr()
    assert output.out == ""
    assert f"localhost:18000 → {name}:8000" in output.err


@pytest.mark.parametrize("action", ["stop", "remove"])
def test_lifecycle_cancel_failure_prevents_resource_mutation_and_state_deletion(tmp_path: Path, action: str) -> None:
    from ipaddress import IPv4Address

    from offbox.errors import CommandFailed
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    with StateStore(paths.state).transaction() as state:
        state.hosts[host.name].control_master_pid = 100
        state.envs[name] = EnvState(
            name=name,
            host=host.name,
            worktree_path=root,
            created_at=datetime.now(UTC),
            local_ports={8000: 8000},
            active_forwards=[8000],
            forward_ip=IPv4Address("10.89.0.2"),
        )
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh", "-O", "check"], stderr="Master running (pid=100)"),
            Script(prefix=["ssh", "-O", "cancel"], returncode=255, stderr="listener cancellation failed"),
        ]
    )
    runtime = Runtime(paths, runner)
    with pytest.raises(CommandFailed, match="listener cancellation failed"):
        (runtime.stop if action == "stop" else runtime.remove)(cwd=root)
    assert StateStore(paths.state).read().envs[name].active_forwards == [8000]
    assert all(call.input is None for call in runner.calls)
    assert not any(call.argv[0] == "mutagen" for call in runner.calls)


@pytest.mark.parametrize("action", ["stop", "remove", "gc"])
def test_cleanup_cancels_owned_forward_before_mutation_and_retains_only_stop_mapping(tmp_path: Path, action: str) -> None:
    from ipaddress import IPv4Address

    from offbox.naming import Worktree, worktree_name
    from offbox.runtime import GcPlan, GcTarget, Runtime

    paths, host, root = runtime_setup(tmp_path)
    work = Worktree(root=root, name=worktree_name(root))
    with StateStore(paths.state).transaction() as state:
        state.hosts[host.name].control_master_pid = 100
        state.envs[work.name] = EnvState(
            name=work.name,
            host=host.name,
            worktree_path=root,
            created_at=datetime.now(UTC),
            local_ports={8000: 18000},
            active_forwards=[8000],
            forward_ip=IPv4Address("10.89.0.2"),
        )
    runner = FakeRunner(
        [
            *([Script(prefix=["git"], stdout=str(root))] if action != "gc" else []),
            Script(prefix=["ssh", "-O", "check"], stderr="Master running (pid=100)"),
            Script(prefix=["ssh", "-O", "cancel"]),
            Script(prefix=["mutagen", "sync", "list"], stdout="[]"),
            Script(prefix=["ssh"]),
        ]
    )
    runtime = Runtime(paths, runner)
    if action == "gc":
        runtime.collect(GcPlan(targets=[GcTarget(host=host.name, worktree=work, reason="stale", registered=True)]))
    else:
        (runtime.stop if action == "stop" else runtime.remove)(cwd=root)
    calls = runner.calls
    cancel = next(index for index, call in enumerate(calls) if call.argv[:3] == ["ssh", "-O", "cancel"])
    assert calls[cancel].argv == ["ssh", "-O", "cancel", "-L", "127.0.0.1:18000:10.89.0.2:8000", "offbox-main"]
    assert cancel < next(index for index, call in enumerate(calls) if call.input)
    state = StateStore(paths.state).read()
    if action == "stop":
        assert state.envs[work.name].local_ports == {8000: 18000}
        assert state.envs[work.name].active_forwards == []
        assert state.envs[work.name].forward_ip is None
    else:
        assert work.name not in state.envs


def test_orphan_gc_does_not_cancel_another_hosts_same_named_environment(tmp_path: Path) -> None:
    from ipaddress import IPv4Address

    from offbox.naming import Worktree, worktree_name
    from offbox.runtime import GcPlan, GcTarget, Runtime

    paths, host, root = runtime_setup(tmp_path)
    work = Worktree(root=root, name=worktree_name(root))
    env = EnvState(
        name=work.name,
        host="other",
        worktree_path=root,
        created_at=datetime.now(UTC),
        local_ports={8000: 8000},
        active_forwards=[8000],
        forward_ip=IPv4Address("10.89.0.3"),
    )
    with StateStore(paths.state).transaction() as state:
        state.hosts["other"] = host.model_copy(update={"name": "other"})
        state.envs[env.name] = env
    runner = FakeRunner([Script(prefix=["ssh"])])
    Runtime(paths, runner).collect(GcPlan(targets=[GcTarget(host=host.name, worktree=work, reason="orphan", registered=False)]))
    assert StateStore(paths.state).read().envs[env.name] == env
    assert len(runner.calls) == 1
    assert runner.calls[0].input is not None


@pytest.mark.parametrize("changed_field", ["control_master_pid", "address"])
def test_lifecycle_revalidation_ignores_master_identity_but_checks_host_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_field: str
) -> None:
    from collections.abc import Generator
    from contextlib import contextmanager

    from offbox.host import host_operation
    from offbox.naming import worktree_name
    from offbox.runtime import Runtime

    paths, host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = EnvState(name=name, host=host.name, worktree_path=root, created_at=datetime.now(UTC))

    @contextmanager
    def changed_master(selected: Paths, host_name: str, *, wait_timeout: float = 0, clock: FakeClock | None = None) -> Generator[None]:
        with host_operation(selected, host_name):
            with StateStore(paths.state).transaction() as state:
                if changed_field == "control_master_pid":
                    state.hosts[host_name].control_master_pid = 201
                else:
                    state.hosts[host_name].address = "192.0.2.2"
            yield

    monkeypatch.setattr("offbox.runtime.host_operation", changed_master)
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["mutagen", "sync", "list"], stdout="[]"),
            Script(prefix=["ssh"]),
        ]
    )
    if changed_field == "control_master_pid":
        Runtime(paths, runner).stop(cwd=root)
        assert StateStore(paths.state).read().hosts[host.name].control_master_pid == 201
        assert len(runner.calls) == 3
    else:
        with pytest.raises(PreconditionError, match="Environment changed"):
            Runtime(paths, runner).stop(cwd=root)
        assert len(runner.calls) == 1
