import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from offbox.paths import Paths
from offbox.ssh import include_line, sync_config, write_private
from offbox.state import HostState, State


def configured(root: Path) -> Paths:
    paths = Paths(root / "cfg", root / "state", root / "cache", root / "control")
    host = HostState(
        name="x",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
        known_hosts_path=root / "state/known hosts",
        ssh_key=root / "key",
    )
    sync_config(paths, State(hosts={"x": host}))
    return paths


def test_generated_host_block_disables_forwarding_and_pins_identity(tmp_path: Path) -> None:
    paths = configured(tmp_path)
    lines = (paths.config / "ssh_config").read_text().splitlines()
    assert lines[:4] == ["Host offbox-x", "  HostName 192.0.2.1", "  User offbox", "  Port 22"]
    assert f'  UserKnownHostsFile "{tmp_path}/state/known hosts"' in lines
    for option in ("  IdentitiesOnly yes", "  ForwardAgent no", "  ForwardX11 no", "  StrictHostKeyChecking yes"):
        assert option in lines
    assert (paths.config / "ssh_config").stat().st_mode & 0o777 == 0o600


def test_write_private_follows_symlink_and_keeps_mode(tmp_path: Path) -> None:
    target = tmp_path / "dotfiles/ssh_config"
    target.parent.mkdir()
    target.write_text("old\n")
    target.chmod(0o644)
    link = tmp_path / "ssh/config"
    link.parent.mkdir()
    link.symlink_to(target)
    write_private(link, "new\n")
    assert link.is_symlink()
    assert target.read_text() == "new\n"
    assert target.stat().st_mode & 0o777 == 0o644
    assert sorted(path.name for path in target.parent.iterdir()) == ["ssh_config"]


def test_write_private_creates_new_files_owner_only(tmp_path: Path) -> None:
    created = tmp_path / "new/config"
    write_private(created, "content\n")
    assert created.read_text() == "content\n"
    assert created.stat().st_mode & 0o777 == 0o600


@pytest.mark.subprocess
@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs the OpenSSH client for ssh -G")
def test_hostile_wildcard_after_include_cannot_enable_forwarding(tmp_path: Path) -> None:
    paths = configured(tmp_path / "home dir")
    config = tmp_path / "config"
    config.write_text(include_line(paths) + "\nHost *\n  ForwardAgent yes\n  ForwardX11 yes\n  IdentitiesOnly no\n")
    resolved = subprocess.run(["ssh", "-G", "-F", str(config), "offbox-x"], capture_output=True, text=True, check=True, timeout=10).stdout
    lines = resolved.splitlines()
    assert {"forwardagent no", "forwardx11 no", "identitiesonly yes"} <= set(lines)
    assert f"userknownhostsfile {tmp_path}/home dir/state/known hosts" in lines
