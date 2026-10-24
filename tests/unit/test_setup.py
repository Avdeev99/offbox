from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from fakes import FakeRunner, Script

from offbox.doctor import Check, DoctorReport
from offbox.errors import PreconditionError
from offbox.host.create import CreateOptions
from offbox.paths import Paths
from offbox.runner import Runner
from offbox.setup import SetupOptions, setup
from offbox.ssh import include_line, ssh_argv
from offbox.state import HostState, State, StateStore


def context(root: Path, monkeypatch: pytest.MonkeyPatch, checks: list[Check] | None = None) -> tuple[Paths, Path, list[Path | None]]:
    paths = Paths(root / "config", root / "state", root / "cache", root / "control")
    seen: list[Path | None] = []

    def doctor(_paths: Paths, _runner: Runner, *, ssh_key: Path | None = None) -> DoctorReport:
        seen.append(ssh_key)
        return DoctorReport(checks=checks or [Check(name="ssh", ok=True, message="SSH ready")])

    monkeypatch.setattr("offbox.setup.diagnose", doctor)
    return paths, root / "ssh-config", seen


@pytest.mark.parametrize("action", [None, "check"])
def test_noninteractive_setup_never_implicitly_provisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str | None) -> None:
    paths, config, _ = context(tmp_path, monkeypatch)
    runner = FakeRunner()
    if action is None:
        with pytest.raises(PreconditionError) as caught:
            setup(paths, runner, SetupOptions(provider="ssh"), ssh_config=config)
        assert caught.value.fix == "Pass --action check, --action create or --action init."
    else:
        setup(paths, runner, SetupOptions(provider="ssh", action="check"), ssh_config=config)
    assert runner.calls == []
    assert config.read_text().startswith(include_line(paths))


def test_tty_provider_choice_and_declined_creation_are_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, config, _ = context(tmp_path, monkeypatch)
    prompts: list[str] = []

    def ask(message: str, default: str) -> str:
        prompts.append(message)
        return "ssh"

    def confirm(message: str) -> bool:
        prompts.append(message)
        return False

    runner = FakeRunner()
    setup(paths, runner, SetupOptions(), ask=ask, confirm=confirm, ssh_config=config)
    assert prompts == ["Provider (hetzner, gcp or ssh)", "Bootstrap an existing SSH host?"]
    assert runner.calls == []


@pytest.mark.parametrize("provider", ["hetzner", "gcp"])
def test_explicit_create_passes_flags_and_ignores_unrelated_offline_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Literal["hetzner", "gcp"]
) -> None:
    paths, config, seen = context(
        tmp_path, monkeypatch, [Check(name="host", ok=False, message="Old default offline", fix="Check its network.")]
    )
    monkeypatch.setenv("HCLOUD_TOKEN", "synthetic-setup-token")
    monkeypatch.setenv("GOOGLE_PROJECT", "synthetic-project")
    created: list[CreateOptions] = []

    def create(_paths: Paths, _runner: Runner, options: CreateOptions, *, ssh_config: Path | None = None) -> HostState:
        assert ssh_config == config
        created.append(options)
        return HostState(
            name=options.name,
            provider=provider,
            address="192.0.2.1",
            ssh_user="offbox",
            created_at=datetime.now(UTC),
            known_hosts_path=tmp_path / "known_hosts",
        )

    monkeypatch.setattr("offbox.setup.create_host", create)
    key = tmp_path / "custom-key"
    setup(
        paths,
        FakeRunner(),
        SetupOptions(provider=provider, action="create", name="new", size="custom-size", region="custom-region", ssh_key=key),
        ssh_config=config,
    )
    assert created == [CreateOptions(provider=provider, name="new", size="custom-size", region="custom-region", ssh_key=key)]
    assert seen == [key]


def test_gcp_declined_creation_only_checks_prerequisites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, config, _ = context(tmp_path, monkeypatch)
    monkeypatch.setenv("GOOGLE_PROJECT", "synthetic-project")
    runner = FakeRunner()
    prompts: list[str] = []

    def confirm(message: str) -> bool:
        prompts.append(message)
        return False

    setup(paths, runner, SetupOptions(), ask=lambda _m, _d: "gcp", confirm=confirm, ssh_config=config)
    assert prompts == ["Create a billable cloud host?"]
    assert runner.calls == []


def test_gcp_create_requires_provider_tool_before_provisioning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, config, _ = context(tmp_path, monkeypatch, [Check(name="tofu", ok=False, message="Tool missing", fix="Install OpenTofu.")])
    runner = FakeRunner()
    with pytest.raises(PreconditionError, match="prerequisites") as caught:
        setup(paths, runner, SetupOptions(provider="gcp", action="create"), ssh_config=config)
    assert caught.value.fix == "Install OpenTofu."
    assert runner.calls == []


def test_setup_repairs_include_and_rechecks_default_host_before_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, config, _ = context(tmp_path, monkeypatch, [Check(name="host", ok=False, message="Alias missing", fix="Check host SSH.")])
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=tmp_path / "known_hosts",
    )
    StateStore(paths.state).write(State(hosts={host.name: host}, default_host=host.name))
    runner = FakeRunner([Script(prefix=ssh_argv(host.name, ["true"], options=["-o", "ConnectTimeout=3"]))])
    setup(paths, runner, SetupOptions(provider="ssh", action="check"), ssh_config=config)
    assert config.read_text().startswith(include_line(paths))
    assert "Host offbox-main" in (paths.config / "ssh_config").read_text()
    assert runner.scripts == []


def test_required_prerequisite_failure_is_not_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, config, _ = context(tmp_path, monkeypatch, [Check(name="ssh-key", ok=False, message="Key locked", fix="Load it with ssh-add.")])
    runner = FakeRunner()
    with pytest.raises(PreconditionError) as caught:
        setup(
            paths, runner, SetupOptions(provider="ssh", action="init", target="user@example.com", accept_host_key=True), ssh_config=config
        )
    assert caught.value.fix == "Load it with ssh-add."
    assert runner.calls == []


def test_missing_target_has_noninteractive_flag_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, config, _ = context(tmp_path, monkeypatch)
    with pytest.raises(PreconditionError) as caught:
        setup(paths, FakeRunner(), SetupOptions(provider="ssh", action="init"), ssh_config=config)
    assert caught.value.fix == "Pass --provider ssh --action init --target USER@ADDRESS --accept-host-key."
