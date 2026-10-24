from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.errors import ConnectionError_, UsageError
from offbox.naming import Worktree
from offbox.paths import Paths
from offbox.state import HostState


def context() -> tuple[Worktree, HostState]:
    return Worktree(root=Path("/src/app"), name="app-abcd"), HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
        known_hosts_path=Path("/state/known_hosts"),
    )


def paths(root: Path) -> Paths:
    return Paths(root / "config", root / "state", root / "cache", root / "control")


@pytest.mark.parametrize(
    ("tty", "forced", "stdin", "ssh_flag", "docker_flags", "shim_flags"),
    [
        (False, False, False, "-n", [], []),
        (False, False, True, "", ["-i"], []),
        (True, False, False, "-t", ["-i", "-t"], ["--tty"]),
        (True, True, False, "-tt", ["-i", "-t"], ["--tty"]),
    ],
)
def test_exact_exec_modes_preserve_remote_argv(
    tty: bool, forced: bool, stdin: bool, ssh_flag: str, docker_flags: list[str], shim_flags: list[str]
) -> None:
    import shlex

    from offbox.execute import command_argv

    work, host = context()
    remote = [
        "docker",
        "exec",
        *docker_flags,
        "-u",
        "dev",
        "-w",
        "/work/nested",
        "-e",
        "CI=1",
        work.container,
        "offbox-run",
        *shim_flags,
        "a" * 32,
        "/work/nested",
        "--",
        "printf",
        "%s",
        "a; echo unexpected",
    ]
    assert command_argv(
        work,
        host,
        ["printf", "%s", "a; echo unexpected"],
        run_id="a" * 32,
        directory="/work/nested",
        env={"CI": "1"},
        tty=tty,
        force_pty=forced,
        stdin=stdin,
    ) == ["ssh", *([ssh_flag] if ssh_flag else []), "offbox-main", "--", shlex.join(remote)]


@pytest.mark.parametrize("status", [0, 1, 42, 130, 143])
def test_exec_returns_real_status_without_adding_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str], status: int) -> None:
    from offbox.execute import Executor

    work, host = context()
    runner = FakeRunner([Script(prefix=["ssh"], method="start_stream", returncode=status)])
    assert Executor(paths(tmp_path), runner).run(work, host, ["true"]) == status
    assert capsys.readouterr().out == ""


def test_remote_exit_255_is_recovered_from_its_status_file(tmp_path: Path) -> None:
    from offbox.execute import Executor

    work, host = context()
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], method="start_stream", returncode=255),
            Script(prefix=["ssh", "-O", "check"]),
            Script(prefix=["ssh"], stdout="255\n"),
        ]
    )
    assert Executor(paths(tmp_path), runner, id_factory=lambda: "a" * 32).run(work, host, ["true"]) == 255
    assert runner.calls[-1].argv[-1].endswith("/run/offbox/exec/" + "a" * 32 + ".code")


def test_lost_connection_is_exit_four_when_no_confirmed_status(tmp_path: Path) -> None:
    from offbox.execute import Executor

    work, host = context()
    runner = FakeRunner(
        [Script(prefix=["ssh"], method="start_stream", returncode=255), Script(prefix=["ssh", "-O", "check"], returncode=1)]
    )
    with pytest.raises(ConnectionError_) as caught:
        Executor(paths(tmp_path), runner, id_factory=lambda: "a" * 32).run(work, host, ["true"])
    assert caught.value.exit_code == 4
    assert caught.value.fix is not None and "Completion is unknown" in caught.value.fix
    assert "offbox-run --signal TERM " + "a" * 32 in caught.value.fix
    assert "do not retry blindly" in caught.value.fix


def test_env_values_are_scoped_and_explicit_values_override() -> None:
    from offbox.execute import execution_env, parse_env

    env = execution_env(
        ["CI", "DEBUG", "UNSET"],
        parse_env(["DEBUG=override", "CUSTOM=a=b"]),
        tty=True,
        local={"CI": "1", "DEBUG": "old", "HCLOUD_TOKEN": "not-forwarded", "TERM": "xterm"},
    )
    assert env == {"CI": "1", "DEBUG": "override", "CUSTOM": "a=b", "TERM": "xterm"}
    with pytest.raises(UsageError):
        parse_env(["NOT VALID=value"])


@pytest.mark.parametrize("status", ["", "256\n", "-1\n", "42\n7\n", "not-a-code"])
def test_invalid_remote_status_is_connection_failure(tmp_path: Path, status: str) -> None:
    from offbox.execute import Executor

    work, host = context()
    runner = FakeRunner(
        [Script(prefix=["ssh"], method="start_stream", returncode=255), Script(prefix=["ssh"]), Script(prefix=["ssh"], stdout=status)]
    )
    with pytest.raises(ConnectionError_) as caught:
        Executor(paths(tmp_path), runner).run(work, host, ["true"])
    assert caught.value.exit_code == 4


@pytest.mark.parametrize("phase", ["master", "status"])
def test_followup_launch_failure_is_connection_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    from collections.abc import Sequence

    from offbox.errors import PreconditionError
    from offbox.execute import Executor
    from offbox.runner import Result

    work, host = context()
    runner = FakeRunner([Script(prefix=["ssh"], method="start_stream", returncode=255)])
    observed: list[list[str]] = []

    def failure(argv: Sequence[str], **kwargs: object) -> Result:
        observed.append(list(argv))
        if phase == "status" and list(argv[:3]) == ["ssh", "-O", "check"]:
            return Result(argv=list(argv), returncode=0)
        raise PreconditionError("follow-up timed out")

    monkeypatch.setattr(runner, "run", failure)
    with pytest.raises(ConnectionError_) as caught:
        Executor(paths(tmp_path), runner).run(work, host, ["true"])
    assert isinstance(caught.value.__cause__, PreconditionError)
    assert str(caught.value.__cause__) == "follow-up timed out"
    assert caught.value.exit_code == 4
    assert len(observed) == (1 if phase == "master" else 2)
    if phase == "master":
        assert observed[-1] == ["ssh", "-O", "check", "offbox-main"]
    else:
        assert ".code" in observed[-1][-1]


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


@pytest.mark.parametrize("number", [2, 15])
def test_signal_retries_metadata_race_and_preserves_drain(tmp_path: Path, number: int) -> None:
    from offbox.execute import Executor, SignalState

    work, host = context()
    clock = FakeClock()
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], method="start_stream", stream_results=[None, None, None, 130]),
            Script(prefix=["ssh"], returncode=2),
            Script(prefix=["ssh"], returncode=1),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], stdout="130\n"),
        ]
    )
    process = runner.start_stream(["ssh"])
    state = SignalState(clock)
    state.receive(number, None)
    assert Executor(paths(tmp_path), runner, clock=clock).interrupt(process, state, host, work, "a" * 32) == 128 + number
    assert len(runner.calls) == 5
    assert all(call.timeout == 1 for call in runner.calls[1:])
    assert not runner.processes[0].killed
    assert clock.value == pytest.approx(0.3)


def test_terminal_hangup_relays_term_to_the_remote_run_and_reports_sighup(tmp_path: Path) -> None:
    import signal

    from offbox.execute import Executor, SignalState

    work, host = context()
    clock = FakeClock()
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], method="start_stream", stream_results=[None, None, 129]),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], stdout="143\n"),
        ]
    )
    process = runner.start_stream(["ssh"])
    state = SignalState(clock)
    state.receive(signal.SIGHUP, None)
    assert Executor(paths(tmp_path), runner, clock=clock).interrupt(process, state, host, work, "a" * 32) == 128 + signal.SIGHUP
    assert runner.calls[1].argv[-1] == f"docker exec {work.container} offbox-run --signal TERM " + "a" * 32


def test_signal_deadline_kills_once_and_warns_exact_remote_cleanup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from offbox.execute import Executor, SignalState

    work, host = context()
    clock = FakeClock()
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], method="start_stream", stream_results=[None]),
            Script(prefix=["ssh"]),
            *[Script(prefix=["ssh"], returncode=1) for _ in range(100)],
        ]
    )
    process = runner.start_stream(["ssh"])
    state = SignalState(clock)
    state.receive(2, None)
    assert Executor(paths(tmp_path), runner, clock=clock).interrupt(process, state, host, work, "a" * 32) == 130
    assert runner.processes[0].killed
    assert clock.value == pytest.approx(9)
    assert max(call.timeout or 0 for call in runner.calls) <= 1
    assert "offbox host ssh main -- docker exec offbox-app-abcd offbox-run --signal KILL " + "a" * 32 in capsys.readouterr().err.replace(
        "\n", ""
    )


def test_signal_confirmation_waits_for_stream_drain(tmp_path: Path) -> None:
    from offbox.execute import Executor, SignalState

    work, host = context()
    clock = FakeClock()
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], method="start_stream", stream_results=[None, None, 130]),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], stdout="130\n"),
        ]
    )
    process = runner.start_stream(["ssh"])
    state = SignalState(clock)
    state.receive(2, None)
    assert Executor(paths(tmp_path), runner, clock=clock).interrupt(process, state, host, work, "a" * 32) == 130
    assert not runner.processes[0].killed
    assert clock.value == pytest.approx(0.2)


def test_setup_stdout_is_redirected_to_stderr() -> None:
    import shlex

    from offbox.execute import command_argv

    work, host = context()
    argv = command_argv(work, host, ["sh", "-c", "npm ci"], run_id="a" * 32, diagnostics=True)
    remote = shlex.split(argv[-1])
    assert remote[:2] == ["sh", "-c"]
    assert remote[2].startswith("exec docker exec -u dev -w /work ")
    assert remote[2].endswith("sh -c 'npm ci' >&2")


def test_fake_scripts_reject_unknown_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Script.model_validate({"prefix": ["ssh"], "error": "silently ignored fixture typo"})


def test_signal_cleanup_error_is_single_attempt_and_preserves_cancellation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from offbox.errors import PreconditionError
    from offbox.execute import Executor, SignalState

    class StuckProcess:
        def __init__(self) -> None:
            self.attempts = 0

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int | None:
            return None

        def kill(self) -> None:
            self.attempts += 1
            raise PreconditionError("unreapable")

    work, host = context()
    clock = FakeClock()
    process = StuckProcess()
    state = SignalState(clock)
    state.receive(15, None)
    clock.value = 9
    assert Executor(paths(tmp_path), FakeRunner(), clock=clock).interrupt(process, state, host, work, "a" * 32) == 143
    assert process.attempts == 1
    assert "Local SSH cleanup failed" in capsys.readouterr().err


def test_disconnected_main_ssh_still_requires_remote_completion_confirmation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from offbox.execute import Executor, SignalState

    work, host = context()
    clock = FakeClock()
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], method="start_stream", returncode=255),
            Script(prefix=["ssh"], returncode=2),
            Script(prefix=["ssh"], stdout="130\n"),
        ]
    )
    process = runner.start_stream(["ssh"])
    state = SignalState(clock)
    state.receive(2, None)
    assert Executor(paths(tmp_path), runner, clock=clock).interrupt(process, state, host, work, "a" * 32) == 130
    assert len(runner.calls) == 3
    assert clock.value == pytest.approx(0.1)
    assert capsys.readouterr().err == ""
