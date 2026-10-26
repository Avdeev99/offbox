import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.errors import PreconditionError
from offbox.execute import ExecOptions, Executor, credential_env_names, execution_env, parse_env
from offbox.logging import redact
from offbox.naming import worktree_name
from offbox.paths import Paths
from offbox.state import EnvState, HostState, State, StateStore


def setup(root: Path, names: list[str], approved: list[str] | None) -> tuple[Paths, Path, str]:
    paths = Paths(root / "config", root / "state", root / "cache", root / "control")
    workroot = root / "project"
    workroot.mkdir()
    (workroot / "offbox.toml").write_text("[exec]\nenv=[" + ", ".join(f'"{name}"' for name in names) + "]\n")
    name = worktree_name(workroot)
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime(2026, 10, 24, tzinfo=UTC),
        known_hosts_path=root / "known_hosts",
    )
    env = EnvState(
        name=name, host="main", worktree_path=workroot, created_at=datetime(2026, 10, 24, tzinfo=UTC), approved_exec_env=approved
    )
    StateStore(paths.state).write(State(hosts={"main": host}, default_host="main", envs={name: env}))
    return paths, workroot, name


@pytest.mark.parametrize(
    "variable",
    ["HCLOUD_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_OAUTH_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN", "SSH_AUTH_SOCK"],
)
def test_exec_refuses_to_forward_provider_credentials_before_remote_work(tmp_path: Path, variable: str) -> None:
    paths, root, _name = setup(tmp_path, ["CI", variable], ["CI", variable])
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root))])
    with pytest.raises(PreconditionError, match=f"exec.env must not forward provider credentials: {variable}") as caught:
        Executor(paths, runner).execute(ExecOptions(command=["true"]), cwd=root)
    assert caught.value.fix == "Remove them from exec.env in offbox.toml; pass a value for one command with offbox exec --env NAME=value."
    assert len(runner.calls) == 1


def test_custom_provider_credentials_are_refused_and_project_ids_are_allowed(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    custom = paths.config / "providers" / "acme"
    custom.mkdir(parents=True)
    (custom / "provider.toml").write_text('name = "acme"\ndescription = "Acme"\ndriver = "tofu"\ncredentials_env = ["ACME_API_KEY"]\n')
    (paths.config / "providers" / "broken").mkdir()
    (paths.config / "providers" / "broken" / "provider.toml").write_text("not toml")
    names = credential_env_names(paths)
    assert {"ACME_API_KEY", "HCLOUD_TOKEN", "SSH_AUTH_SOCK"} <= names
    assert "GOOGLE_PROJECT" not in names


@pytest.mark.parametrize(("approved", "missing"), [(None, "CI, NPM_TOKEN"), (["CI"], "NPM_TOKEN")])
def test_exec_refuses_exec_env_names_not_approved_by_up(tmp_path: Path, approved: list[str] | None, missing: str) -> None:
    paths, root, _name = setup(tmp_path, ["CI", "NPM_TOKEN"], approved)
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root))])
    with pytest.raises(PreconditionError) as caught:
        Executor(paths, runner).execute(ExecOptions(command=["true"]), cwd=root)
    assert caught.value.message == f"exec.env forwards variables that offbox up has not approved: {missing}"
    assert caught.value.fix == "Review exec.env in offbox.toml, then run offbox up to approve it."
    assert len(runner.calls) == 1


def test_removing_names_from_exec_env_needs_no_new_approval(tmp_path: Path) -> None:
    paths, root, name = setup(tmp_path, ["CI"], ["CI", "NPM_TOKEN"])
    container = {
        "Name": f"/offbox-{name}",
        "Image": "sha256:" + "f" * 64,
        "State": {"Running": True},
        "Config": {"Labels": {"offbox.env": name, "offbox.worktree": str(root)}},
        "NetworkSettings": {"Networks": {"offbox": {"IPAddress": "10.89.0.2"}}},
    }
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(
                prefix=["ssh", "offbox-main"],
                stdout=json.dumps({"containers": [container], "install": "", "compose": "", "compose_failed": False}),
            ),
            Script(prefix=["ssh", "offbox-main"], method="start_stream"),
        ]
    )
    assert Executor(paths, runner).execute(ExecOptions(command=["true"], no_sync=True), cwd=root) == 0


def test_state_written_before_exec_env_approval_still_loads(tmp_path: Path) -> None:
    paths, _root, name = setup(tmp_path, [], None)
    path = paths.state / "state.json"
    data = json.loads(path.read_text())
    del data["envs"][name]["approved_exec_env"]
    path.write_text(json.dumps(data))
    assert StateStore(paths.state).read().envs[name].approved_exec_env is None


def test_override_values_are_redacted_regardless_of_name() -> None:
    parse_env(["DATABASE_URL=postgres://u:hunter2-q81@db", "CI=1"])
    assert redact("url postgres://u:hunter2-q81@db", {}) == "url [REDACTED]"
    assert redact("-e CI=1 exit 1", {}) == "-e [REDACTED] exit 1"


def test_configured_exec_env_values_are_redacted_but_terminal_settings_are_not() -> None:
    execution_env(["SERVICE_URL"], {}, tty=True, local={"SERVICE_URL": "https://user:pw-z53@example.test", "TERM": "xterm-kitty-q81"})
    assert redact("https://user:pw-z53@example.test xterm-kitty-q81", {}) == "[REDACTED] xterm-kitty-q81"


def test_short_forwarded_values_are_redacted_only_as_assignments() -> None:
    execution_env(["HOME", "LANG"], {}, tty=False, local={"HOME": "/Users/me-q71", "LANG": "en_GB.UTF-8-q71"})
    assert redact("-e HOME=/Users/me-q71 cd /Users/me-q71", {}) == "-e [REDACTED] cd /Users/me-q71"
    assert redact("-e LANG=en_GB.UTF-8-q71 en_GB.UTF-8-q71", {}) == "-e [REDACTED] en_GB.UTF-8-q71"


def test_secret_named_and_long_forwarded_values_are_redacted_anywhere() -> None:
    parse_env(["API_TOKEN=short-k42", "SERVICE_URL=https://long-value.test/k42"])
    assert redact("short-k42 https://long-value.test/k42", {}) == "[REDACTED] [REDACTED]"


def test_exec_refuses_unapproved_file_copies_and_git_mirror_before_remote_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OFFBOX_GIT_MIRROR")
    paths, root, _name = setup(tmp_path, [], [])
    (root / "offbox.toml").write_text('[files]\ncopy=["~/.config/gcloud/adc.json"]\n[git]\nmirror=true\nrefs=["origin/dev"]\n')
    runner = FakeRunner([Script(prefix=["git"], stdout=str(root))])
    with pytest.raises(PreconditionError) as caught:
        Executor(paths, runner).execute(ExecOptions(command=["true"]), cwd=root)
    assert caught.value.message == "offbox up has not approved sharing: files.copy ~/.config/gcloud/adc.json, git origin/dev"
    assert len(runner.calls) == 1
