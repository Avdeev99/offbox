import json
import signal
from datetime import UTC, datetime
from pathlib import Path

import pytest

from offbox.errors import PreconditionError
from offbox.execute import ExecOptions, Executor
from offbox.naming import worktree_name
from offbox.paths import Paths
from offbox.runner import FakeRunner, Script
from offbox.state import EnvState, HostState, State, StateStore


def setup(root: Path) -> tuple[Paths, Path, str]:
    paths = Paths(root / "config", root / "state", root / "cache", root / "control")
    workroot = root / "project"
    workroot.mkdir()
    name = worktree_name(workroot)
    host = HostState(
        name="recorded",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=root / "known_hosts",
    )
    env = EnvState(name=name, host="recorded", worktree_path=workroot, created_at=datetime.now(UTC))
    other = host.model_copy(update={"name": "other"})
    StateStore(paths.state).write(State(hosts={"recorded": host, "other": other}, default_host="other", envs={name: env}))
    return paths, workroot, name


def container(root: Path, name: str, *, running: bool = True) -> str:
    return json.dumps(
        [
            {
                "Name": f"/offbox-{name}",
                "Image": "sha256:actual",
                "State": {"Running": running},
                "Config": {"Labels": {"offbox.env": name, "offbox.worktree": str(root)}},
            }
        ]
    )


@pytest.mark.parametrize(
    ("tty", "local_tty", "expected"),
    [(None, False, "-n"), (None, True, "-t"), (True, False, "-tt"), (True, True, "-t"), (False, True, "-n")],
)
def test_exec_maps_nested_cwd_uses_recorded_host_and_scoped_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tty: bool | None, local_tty: bool, expected: str
) -> None:
    import sys

    paths, root, name = setup(tmp_path)
    nested = root / "nested dir"
    nested.mkdir()
    (root / "offbox.toml").write_text('[exec]\nenv=["CI"]\n')
    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: local_tty)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: local_tty)
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh", "offbox-recorded"], stdout=container(root, name)),
            Script(
                prefix=["ssh", expected, "offbox-recorded"], method="stream" if expected in ("-t", "-tt") else "start_stream", returncode=42
            ),
        ]
    )
    assert Executor(paths, runner).execute(ExecOptions(command=["pytest", "-q"], no_sync=True, tty=tty), cwd=nested) == 42
    assert "'/work/nested dir'" in runner.calls[-1].argv[-1]
    assert "-e CI=1" in runner.calls[-1].argv[-1]
    assert all(call.argv[0] != "mutagen" for call in runner.calls)


def test_stopped_env_refuses_before_sync_or_user_command(tmp_path: Path) -> None:
    paths, root, name = setup(tmp_path)
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root)), Script(prefix=["ssh"], stdout=container(root, name, running=False))])
    with pytest.raises(PreconditionError) as caught:
        Executor(paths, runner).execute(ExecOptions(command=["true"]), cwd=root)
    assert caught.value.fix == "offbox up"
    assert len(runner.calls) == 2


def test_default_exec_flushes_before_requested_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.naming import Worktree
    from offbox.sync import SyncManager

    paths, root, name = setup(tmp_path)
    seen: list[int] = []
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh"], stdout=container(root, name)),
            Script(prefix=["ssh"], method="start_stream"),
        ]
    )

    def flush(self: SyncManager, work: Worktree, host: HostState | None = None) -> None:
        assert work.name == name and host is not None and host.name == "recorded"
        seen.append(len(runner.calls))

    monkeypatch.setattr(SyncManager, "flush", flush)
    assert Executor(paths, runner).execute(ExecOptions(command=["true"]), cwd=root) == 0
    assert seen == [2]


def test_run_restores_signal_handlers_after_relay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.execute import SignalState
    from offbox.naming import Worktree

    paths, root, name = setup(tmp_path)
    host = StateStore(paths.state).read().hosts["recorded"]
    originals = [signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)]
    runner = FakeRunner([Script(prefix=["ssh"], method="start_stream", stream_results=[None, 130])])

    class InterruptClock:
        def now(self) -> float:
            return 0

        def sleep(self, seconds: float) -> None:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)

    def relay(self: Executor, process: object, state: SignalState, target: HostState, work: Worktree, run_id: str) -> int:
        assert state.number == signal.SIGINT
        assert target.name == "recorded"
        return 130

    monkeypatch.setattr(Executor, "interrupt", relay)
    assert Executor(paths, runner, clock=InterruptClock()).run(Worktree(root=root, name=name), host, ["true"]) == 130
    assert [signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)] == originals


def test_invalid_setup_env_refuses_before_fresh_mutation(tmp_path: Path) -> None:
    from offbox.runtime import Runtime

    paths, root, _ = setup(tmp_path)
    (root / "offbox.toml").write_text('[exec]\nenv=["NOT VALID"]\n')
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root))])
    with pytest.raises(PreconditionError, match="Invalid exec.env"):
        Runtime(paths, runner).up(cwd=root, fresh=True)
    assert [call.argv[0] for call in runner.calls] == ["git"]
