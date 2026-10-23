from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import ValidationError

from offbox import console
from offbox.drivers.base import HostVariables, ProviderOutputs, TofuOutputs
from offbox.errors import CommandFailed, ProviderError
from offbox.logging import redact
from offbox.runner import Result, Runner
from offbox.ssh import write_private
from offbox.timings import stage


class TofuDriver:
    def __init__(self, runner: Runner, workspace: Path, cache: Path, credentials: Mapping[str, str], executable: Path) -> None:
        self.runner = runner
        self.workspace = workspace
        self.executable = str(executable)
        plugins = cache / "tofu-plugins"
        plugins.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.env = {
            **credentials,
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
            "TF_PLUGIN_CACHE_DIR": str(plugins),
            "TF_CLI_ARGS": "-no-color",
        }

    def _run(self, args: Sequence[str]) -> Result:
        result = self.runner.run([self.executable, *args], cwd=self.workspace, env=self.env, check=False)
        if result.returncode:
            raise CommandFailed([self.executable, *args], result.returncode, redact(result.stderr, self.env))
        return result

    def create(self, variables: HostVariables) -> ProviderOutputs:
        write_private(self.workspace / "offbox.auto.tfvars.json", variables.model_dump_json(indent=2))
        console.step("Initialising provider")
        with stage("Provider initialization"):
            self._run(["init", "-input=false"])
        console.step("Creating host")
        with stage("VM provisioning"):
            self._run(["apply", "-auto-approve", "-input=false"])
        output = self._run(["output", "-json"])
        try:
            values = TofuOutputs.model_validate_json(output.stdout)
            return ProviderOutputs(ip=values.ip.value, ssh_user=values.ssh_user.value)
        except ValidationError as exc:
            raise ProviderError(
                "Provider returned invalid host outputs", "Check provider outputs; destroy the host before retrying."
            ) from exc

    def destroy(self) -> None:
        console.step("Destroying host")
        with stage("VM deletion"):
            self._run(["destroy", "-auto-approve", "-input=false"])
