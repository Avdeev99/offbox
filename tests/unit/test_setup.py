from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from fakes import FakeRunner, Script

from offbox.doctor import NO_DEFAULT_HOST, Check, DoctorReport
from offbox.errors import PreconditionError
from offbox.host.create import CreateOptions
from offbox.host.init import InitOptions
from offbox.paths import Paths
from offbox.runner import Runner
from offbox.setup import SetupOptions, setup
from offbox.ssh import include_line
from offbox.state import HostState, State, StateStore
from offbox.tools import ToolDiscovery


def context(
    root: Path, monkeypatch: pytest.MonkeyPatch, checks: list[Check] | None = None, *, default_key: bool = True
) -> tuple[Paths, Path, list[Path | None]]:
    paths = Paths(root / "config", root / "state", root / "cache", root / "control")
    seen: list[Path | None] = []
    home = root / "home"
    (home / ".ssh").mkdir(parents=True)
    if default_key:
        (home / ".ssh/id_ed25519").write_text("synthetic private key")
    monkeypatch.setenv("HOME", str(home))

    def doctor(_paths: Paths, _runner: Runner, *, ssh_key: Path | None = None, tools: ToolDiscovery | None = None) -> DoctorReport:
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
    assert not config.exists()
    assert not (paths.config / "ssh_config").exists()


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


def test_check_action_leaves_ssh_configuration_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    config.write_text("Host personal\n")
    runner = FakeRunner()
    with pytest.raises(PreconditionError, match="prerequisites") as caught:
        setup(paths, runner, SetupOptions(provider="ssh", action="check"), ssh_config=config)
    assert caught.value.fix == "Check host SSH."
    assert config.read_text() == "Host personal\n"
    assert not (paths.config / "ssh_config").exists()
    assert runner.calls == []


def test_check_without_any_host_only_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    paths, config, _ = context(tmp_path, monkeypatch, [Check(name="host", ok=False, message=NO_DEFAULT_HOST, fix="Run offbox setup.")])
    setup(paths, FakeRunner(), SetupOptions(provider="ssh", action="check"), ssh_config=config)
    assert "! No default host configured. Fix: Run offbox setup." in capsys.readouterr().err


def test_init_repairs_include_before_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, config, _ = context(tmp_path, monkeypatch)
    initialised: list[InitOptions] = []

    def init(_paths: Paths, _runner: Runner, options: InitOptions, **_kwargs: object) -> None:
        assert config.read_text().startswith(include_line(paths))
        initialised.append(options)

    monkeypatch.setattr("offbox.setup.init_host", init)
    fingerprint = "SHA256:" + "A" * 43
    setup(
        paths,
        FakeRunner(),
        SetupOptions(provider="ssh", action="init", target="dev@192.0.2.1", host_key_fingerprint=fingerprint),
        ssh_config=config,
    )
    assert initialised == [InitOptions(target="dev@192.0.2.1", host_key_fingerprint=fingerprint)]


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
    assert caught.value.fix == "Pass --provider ssh --action init --target USER@ADDRESS --host-key-fingerprint SHA256:..."


def record_create(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[CreateOptions]:
    created: list[CreateOptions] = []

    def create(_paths: Paths, _runner: Runner, options: CreateOptions, *, ssh_config: Path | None = None) -> HostState:
        created.append(options)
        return HostState(
            name=options.name,
            provider="hetzner",
            address="192.0.2.1",
            ssh_user="offbox",
            created_at=datetime.now(UTC),
            known_hosts_path=root,
        )

    monkeypatch.setattr("offbox.setup.create_host", create)
    monkeypatch.setenv("HCLOUD_TOKEN", "synthetic-setup-token")
    return created


def test_existing_dedicated_key_is_preferred_when_default_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, config, seen = context(tmp_path, monkeypatch, default_key=False)
    dedicated = tmp_path / "home/.ssh/offbox_ed25519"
    dedicated.write_text("synthetic private key")
    created = record_create(monkeypatch, tmp_path)
    setup(paths, FakeRunner(), SetupOptions(provider="hetzner", action="create"), ssh_config=config)
    assert seen == [dedicated]
    assert [options.ssh_key for options in created] == [dedicated]


def test_interactive_setup_creates_a_dedicated_key_with_consent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, config, seen = context(tmp_path, monkeypatch, default_key=False)
    (tmp_path / "home/.ssh").rmdir()
    created = record_create(monkeypatch, tmp_path)
    dedicated = tmp_path / "home/.ssh/offbox_ed25519"
    runner = FakeRunner([Script(prefix=["ssh-keygen"])])
    offers: list[str] = []

    def offer(message: str) -> bool:
        offers.append(message)
        return True

    setup(paths, runner, SetupOptions(provider="hetzner", action="create"), offer=offer, ssh_config=config)
    assert offers == ["No SSH key found. Create a dedicated key ~/.ssh/offbox_ed25519 for offbox hosts?"]
    assert runner.calls[0].argv == ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "offbox", "-f", str(dedicated)]
    assert (tmp_path / "home/.ssh").stat().st_mode & 0o777 == 0o700
    assert seen == [dedicated]
    assert [options.ssh_key for options in created] == [dedicated]


def test_noninteractive_missing_key_prints_the_keygen_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, config, _ = context(tmp_path, monkeypatch, default_key=False)
    record_create(monkeypatch, tmp_path)
    runner = FakeRunner()
    with pytest.raises(PreconditionError, match="No SSH key found") as caught:
        setup(paths, runner, SetupOptions(provider="hetzner", action="create"), ssh_config=config)
    assert caught.value.fix == 'Run ssh-keygen -t ed25519 -N "" -C offbox -f ~/.ssh/offbox_ed25519, then rerun offbox setup.'
    assert runner.calls == []


def test_missing_mutagen_is_downloaded_after_consent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.tools import mutagen_directory

    paths, config, _ = context(
        tmp_path, monkeypatch, [Check(name="mutagen", ok=False, message="Missing tool: mutagen", fix="Run offbox setup.")]
    )
    executable = mutagen_directory(paths.cache) / "mutagen"

    def install(cache: Path) -> Path:
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"binary")
        (executable.parent / "mutagen-agents.tar.gz").write_bytes(b"agents")
        return executable

    monkeypatch.setattr("offbox.setup.install_mutagen", install)
    runner = FakeRunner([Script(prefix=[str(executable), "--version"], stdout="Mutagen version 0.18.1")])
    offers: list[str] = []

    def offer(message: str) -> bool:
        offers.append(message)
        return True

    setup(
        paths,
        runner,
        SetupOptions(provider="ssh", action="check"),
        offer=offer,
        ssh_config=config,
        tools=ToolDiscovery(runner, paths.cache, lookup=lambda _name: None),
    )
    assert offers == [f"Mutagen 0.18.1 not found. Download it (verified) into {executable.parent}?"]
    assert runner.scripts == []


def test_noninteractive_missing_mutagen_points_to_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fix = "Run offbox setup to download Mutagen, or install Mutagen 0.18.x."
    paths, config, _ = context(tmp_path, monkeypatch, [Check(name="mutagen", ok=False, message="Missing tool: mutagen", fix=fix)])
    with pytest.raises(PreconditionError) as caught:
        setup(paths, FakeRunner(), SetupOptions(provider="ssh", action="check"), ssh_config=config)
    assert caught.value.fix == fix
    assert not paths.cache.exists()


@pytest.mark.parametrize(
    ("provider", "variable", "anchor"),
    [("hetzner", "export HCLOUD_TOKEN=...", "#hetzner"), ("gcp", "export GOOGLE_PROJECT=", "#google-cloud")],
)
def test_missing_credentials_name_the_variable_and_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: Literal["hetzner", "gcp"], variable: str, anchor: str
) -> None:
    paths, config, _ = context(tmp_path, monkeypatch)
    monkeypatch.delenv("HCLOUD_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_PROJECT", raising=False)
    with pytest.raises(PreconditionError, match="Missing") as caught:
        setup(paths, FakeRunner(), SetupOptions(provider=provider, action="check"), ssh_config=config)
    assert caught.value.fix is not None
    assert caught.value.fix.startswith(variable)
    assert caught.value.fix.endswith(f"https://github.com/o-avdieiev/offbox/blob/master/docs/installation.md{anchor}")


@pytest.mark.parametrize(
    ("files", "expected"),
    [((), "id_ed25519"), (("offbox_ed25519",), "offbox_ed25519"), (("id_ed25519.pub", "offbox_ed25519"), "id_ed25519")],
)
def test_default_ssh_key_prefers_the_dedicated_key_only_without_id_ed25519(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: tuple[str, ...], expected: str
) -> None:
    from offbox.ssh import default_ssh_key

    (tmp_path / ".ssh").mkdir()
    for name in files:
        (tmp_path / ".ssh" / name).write_text("synthetic key")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_ssh_key() == tmp_path / ".ssh" / expected


def test_host_init_defaults_to_the_dedicated_key_setup_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from offbox.host.init import init_host

    (tmp_path / "home/.ssh").mkdir(parents=True)
    dedicated = tmp_path / "home/.ssh/offbox_ed25519"
    dedicated.write_text("synthetic private key")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runner = FakeRunner(
        [Script(prefix=["ssh-keyscan"], stdout="192.0.2.1 ssh-ed25519 AAAA\n"), Script(prefix=["ssh-keygen", "-y"], returncode=1)]
    )
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    with pytest.raises(PreconditionError, match="SSH key is unavailable"):
        init_host(paths, runner, InitOptions(target="dev@192.0.2.1", accept_host_key=True))
    assert runner.calls[1].argv == ["ssh-keygen", "-y", "-P", "", "-f", str(dedicated.resolve())]
