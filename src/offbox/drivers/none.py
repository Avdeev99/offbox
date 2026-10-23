import re

from offbox.errors import ProviderError
from offbox.runner import Result, Runner
from offbox.ssh import ssh_argv
from offbox.state import HostState


class NoneDriver:
    def __init__(self, runner: Runner, host: HostState) -> None:
        self.runner = runner
        self.host = host

    def _run(self, argv: list[str], *, check: bool = True) -> Result:
        return self.runner.run(ssh_argv(self.host.name, argv), check=check, timeout=120)

    def destroy(self) -> None:
        containers = self._run(["docker", "ps", "-aq", "--filter", "label=offbox.env"]).stdout.split()
        if any(re.fullmatch(r"[0-9a-f]{12,64}", value) is None for value in containers):
            raise ProviderError("Docker returned invalid container identifiers", "Inspect the host Docker daemon before retrying.")
        if containers:
            self._run(["docker", "rm", "-f", *containers])
        volumes = self._run(["docker", "volume", "ls", "-q", "--filter", "name=^offbox-"]).stdout.split()
        if any(re.fullmatch(r"offbox-[A-Za-z0-9_.-]+", value) is None for value in volumes):
            raise ProviderError("Docker returned unexpected volume names", "Inspect the host Docker volumes before retrying.")
        if volumes:
            self._run(["docker", "volume", "rm", *volumes])
        if self._run(["docker", "network", "inspect", "offbox"], check=False).returncode == 0:
            self._run(["docker", "network", "rm", "offbox"])
        self._run(["sudo", "-n", "rm", "-rf", "/srv/offbox", "/var/lib/offbox"])
