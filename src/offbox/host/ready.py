import logging
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from offbox.errors import ConnectionError_, OffboxError
from offbox.runner import Runner
from offbox.ssh import ssh_argv
from offbox.timings import measured

logger = logging.getLogger(__name__)


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

    def fail() -> None:
        try:
            diagnostic = runner.run(
                ssh_argv(name, ["sudo", "-n", "tail", "-n", "40", "/var/log/cloud-init-output.log"], options=["-o", "ConnectTimeout=3"]),
                check=False,
                timeout=10,
            )
            logger.error("host bootstrap diagnostics: %s\n%s", diagnostic.stdout, diagnostic.stderr)
        except OffboxError:
            logger.exception("Cannot retrieve bootstrap diagnostics")
        raise ConnectionError_(f"Host {name} did not become ready", f"Run offbox host ssh {name}; inspect {log_path}.")

    deadline = timer.now() + tcp_timeout
    while not probe(address, port, min(3, max(0.1, deadline - timer.now()))):
        if timer.now() >= deadline:
            fail()
        timer.sleep(min(3, max(0, deadline - timer.now())))
    deadline = timer.now() + ready_timeout
    while timer.now() < deadline:
        try:
            result = runner.run(
                ssh_argv(name, ["test", "-f", "/var/lib/offbox/ready"], options=["-o", "ConnectTimeout=3"]),
                check=False,
                timeout=min(10, deadline - timer.now()),
            )
            if result.returncode == 0:
                return
        except OffboxError:
            logger.debug("Host SSH not ready", exc_info=True)
        timer.sleep(min(3, max(0, deadline - timer.now())))
    fail()
