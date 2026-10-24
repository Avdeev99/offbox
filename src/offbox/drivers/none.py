import re
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from offbox import console
from offbox.errors import ProviderError
from offbox.runner import Result, Runner
from offbox.ssh import ssh_argv
from offbox.state import HostState


class DockerResource(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    name: str = Field(alias="Name")
    labels: dict[str, str] | None = Field(default=None, alias="Labels")


class NetworkEndpoint(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    name: str = Field(alias="Name")


class DockerNetwork(DockerResource):
    containers: dict[str, NetworkEndpoint] | None = Field(default=None, alias="Containers")


class DockerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    labels: dict[str, str] | None = Field(default=None, alias="Labels")


class DockerContainer(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    id: str = Field(alias="Id")
    name: str = Field(alias="Name")
    config: DockerConfig = Field(alias="Config")


def _verify_resource(name: str, labels: dict[str, str] | None, *, volume: bool = False) -> None:
    values = labels or {}
    env = values.get("offbox.env", "")
    root = PurePosixPath(values.get("offbox.worktree", ""))
    expected = f"offbox-{env}" + ("-docker" if volume else "")
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,36}", env) is None or not root.is_absolute() or name.lstrip("/") != expected:
        raise ProviderError(
            "Conflicting Docker ownership labels", "Inspect the host resources and their labels before retrying destruction."
        )


class NoneDriver:
    def __init__(self, runner: Runner, host: HostState) -> None:
        self.runner = runner
        self.host = host

    def _run(self, argv: list[str], *, check: bool = True) -> Result:
        return self.runner.run(ssh_argv(self.host.name, argv), check=check, timeout=120)

    def destroy(self) -> None:
        containers = self._run(["docker", "ps", "-aq", "--no-trunc", "--filter", "label=offbox.env"]).stdout.split()
        if any(re.fullmatch(r"[0-9a-f]{12,64}", value) is None for value in containers):
            raise ProviderError("Docker returned invalid container identifiers", "Inspect the host Docker daemon before retrying.")
        try:
            if containers:
                inspected = TypeAdapter(list[DockerContainer]).validate_json(
                    self._run(["docker", "container", "inspect", *containers]).stdout
                )
                if {item.id for item in inspected} != set(containers) or len(inspected) != len(containers):
                    raise ProviderError("Docker inventory changed", "Retry destruction after other Docker operations finish.")
                for container in inspected:
                    _verify_resource(container.name, container.config.labels)
            volumes = self._run(["docker", "volume", "ls", "-q", "--filter", "label=offbox.env"]).stdout.split()
            if any(re.fullmatch(r"offbox-[a-z0-9][a-z0-9-]{0,36}-docker", value) is None for value in volumes):
                raise ProviderError("Docker returned unexpected volume names", "Inspect the host Docker volumes before retrying.")
            if volumes:
                inspected_volumes = TypeAdapter(list[DockerResource]).validate_json(
                    self._run(["docker", "volume", "inspect", *volumes]).stdout
                )
                if {item.name for item in inspected_volumes} != set(volumes) or len(inspected_volumes) != len(volumes):
                    raise ProviderError("Docker volume inventory changed", "Retry destruction after other Docker operations finish.")
                for volume in inspected_volumes:
                    _verify_resource(volume.name, volume.labels, volume=True)
            network_result = self._run(["docker", "network", "inspect", "offbox"], check=False)
            owned_network = False
            if network_result.returncode == 0:
                networks = TypeAdapter(list[DockerNetwork]).validate_json(network_result.stdout)
                if len(networks) != 1 or networks[0].name != "offbox":
                    raise ProviderError("Unexpected Docker network inventory", "Inspect the offbox network before retrying destruction.")
                owned_network = (networks[0].labels or {}).get("offbox.managed") == "1"
                if owned_network:
                    attached = self._run(["docker", "ps", "-aq", "--no-trunc", "--filter", "network=offbox"]).stdout.split()
                    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in attached):
                        raise ProviderError("Invalid Docker network members", "Inspect network membership before retrying destruction.")
                    if (set(attached) | set(networks[0].containers or {})) - set(containers):
                        raise ProviderError(
                            "Managed network has unrelated attached containers",
                            f"Run offbox host ssh {self.host.name} -- docker ps -a --filter network=offbox; disconnect unrelated containers before retrying.",
                        )
                if not owned_network:
                    console.info("Preserving the adopted or unlabelled Docker network offbox")
            elif network_result.returncode != 1 or not any(
                text in network_result.stderr.lower() for text in ("no such network", "not found")
            ):
                raise ProviderError("Cannot inspect the offbox network", "Check host connectivity and Docker before retrying destruction.")
        except ValidationError as exc:
            raise ProviderError("Invalid Docker ownership inventory", "Inspect the host resources before retrying destruction.") from exc
        if containers:
            self._run(["docker", "rm", "-f", *containers])
        if volumes:
            self._run(["docker", "volume", "rm", *volumes])
        if owned_network:
            self._run(["docker", "network", "rm", "offbox"])
        self._run(["sudo", "-n", "rm", "-rf", "/srv/offbox", "/var/lib/offbox"])
