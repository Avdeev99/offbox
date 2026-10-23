import logging
import os
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from offbox import DEFAULT_IMAGE, console
from offbox.config import EnvConfig, ProjectConfig, UserConfig, default_host, hetzner_token, load_config, load_credentials
from offbox.errors import CommandFailed, ConnectionError_, OffboxError, PreconditionError, ProviderError, UsageError
from offbox.logging import configure, redact
from offbox.paths import Paths
from offbox.state import State, StateStore


@pytest.mark.parametrize(
    ("error", "code"),
    [(OffboxError("x"), 1), (UsageError("x"), 2), (PreconditionError("x"), 3), (ConnectionError_("x"), 4), (ProviderError("x"), 1)],
)
def test_error_exit_contract(error: OffboxError, code: int) -> None:
    assert error.exit_code == code
    assert error.message == "x"


def test_command_failure_tail() -> None:
    error = CommandFailed(["bad", "arg"], 42, "\n".join(str(i) for i in range(30)))
    assert error.returncode == 42
    assert error.stderr_tail.splitlines() == [str(i) for i in range(10, 30)]
    assert error.argv == ["bad", "arg"]


def test_console_channels(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "")
    console.info("info")
    console.success("yes")
    console.warn("warn")
    console.error("bad", "repair")
    console.step("work")
    console.json_out(State())
    result = capsys.readouterr()
    assert result.err == "info\n✓ yes\n! warn\n✗ bad\n  Fix: repair\n→ work\n"
    assert State.model_validate_json(result.out) == State()
    assert "\x1b" not in result.err


def test_paths_and_control(tmp_path: Path) -> None:
    paths = Paths.discover({}, tmp_path)
    assert paths.user_config == tmp_path / ".config/offbox/config.toml"
    assert paths.state == tmp_path / ".local/state/offbox"
    assert paths.cache == tmp_path / ".cache/offbox"
    assert paths.control == Path(f"/tmp/offbox-{os.getuid()}")
    overridden = Paths.discover(
        {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_STATE_HOME": str(tmp_path / "st"), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    )
    assert overridden.config == tmp_path / "cfg/offbox"
    custom = Paths(paths.config, paths.state, paths.cache, tmp_path / "control")
    assert custom.ensure_control().stat().st_mode & 0o777 == 0o700
    assert paths.log_file(date(2026, 10, 23)).name == "2026-10-23.log"


def test_control_symlink_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(PreconditionError, match="Unsafe SSH"):
        Paths(tmp_path, tmp_path, tmp_path, link).ensure_control()


def test_project_defaults_and_resources(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent", ProjectConfig)
    assert config.env.image == DEFAULT_IMAGE
    assert config.env.shm_size == "2g"
    assert config.env.resource_limits(8, 16000) == (7.0, "12000")
    assert config.env.resource_limits(1, 100) == (1.0, "75")
    assert config.ports.auto is True
    assert config.stack.commands(tmp_path) == (None, None)
    (tmp_path / "compose.yaml").touch()
    assert config.stack.commands(tmp_path) == ("docker compose up -d", "docker compose down")
    assert EnvConfig(dockerfile=Path("Dockerfile")).image is None
    with pytest.raises(ValidationError, match="mutually exclusive"):
        EnvConfig(image="other", dockerfile=Path("Dockerfile"))


def test_project_all_sections(tmp_path: Path) -> None:
    file = tmp_path / "offbox.toml"
    file.write_text(
        '[env]\ncpus=4\nmemory="8g"\n[install]\ncommand="uv sync"\nwhen=["uv.lock"]\n[ports]\nforward=[3000]\nauto=false\n[exec]\nenv=["CI"]\n[sync]\nignore=["data/"]\n[stack]\nup="custom"\n'
    )
    config = load_config(file, ProjectConfig)
    assert config.env.resource_limits(8, 16000) == (4.0, "8g")
    assert config.install.when == ["uv.lock"]
    assert config.ports.forward == [3000]
    assert config.exec.env == ["CI"]
    assert config.sync.ignore == ["data/"]
    assert config.stack.commands(tmp_path) == ("custom", None)


def test_unknown_key_exact_message(tmp_path: Path) -> None:
    file = tmp_path / "offbox.toml"
    file.write_text('[env]\nimgae="wrong"\n')
    with pytest.raises(PreconditionError) as caught:
        load_config(file, ProjectConfig)
    assert caught.value.message == f"Invalid configuration in {file}: env.imgae"
    assert caught.value.fix == f"Correct these keys in {file}."


@pytest.mark.parametrize("data", [b"[broken", b"\xff"])
def test_invalid_config(tmp_path: Path, data: bytes) -> None:
    file = tmp_path / "config.toml"
    file.write_bytes(data)
    with pytest.raises(PreconditionError, match="Cannot read configuration"):
        load_config(file, UserConfig)


def test_user_presets_default_precedence(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text('default_host="main"\n[hosts.main]\nprovider="ssh"\naddress="dev@example.com"\nport=2222\n')
    user = load_config(file, UserConfig)
    assert user.hosts["main"].port == 2222
    assert default_host(user, None) == "main"
    assert default_host(user, "other") == "other"


def test_credentials_permissions_env_precedence(tmp_path: Path) -> None:
    file = tmp_path / "credentials.toml"
    file.write_text('[hetzner]\ntoken="fake-token-for-test"\n')
    file.chmod(0o644)
    assert hetzner_token(file, {"HCLOUD_TOKEN": "env-token"}).get_secret_value() == "env-token"
    with pytest.raises(PreconditionError, match="more permissive"):
        load_credentials(file)
    file.chmod(0o600)
    assert hetzner_token(file, {}).get_secret_value() == "fake-token-for-test"
    assert "fake-token-for-test" not in repr(load_credentials(file))
    file.unlink()
    with pytest.raises(PreconditionError, match="Missing Hetzner"):
        hetzner_token(file, {})


def test_state_atomic_transaction_and_invalid_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    assert store.read() == State()
    with store.transaction() as state:
        state.default_host = "main"
    assert store.read().default_host == "main"
    with pytest.raises(UsageError), store.transaction() as state:
        state.default_host = "discarded"
        raise UsageError("abort")
    assert store.read().default_host == "main"
    assert list(tmp_path.glob(".state-*")) == []
    assert store.path.stat().st_mode & 0o777 == 0o600
    store.path.write_text('{"version":2}')
    with pytest.raises(PreconditionError, match="Cannot read state"):
        store.read()


def test_logging_rotation_and_redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path, tmp_path, tmp_path, tmp_path)
    paths.logs.mkdir()
    for name in ("2026-10-08.log", "2026-10-09.log", "2026-10-10.log", "other.log"):
        (paths.logs / name).touch()
    monkeypatch.setenv("SAMPLE_API_TOKEN", "log-test-token")
    target = configure(paths, ["exec", "log-test-token"], date(2026, 10, 23))
    logging.getLogger("offbox.test").warning("failure log-test-token")
    text = target.read_text()
    assert "log-test-token" not in text
    assert "[REDACTED]" in text
    assert not (paths.logs / "2026-10-09.log").exists()
    assert (paths.logs / "2026-10-10.log").exists()
    assert (paths.logs / "other.log").exists()
    assert (
        redact("a b c d e", {"A_TOKEN": "a", "B_secret": "b", "C_PASSWORD": "c", "D_KEY": "d", "OTHER": "e"})
        == "[REDACTED] [REDACTED] [REDACTED] [REDACTED] e"
    )
