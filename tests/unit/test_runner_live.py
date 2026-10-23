import os
import selectors
import signal
import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

import pytest

from offbox.errors import OffboxError, PreconditionError
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
    result = SubprocessRunner(verbose=verbose).run(["tofu", "apply"], env={"TF_IN_AUTOMATION": "1", "HCLOUD_TOKEN": secret})
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
        SubprocessRunner().run(["tofu", "apply"], env={"TF_IN_AUTOMATION": "1"}, timeout=5)
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
        SubprocessRunner().run(["tofu", "apply"], env={"TF_IN_AUTOMATION": "1"})
    except KeyboardInterrupt:
        exit_code = -1
    except OffboxError as exc:
        exit_code = exc.exit_code
    assert exit_code == 130
    assert signals == [(123456, signal.SIGINT)]
    assert "provider saved resource state" in log.read_text()
