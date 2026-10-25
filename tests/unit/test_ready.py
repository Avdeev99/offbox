import shlex
import subprocess
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.errors import ConnectionError_, ProviderError
from offbox.host.ready import BOOTSTRAP_FAILED, READY_PROBE, wait_ready

PROBE_ARGV = ["ssh", "-o", "ConnectTimeout=3", "offbox-main", "--", shlex.join(["sh", "-c", READY_PROBE])]
TAIL_ARGV = ["ssh", "-o", "ConnectTimeout=3", "offbox-main", "--", "sudo -n tail -n 40 /var/log/cloud-init-output.log"]


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def now(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def wait(runner: FakeRunner, clock: FakeClock, tmp_path: Path) -> None:
    wait_ready("main", "192.0.2.1", 22, runner, tmp_path / "run.log", clock=clock, probe=lambda _a, _p, _t: True, ready_timeout=60)


def test_ready_marker_returns_after_polling(tmp_path: Path) -> None:
    runner = FakeRunner([Script(prefix=PROBE_ARGV, returncode=1), Script(prefix=PROBE_ARGV, returncode=0)])
    clock = FakeClock()
    wait(runner, clock, tmp_path)
    assert [call.argv for call in runner.calls] == [PROBE_ARGV, PROBE_ARGV]
    assert clock.elapsed == 3


def test_failed_marker_fails_fast_with_bootstrap_log_tail(tmp_path: Path) -> None:
    log = "\n".join(f"cloud-init line {index}" for index in range(40))
    runner = FakeRunner(
        [
            Script(prefix=PROBE_ARGV, returncode=1),
            Script(prefix=PROBE_ARGV, returncode=BOOTSTRAP_FAILED, stdout="exit=100 stage=docker \n"),
            Script(prefix=TAIL_ARGV, stdout=log),
        ]
    )
    clock = FakeClock()
    with pytest.raises(ProviderError) as caught:
        wait(runner, clock, tmp_path)
    message = caught.value.message
    assert message.splitlines()[0] == "Host main bootstrap failed (exit=100 stage=docker)"
    assert message.splitlines()[1:] == [f"cloud-init line {index}" for index in range(25, 40)]
    assert caught.value.fix == f"Run offbox host ssh main and inspect /var/log/cloud-init-output.log and {tmp_path / 'run.log'}."
    assert clock.elapsed == 3
    assert [call.argv for call in runner.calls] == [PROBE_ARGV, PROBE_ARGV, TAIL_ARGV]


def test_failed_marker_without_reachable_log_still_fails_fast(tmp_path: Path) -> None:
    runner = FakeRunner([Script(prefix=PROBE_ARGV, returncode=BOOTSTRAP_FAILED, stdout=""), Script(prefix=TAIL_ARGV, returncode=255)])
    with pytest.raises(ProviderError, match=r"^Host main bootstrap failed \(no details recorded\)$"):
        wait(runner, FakeClock(), tmp_path)


def test_unreachable_ssh_keeps_waiting_until_timeout(tmp_path: Path) -> None:
    runner = FakeRunner([*(Script(prefix=PROBE_ARGV, returncode=255) for _ in range(20)), Script(prefix=TAIL_ARGV, stdout="booting")])
    clock = FakeClock()
    with pytest.raises(ConnectionError_, match="did not become ready"):
        wait(runner, clock, tmp_path)
    assert clock.elapsed == 60
    assert runner.calls[-1].argv == TAIL_ARGV


@pytest.mark.subprocess
@pytest.mark.parametrize(
    ("markers", "returncode", "stdout"),
    [((), 1, ""), (("failed",), BOOTSTRAP_FAILED, "exit=3 stage=preflight"), (("ready", "failed"), 0, "")],
)
def test_ready_probe_script_reports_markers(tmp_path: Path, markers: tuple[str, ...], returncode: int, stdout: str) -> None:
    for marker in markers:
        (tmp_path / marker).write_text("exit=3 stage=preflight\n" if marker == "failed" else "")
    script = READY_PROBE.replace("/var/lib/offbox", str(tmp_path))
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert (result.returncode, result.stdout.strip()) == (returncode, stdout)
