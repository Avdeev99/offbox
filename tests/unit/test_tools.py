import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from offbox import tools
from offbox.errors import PreconditionError
from offbox.runner import FakeRunner, Script
from offbox.tools import ToolDiscovery, install_tofu


@pytest.mark.parametrize(
    ("name", "version", "flag"),
    [
        ("ssh", "OpenSSH_9.9p2, LibreSSL", "-V"),
        ("git", "git version 2.49.0", "--version"),
        ("mutagen", "mutagen version 0.18.1", "--version"),
        ("tofu", "OpenTofu v1.10.6", "--version"),
    ],
)
def test_tool_versions_and_exact_argv(tmp_path: Path, name: str, version: str, flag: str) -> None:
    runner = FakeRunner([Script(prefix=[f"/bin/{name}"], stdout="" if name == "ssh" else version, stderr=version if name == "ssh" else "")])
    tool = ToolDiscovery(runner, tmp_path, lookup=lambda value: f"/bin/{value}").check(name)
    assert tool.name == name
    assert tool.path == Path(f"/bin/{name}")
    assert runner.calls[0].argv == [f"/bin/{name}", flag]
    assert runner.calls[0].timeout == 10


@pytest.mark.parametrize(
    ("name", "output"),
    [
        ("ssh", "OpenSSH_7.9"),
        ("git", "git version 2.29.0"),
        ("mutagen", "mutagen version 0.17.0"),
        ("mutagen", "mutagen version 0.19.0"),
        ("tofu", "OpenTofu v1.7.9"),
    ],
)
def test_unsupported_versions(tmp_path: Path, name: str, output: str) -> None:
    runner = FakeRunner([Script(prefix=[name], stdout=output)])
    with pytest.raises(PreconditionError, match="Unsupported"):
        ToolDiscovery(runner, tmp_path, lookup=lambda value: value).check(name)


def test_missing_mutagen_never_downloads(tmp_path: Path) -> None:
    with pytest.raises(PreconditionError, match="Missing tool: mutagen"):
        ToolDiscovery(FakeRunner(), tmp_path, lookup=lambda _: None).check("mutagen")
    assert list(tmp_path.iterdir()) == []


def test_terraform_preferred_to_download(tmp_path: Path) -> None:
    runner = FakeRunner([Script(prefix=["/bin/terraform"], stdout="Terraform v1.6.0")])
    tool = ToolDiscovery(runner, tmp_path, lookup=lambda name: "/bin/terraform" if name == "terraform" else None).check("tofu")
    assert tool.name == "terraform"
    assert runner.calls[0].argv == ["/bin/terraform", "--version"]
    assert list(tmp_path.iterdir()) == []


def test_unknown_version_output(tmp_path: Path) -> None:
    runner = FakeRunner([Script(prefix=["git"], stdout="surprise")])
    with pytest.raises(PreconditionError, match="Cannot determine git"):
        ToolDiscovery(runner, tmp_path, lookup=lambda name: name).check("git")


def test_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    observed: list[tuple[str, float]] = []

    def fetch(url: str, timeout: float) -> bytes:
        observed.append((url, timeout))
        return b"untrusted archive"

    with pytest.raises(PreconditionError, match="checksum mismatch"):
        install_tofu(tmp_path, system="Linux", machine="x86_64", fetch=fetch)
    assert observed == [
        (f"https://github.com/opentofu/opentofu/releases/download/v{tools.TOFU_VERSION}/tofu_{tools.TOFU_VERSION}_linux_amd64.zip", 60.0)
    ]
    assert not (tmp_path / "tools/tofu" / tools.TOFU_VERSION / "tofu").exists()


def test_verified_cache_restores_binary_and_rejects_archive_tampering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("tofu", b"verified binary")
    content = buffer.getvalue()
    monkeypatch.setitem(tools.TOFU_SHA256, ("darwin", "arm64"), hashlib.sha256(content).hexdigest())
    observed: list[str] = []

    def fetch(url: str, timeout: float) -> bytes:
        observed.append(url)
        return content

    executable = install_tofu(tmp_path, system="Darwin", machine="arm64", fetch=fetch)
    assert executable.read_bytes() == b"verified binary"
    assert executable.stat().st_mode & 0o777 == 0o700
    executable.write_bytes(b"tampered")
    assert install_tofu(tmp_path, system="Darwin", machine="arm64", fetch=fetch).read_bytes() == b"verified binary"
    assert len(observed) == 1
    archive_path = executable.parent / f"tofu_{tools.TOFU_VERSION}_darwin_arm64.zip"
    archive_path.write_bytes(b"corrupt cache")
    with pytest.raises(PreconditionError, match="checksum mismatch"):
        install_tofu(tmp_path, system="Darwin", machine="arm64", fetch=fetch)


def test_unsupported_platform(tmp_path: Path) -> None:
    with pytest.raises(PreconditionError, match="Unsupported OpenTofu platform"):
        install_tofu(tmp_path, system="Windows", machine="arm64")
