import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from offbox.config import EnvConfig
from offbox.errors import PreconditionError
from offbox.paths import Paths
from offbox.runner import FakeRunner, Script
from offbox.state import EnvState, HostState, State, StateStore


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
        "offbox.version=0.0.1",
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
        "offbox.version=0.0.1",
        "offbox-project-abcd-docker",
    ]


def test_custom_dockerfile_refuses_before_any_remote_mutation(tmp_path: Path) -> None:
    from offbox.paths import Paths
    from offbox.runtime import Runtime

    (tmp_path / "offbox.toml").write_text('[env]\ndockerfile="Dockerfile"\n')
    runner = FakeRunner([Script(prefix=["git", "rev-parse"], stdout=str(tmp_path))])
    with pytest.raises(PreconditionError, match="source sync"):
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
            }
        ]
    )


def test_remove_cleans_remote_resources_before_state(tmp_path: Path) -> None:
    from offbox.naming import Worktree, worktree_name
    from offbox.runtime import Runtime

    paths, _host, root = runtime_setup(tmp_path)
    work = Worktree(root=root, name=worktree_name(root))
    with StateStore(paths.state).transaction() as state:
        state.envs[work.name] = EnvState(name=work.name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root)), Script(prefix=["ssh"])])
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
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root)), Script(prefix=["ssh"])])
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
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], stdout='{"cpus":8,"memory":16000000000}'),
            Script(prefix=["ssh"], stdout=old),
            Script(prefix=["ssh"], stdout=container_json(root, name)),
            Script(prefix=["ssh"]),
        ]
    )
    env = Runtime(paths, runner, FakeClock()).up(cwd=root, fresh=fresh)
    assert env.image_digest == "sha256:actual-image"
    assert StateStore(paths.state).read().envs[name].worktree_path == root
    assert StateStore(paths.state).read().envs[name].host == "main"
    mutation = runner.calls[-2]
    assert mutation.argv[-1].startswith("flock /var/lib/offbox/lock sh -s -- ")
    assert mutation.input is not None
    assert "docker pull " in mutation.input
    assert mutation.input.index("docker pull ") < mutation.input.index('docker rm -f "$container"')
    assert 'rm -rf -- "$directory"' not in mutation.input
    assert (
        runner.calls[-1].argv[-1]
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
            Script(prefix=["ssh"]),
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
            Script(prefix=["ssh"]),
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
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root)), Script(prefix=["ssh"], returncode=1, stderr="volume busy")])
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
        runtime.wait_ready(host, work, container, timeout=2)
    assert clock.elapsed == 2
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
    assert clock.elapsed == 1
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
    assert clock.elapsed == 2
    inspection = next(call for call in calls if "container inspect" in call[0][-1])
    assert inspection[1] == 1
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
            Script(prefix=["ssh"]),
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
