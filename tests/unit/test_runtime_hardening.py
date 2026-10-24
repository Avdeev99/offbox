import fcntl
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import pytest
from fakes import FakeRunner, Script
from test_runtime import FakeClock, container_json, runtime_setup, sync_ready_scripts

from offbox.config import EnvConfig
from offbox.errors import CommandFailed, PreconditionError
from offbox.naming import Worktree, worktree_name
from offbox.runtime import SETTINGS_LABEL, Runtime, container_command, settings_digest, with_settings_label
from offbox.state import EnvState, StateStore


def labelled(root: Path, name: str, settings: str | None) -> str:
    containers = json.loads(container_json(root, name))
    if settings is not None:
        containers[0]["Config"]["Labels"][SETTINGS_LABEL] = settings
    return json.dumps(containers)


def up_runner(root: Path, name: str, created: str) -> FakeRunner:
    return FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh"], stdout='{"cpus":8,"memory":16000000000}'),
            Script(prefix=["ssh"], stdout=created),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], stdout=created),
            Script(prefix=["ssh"]),
            *sync_ready_scripts(root, name),
        ]
    )


def expected_settings(root: Path, name: str) -> str:
    return settings_digest(container_command(Worktree(root=root, name=name), EnvConfig(), 8, 16000000000))


def test_settings_label_hashes_only_container_shaping_values() -> None:
    work = Worktree(root=Path("/src/project"), name="project-abcd")
    command = container_command(work, EnvConfig(image="registry/image:tag"), 8, 16000000000)
    labelled_command = with_settings_label(command)
    assert labelled_command[-1] == "registry/image:tag"
    assert labelled_command[-3:-1] == ["--label", f"{SETTINGS_LABEL}={settings_digest(command)}"]
    assert settings_digest(labelled_command) == settings_digest(command)
    assert settings_digest(container_command(work, EnvConfig(image="registry/image:tag", cpus=2), 8, 16000000000)) != settings_digest(
        command
    )
    assert settings_digest([*command[:-1], "registry/image:other"]) != settings_digest(command)


@pytest.mark.parametrize(("settings", "warned"), [(None, False), ("0" * 32, True), ("expected", False)])
def test_up_warns_when_an_existing_container_has_different_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], settings: str | None, warned: bool
) -> None:
    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    value = expected_settings(root, name) if settings == "expected" else settings
    runner = up_runner(root, name, labelled(root, name, value))
    Runtime(paths, runner, FakeClock()).up(cwd=root)
    mutation = next(call for call in runner.calls if call.input and "docker run" in call.input)
    assert f"--label {SETTINGS_LABEL}={expected_settings(root, name)} " in (mutation.input or "")
    error = " ".join(capsys.readouterr().err.split())
    assert ("Environment settings changed (image, cpus, memory or shm_size); run offbox up --fresh to apply them." in error) is warned


def test_up_checks_host_readiness_and_resources_in_one_round_trip(tmp_path: Path) -> None:
    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    runner = up_runner(root, name, labelled(root, name, expected_settings(root, name)))
    Runtime(paths, runner, FakeClock()).up(cwd=root)
    first = shlex.split(runner.calls[1].argv[-1])
    assert first[:2] == ["sh", "-c"]
    assert first[2].startswith("test -f /var/lib/offbox/ready ||")
    assert "exec docker info --format" in first[2]


def test_uninitialised_host_still_refuses_up(tmp_path: Path) -> None:
    paths, _host, root = runtime_setup(tmp_path)
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh"], returncode=3, stderr="offbox: host initialisation is incomplete"),
        ]
    )
    with pytest.raises(CommandFailed, match="host initialisation is incomplete"):
        Runtime(paths, runner, FakeClock()).up(cwd=root)


@pytest.mark.parametrize(("previous", "warning"), [(None, None), (["CI"], "NPM_TOKEN"), (["CI", "NPM_TOKEN", "OLD"], None)])
def test_up_approves_exec_env_and_warns_about_new_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], previous: list[str] | None, warning: str | None
) -> None:
    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    (root / "offbox.toml").write_text('[exec]\nenv=["NPM_TOKEN", "CI"]\n')
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC), approved_exec_env=previous)
    runner = up_runner(root, name, labelled(root, name, expected_settings(root, name)))
    Runtime(paths, runner, FakeClock()).up(cwd=root)
    assert StateStore(paths.state).read().envs[name].approved_exec_env == ["CI", "NPM_TOKEN"]
    error = " ".join(capsys.readouterr().err.split())
    if warning is None:
        assert "Forwarding new local variables" not in error
    else:
        assert f"Forwarding new local variables to the environment: {warning}" in error


def test_up_refuses_credential_variables_in_exec_env_before_remote_work(tmp_path: Path) -> None:
    paths, _host, root = runtime_setup(tmp_path)
    (root / "offbox.toml").write_text('[exec]\nenv=["HCLOUD_TOKEN"]\n')
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root))])
    with pytest.raises(PreconditionError, match="must not forward provider credentials: HCLOUD_TOKEN"):
        Runtime(paths, runner).up(cwd=root)
    assert len(runner.calls) == 1


class ReleasingClock(FakeClock):
    def __init__(self, handle: TextIO, after: int) -> None:
        super().__init__()
        self.handle = handle
        self.after = after
        self.sleeps = 0

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        self.sleeps += 1
        if self.sleeps == self.after:
            self.handle.close()


@pytest.mark.parametrize("action", ["stop", "remove"])
def test_stop_and_remove_wait_briefly_for_a_sibling_operation(tmp_path: Path, action: str) -> None:
    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    (paths.state / "host-locks").mkdir()
    held = (paths.state / "host-locks" / "main.lock").open("a")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    clock = ReleasingClock(held, after=4)
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root)), Script(prefix=["mutagen"], stdout="[]"), Script(prefix=["ssh"])])
    runtime = Runtime(paths, runner, clock)
    if action == "stop":
        runtime.stop(cwd=root)
    else:
        runtime.remove(cwd=root)
    assert clock.sleeps == 4
    assert runner.calls[-1].input is not None


def test_stop_wait_for_the_host_lock_is_bounded(tmp_path: Path) -> None:
    paths, _host, root = runtime_setup(tmp_path)
    name = worktree_name(root)
    with StateStore(paths.state).transaction() as state:
        state.envs[name] = EnvState(name=name, host="main", worktree_path=root, created_at=datetime.now(UTC))
    (paths.state / "host-locks").mkdir()
    with (paths.state / "host-locks" / "main.lock").open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        clock = FakeClock()
        runner = FakeRunner([Script(prefix=["git"], stdout=str(root))])
        with pytest.raises(PreconditionError, match="after waiting 10s"):
            Runtime(paths, runner, clock).stop(cwd=root)
    assert clock.elapsed == pytest.approx(10)
    assert len(runner.calls) == 1


def test_runtime_sync_uses_the_configured_cache(tmp_path: Path) -> None:
    paths, _host, _root = runtime_setup(tmp_path)
    assert Runtime(paths, FakeRunner(), FakeClock()).sync.cache == paths.cache
