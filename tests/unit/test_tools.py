import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox import tools
from offbox.errors import PreconditionError
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
    with pytest.raises(tools.MutagenMissing, match="Missing tool: mutagen") as caught:
        ToolDiscovery(FakeRunner(), tmp_path, lookup=lambda _: None).check("mutagen")
    assert caught.value.fix == "Run offbox setup to download Mutagen, or install Mutagen 0.18.x."
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


def mutagen_archive(members: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if symlink is not None:
            link = tarfile.TarInfo(symlink)
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive.addfile(link)
    return buffer.getvalue()


def test_mutagen_download_is_verified_and_unpacks_binary_beside_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = mutagen_archive({"mutagen": b"binary", "mutagen-agents.tar.gz": b"agents", "../escape": b"ignored"})
    monkeypatch.setitem(tools.MUTAGEN_SHA256, ("linux", "arm64"), hashlib.sha256(content).hexdigest())
    observed: list[tuple[str, float]] = []

    def fetch(url: str, timeout: float) -> bytes:
        observed.append((url, timeout))
        return content

    executable = tools.install_mutagen(tmp_path, system="Linux", machine="aarch64", fetch=fetch)
    assert observed == [("https://github.com/mutagen-io/mutagen/releases/download/v0.18.1/mutagen_linux_arm64_v0.18.1.tar.gz", 300.0)]
    assert executable == tmp_path / "tools/mutagen/0.18.1/mutagen"
    assert executable.read_bytes() == b"binary"
    assert executable.stat().st_mode & 0o777 == 0o755
    assert (executable.parent / "mutagen-agents.tar.gz").read_bytes() == b"agents"
    assert sorted(path.name for path in executable.parent.iterdir()) == ["mutagen", "mutagen-agents.tar.gz"]
    assert not (tmp_path / "tools/mutagen/escape").exists()
    assert tools.cached_mutagen(tmp_path) == executable


def test_mutagen_checksum_mismatch_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(PreconditionError, match="Mutagen checksum mismatch"):
        tools.install_mutagen(tmp_path, system="Darwin", machine="arm64", fetch=lambda _url, _timeout: b"untrusted archive")
    assert list(tmp_path.iterdir()) == []


def test_mutagen_archive_member_must_be_a_regular_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = mutagen_archive({"mutagen-agents.tar.gz": b"agents"}, symlink="mutagen")
    monkeypatch.setitem(tools.MUTAGEN_SHA256, ("darwin", "amd64"), hashlib.sha256(content).hexdigest())
    with pytest.raises(PreconditionError, match="Invalid Mutagen archive member: mutagen"):
        tools.install_mutagen(tmp_path, system="Darwin", machine="x86_64", fetch=lambda _url, _timeout: content)
    assert tools.cached_mutagen(tmp_path) is None


def test_mutagen_on_path_wins_over_the_cache(tmp_path: Path) -> None:
    directory = tools.mutagen_directory(tmp_path)
    directory.mkdir(parents=True)
    (directory / "mutagen").write_bytes(b"binary")
    (directory / "mutagen-agents.tar.gz").write_bytes(b"agents")
    runner = FakeRunner([Script(prefix=["/usr/bin/mutagen"], stdout="Mutagen version 0.18.0")])
    tool = ToolDiscovery(runner, tmp_path, lookup=lambda _name: "/usr/bin/mutagen").check("mutagen")
    assert tool.path == Path("/usr/bin/mutagen")


def test_wrong_mutagen_version_on_path_explains_the_daemon_switch(tmp_path: Path) -> None:
    runner = FakeRunner([Script(prefix=["/opt/mutagen"], stdout="Mutagen version 0.17.6")])
    with pytest.raises(PreconditionError, match="Unsupported mutagen") as caught:
        ToolDiscovery(runner, tmp_path, lookup=lambda _name: "/opt/mutagen").check("mutagen")
    assert caught.value.fix == "Install Mutagen 0.18.x, or run mutagen daemon stop, remove /opt/mutagen from PATH and run offbox setup."


@pytest.mark.parametrize(
    ("system", "release", "hint"),
    [
        ("Darwin", {}, "Run xcode-select --install or brew install git."),
        ("Linux", {"ID": "ubuntu", "ID_LIKE": "debian"}, "Run sudo apt install git openssh-client."),
        ("Linux", {"ID": "fedora"}, "Run sudo dnf install git openssh-clients."),
        ("Linux", {"ID": "arch"}, "Install git >=2.30 and OpenSSH >=8.0 with your package manager."),
    ],
)
def test_vcs_install_hint_matches_the_platform(system: str, release: dict[str, str], hint: str) -> None:
    assert tools.install_hint("git", system, release) == hint
