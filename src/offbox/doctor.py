from pathlib import Path

from pydantic import Field

from offbox.config import Credentials, Model, ProjectConfig, UserConfig, default_host, load_config, load_credentials
from offbox.errors import OffboxError, PreconditionError
from offbox.paths import Paths
from offbox.runner import Runner
from offbox.state import State, StateStore
from offbox.tools import ToolDiscovery


class Check(Model):
    name: str
    ok: bool
    message: str
    fix: str | None = None


class DoctorReport(Model):
    checks: list[Check] = Field(default_factory=list[Check])

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 3


def _failure(name: str, error: OffboxError) -> Check:
    return Check(name=name, ok=False, message=error.message, fix=error.fix or "Correct the reported problem and retry offbox doctor.")


def check_key(runner: Runner, key: Path, *, configured: bool = False) -> Check:
    agent = runner.run(["ssh-add", "-l"], check=False, timeout=5)
    if agent.returncode == 0 and agent.stdout.strip():
        if not configured and not key.exists():
            return Check(name="ssh-key", ok=True, message="SSH agent has a usable identity")
        fingerprint_path = key if key.exists() else key.with_name(key.name + ".pub")
        fingerprint = runner.run(["ssh-keygen", "-lf", str(fingerprint_path)], check=False, timeout=5)
        fields = fingerprint.stdout.split()
        if fingerprint.returncode == 0 and len(fields) > 1 and fields[1] in agent.stdout:
            return Check(name="ssh-key", ok=True, message="SSH key is available in the agent")
    if key.is_file():
        unlocked = runner.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)], check=False, timeout=5)
        if unlocked.returncode == 0:
            return Check(name="ssh-key", ok=True, message="SSH key is usable without a prompt")
    return Check(
        name="ssh-key",
        ok=False,
        message=f"SSH key is unavailable without a prompt: {key}",
        fix=f"Generate {key} or load it with ssh-add {key}.",
    )


def diagnose(paths: Paths, runner: Runner, *, project_root: Path | None = None, tools: ToolDiscovery | None = None) -> DoctorReport:
    report = DoctorReport()
    discovery = tools or ToolDiscovery(runner, paths.cache)
    for name in ("ssh", "git", "mutagen", "tofu"):
        try:
            tool = discovery.check(name)
            report.checks.append(Check(name=name, ok=True, message=f"{tool.name} {tool.version} ({tool.path})"))
        except OffboxError as exc:
            report.checks.append(_failure(name, exc))
    user: UserConfig | None = None
    state: State | None = None
    try:
        user = load_config(paths.user_config, UserConfig)
        report.checks.append(Check(name="user-config", ok=True, message="User config parses"))
    except OffboxError as exc:
        report.checks.append(_failure("user-config", exc))
    try:
        root = project_root
        if root is None:
            root = Path.cwd()
            git_available = any(check.name == "git" and check.ok for check in report.checks)
            if git_available:
                detected = runner.run(["git", "rev-parse", "--show-toplevel"], check=False, cwd=root, timeout=5)
                if detected.returncode == 0 and detected.stdout.strip():
                    root = Path(detected.stdout.strip())
        load_config(root / "offbox.toml", ProjectConfig)
        report.checks.append(Check(name="project-config", ok=True, message="Project config parses"))
    except OffboxError as exc:
        report.checks.append(_failure("project-config", exc))
    try:
        credentials: Credentials = load_credentials(paths.credentials)
        del credentials
        report.checks.append(Check(name="credentials", ok=True, message="Credentials configuration is valid"))
    except OffboxError as exc:
        report.checks.append(_failure("credentials", exc))
    try:
        state = StateStore(paths.state).read()
        report.checks.append(Check(name="state", ok=True, message="State validates"))
    except OffboxError as exc:
        report.checks.append(_failure("state", exc))
    key = Path.home() / ".ssh/id_ed25519"
    configured_key = False
    if user is not None and state is not None:
        selected = default_host(user, state.default_host)
        if selected is None:
            report.checks.append(Check(name="host", ok=True, message="No default host configured"))
        else:
            host = state.hosts.get(selected)
            if host is None:
                report.checks.append(
                    _failure(
                        "host",
                        PreconditionError(f"Default host {selected} is not registered", "Create the host or choose a registered host."),
                    )
                )
            else:
                configured_key = host.ssh_key is not None or (selected in user.hosts and user.hosts[selected].ssh_key is not None)
                key = host.ssh_key or (user.hosts[selected].ssh_key if selected in user.hosts else None) or key
                try:
                    result = runner.run(
                        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", f"offbox-{selected}", "--", "true"], check=False, timeout=5
                    )
                    report.checks.append(
                        Check(
                            name="host",
                            ok=result.returncode == 0,
                            message=f"Default host {selected} " + ("is reachable" if result.returncode == 0 else "is unreachable"),
                            fix=None if result.returncode == 0 else f"Check connectivity with offbox host ssh {selected}.",
                        )
                    )
                except OffboxError as exc:
                    report.checks.append(_failure("host", exc))
    try:
        report.checks.append(check_key(runner, key.expanduser(), configured=configured_key))
    except OffboxError as exc:
        report.checks.append(_failure("ssh-key", exc))
    return report
