import logging
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, Protocol

from offbox.errors import ConnectionError_, OffboxError, ProviderError
from offbox.runner import Runner
from offbox.ssh import ssh_argv
from offbox.timings import measured

logger = logging.getLogger(__name__)

BOOTSTRAP_FAILED = 7
BOOTSTRAP_TAIL_LINES = 15
READY_PROBE = (
    "test -f /var/lib/offbox/ready && exit 0; "
    f"test -f /var/lib/offbox/failed && {{ head -c 2000 /var/lib/offbox/failed; exit {BOOTSTRAP_FAILED}; }}; "
    "exit 1"
)


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def tcp_probe(address: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


@measured("Host readiness")
def wait_ready(
    name: str,
    address: str,
    port: int,
    runner: Runner,
    log_path: Path,
    *,
    clock: Clock | None = None,
    probe: Callable[[str, int, float], bool] = tcp_probe,
    tcp_timeout: float = 300,
    ready_timeout: float = 900,
) -> None:
    timer = clock or SystemClock()

    def diagnostics() -> str:
        try:
            diagnostic = runner.run(
                ssh_argv(name, ["sudo", "-n", "tail", "-n", "40", "/var/log/cloud-init-output.log"], options=["-o", "ConnectTimeout=3"]),
                check=False,
                timeout=10,
            )
            logger.error("host bootstrap diagnostics: %s\n%s", diagnostic.stdout, diagnostic.stderr)
            return diagnostic.stdout
        except OffboxError:
            logger.exception("Cannot retrieve bootstrap diagnostics")
            return ""

    def fail() -> NoReturn:
        diagnostics()
        raise ConnectionError_(f"Host {name} did not become ready", f"Run offbox host ssh {name}; inspect {log_path}.")

    def failed(marker: str) -> NoReturn:
        tail = "\n".join(diagnostics().splitlines()[-BOOTSTRAP_TAIL_LINES:])
        details = marker.strip() or "no details recorded"
        raise ProviderError(
            f"Host {name} bootstrap failed ({details})" + (f"\n{tail}" if tail else ""),
            f"Run offbox host ssh {name} and inspect /var/log/cloud-init-output.log and {log_path}.",
        )

    deadline = timer.now() + tcp_timeout
    while not probe(address, port, min(3, max(0.1, deadline - timer.now()))):
        if timer.now() >= deadline:
            fail()
        timer.sleep(min(3, max(0, deadline - timer.now())))
    deadline = timer.now() + ready_timeout
    while timer.now() < deadline:
        try:
            result = runner.run(
                ssh_argv(name, ["sh", "-c", READY_PROBE], options=["-o", "ConnectTimeout=3"]),
                check=False,
                timeout=min(10, deadline - timer.now()),
            )
        except OffboxError:
            logger.debug("Host SSH not ready", exc_info=True)
        else:
            if result.returncode == 0:
                return
            if result.returncode == BOOTSTRAP_FAILED:
                failed(result.stdout)
        timer.sleep(min(3, max(0, deadline - timer.now())))
    fail()
