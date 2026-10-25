import os
import selectors
import signal
import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, ClassVar, Self

import pytest

from offbox.errors import OffboxError, PreconditionError, ProviderInterrupted
from offbox.logging import configure
from offbox.paths import Paths
from offbox.runner import SubprocessRunner


def _pipe(content: bytes) -> BinaryIO:
    reader, writer = os.pipe()
    with os.fdopen(writer, "wb") as source:
        source.write(content)
    return os.fdopen(reader, "rb")


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("secret", ["live-cloud-token", "first-part\nsecond-part"])
def test_provider_output_is_logged_before_completion_without_polluting_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], verbose: bool, secret: str
) -> None:
    log = configure(Paths(tmp_path, tmp_path, tmp_path, tmp_path), ["host", "create"])

    class Process:
        def __init__(self, argv: Sequence[str], **kwargs: object) -> None:
            self.args = list(argv)
            self.stdout = _pipe(f"Creating host with {secret}\nFinished ✓\n".encode())
            self.stderr = _pipe(b"provider warning\n")
            self.pid = 123456
            self.returncode: int | None = None

        def __enter__(self) -> Self:
            return self

        def __exit__(self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None) -> None:
            self.stdout.close()
            self.stderr.close()

        def wait(self, timeout: float | None = None) -> int:
            text = log.read_text()
            assert "Creating host with [REDACTED]" in text
            assert "provider warning" in text
            self.returncode = 0
            return 0

        def communicate(self, input: str | None = None, timeout: float | None = None) -> tuple[str, str]:
            self.wait(timeout)
            return f"Creating host with {secret}\nFinished ✓\n", "provider warning\n"

        def kill(self) -> None:
            self.returncode = -9

        def poll(self) -> int | None:
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", Process)
    read = os.read

    def short_read(descriptor: int, size: int) -> bytes:
        return read(descriptor, min(size, 5))

    monkeypatch.setattr(os, "read", short_read)
    result = SubprocessRunner(verbose=verbose).run(["tofu", "apply"], env={"HCLOUD_TOKEN": secret}, interruptible=True)
    assert result.stdout == f"Creating host with {secret}\nFinished ✓\n"
    assert result.stderr == "provider warning\n"
    assert result.returncode == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert secret not in output.err + log.read_text()
    for part in secret.splitlines():
        assert part not in output.err + log.read_text()
    if verbose:
        assert "Finished ✓" in output.err
    else:
        assert output.err == ""


def test_provider_timeout_terminates_the_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[tuple[int, int]] = []

    class Process:
        def __init__(self, argv: Sequence[str], **kwargs: object) -> None:
            self.stdout = _pipe(b"")
            self.stderr = _pipe(b"")
            self.pid = 123456

        def __enter__(self) -> Self:
            return self

        def __exit__(self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None) -> None:
            self.stdout.close()
            self.stderr.close()

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired(["tofu", "apply"], timeout)
            return -9

    def kill_group(pid: int, sig: int) -> None:
        killed.append((pid, sig))

    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr(os, "killpg", kill_group)
    with pytest.raises(PreconditionError, match="Command timed out"):
        SubprocessRunner().run(["tofu", "apply"], timeout=5, interruptible=True)
    assert killed == [(123456, signal.SIGKILL)]


def test_interrupt_allows_the_provider_to_persist_state_before_exiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log = configure(Paths(tmp_path, tmp_path, tmp_path, tmp_path), ["host", "create"])
    signals: list[tuple[int, int]] = []

    class Process:
        def __init__(self, argv: Sequence[str], **kwargs: object) -> None:
            self.stdout = _pipe(b"provider saved resource state\n")
            self.stderr = _pipe(b"")
            self.pid = 123456

        def __enter__(self) -> Self:
            return self

        def __exit__(self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None) -> None:
            self.stdout.close()
            self.stderr.close()

        def wait(self, timeout: float | None = None) -> int:
            return 0

    class InterruptingSelector(selectors.SelectSelector):
        interrupted = False

        def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return super().select(timeout)

    def signal_group(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr(selectors, "DefaultSelector", InterruptingSelector)
    monkeypatch.setattr(os, "killpg", signal_group)
    exit_code: int | None = None
    try:
        SubprocessRunner().run(["tofu", "apply"], interruptible=True)
    except KeyboardInterrupt:
        exit_code = -1
    except OffboxError as exc:
        exit_code = exc.exit_code
    assert exit_code == 130
    assert signals == [(123456, signal.SIGINT)]
    assert "provider saved resource state" in log.read_text()


class _InterruptedProvider:
    interrupts: ClassVar[int] = 0
    waits: ClassVar[list[float | None]] = []

    def __init__(self, argv: Sequence[str], **kwargs: object) -> None:
        self.stdout = _pipe(b"")
        self.stderr = _pipe(b"")
        self.pid = 123456

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None) -> None:
        self.stdout.close()
        self.stderr.close()

    def wait(self, timeout: float | None = None) -> int:
        type(self).waits.append(timeout)
        if type(self).interrupts:
            type(self).interrupts -= 1
            raise KeyboardInterrupt
        return 1


@pytest.mark.parametrize("presses", [1, 2])
def test_repeated_interrupts_keep_waiting_for_the_provider_without_a_deadline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], presses: int
) -> None:
    signals: list[tuple[int, int]] = []

    def signal_group(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(_InterruptedProvider, "interrupts", presses)
    monkeypatch.setattr(_InterruptedProvider, "waits", [])
    monkeypatch.setattr(subprocess, "Popen", _InterruptedProvider)
    monkeypatch.setattr(os, "killpg", signal_group)
    with pytest.raises(ProviderInterrupted) as caught:
        SubprocessRunner().run(["tofu", "apply"], interruptible=True)
    assert caught.value.exit_code == 130
    assert caught.value.killed is False
    assert signals == [(123456, signal.SIGINT)]
    assert _InterruptedProvider.waits == [None] * (presses + 1)
    err = capsys.readouterr().err
    assert "waiting for the provider to save state" in err
    assert ("still finishing" in err) is (presses == 2)


def test_third_interrupt_force_stops_the_provider_and_names_the_cloud_label(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    signals: list[tuple[int, int]] = []

    def signal_group(pid: int, sig: int) -> None:
        signals.append((pid, sig))

    monkeypatch.setattr(_InterruptedProvider, "interrupts", 3)
    monkeypatch.setattr(_InterruptedProvider, "waits", [])
    monkeypatch.setattr(subprocess, "Popen", _InterruptedProvider)
    monkeypatch.setattr(os, "killpg", signal_group)
    with pytest.raises(ProviderInterrupted) as caught:
        SubprocessRunner().run(["tofu", "apply"], interruptible=True)
    assert caught.value.exit_code == 130
    assert caught.value.killed is True
    assert caught.value.fix == (
        "Check the provider console for resources labelled offbox-host and delete them, then run offbox host destroy NAME."
    )
    assert signals == [(123456, signal.SIGINT), (123456, signal.SIGKILL)]
    assert "force-stop" in capsys.readouterr().err
