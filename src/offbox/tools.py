import hashlib
import io
import os
import platform
import re
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path

from offbox import console
from offbox.config import Model
from offbox.errors import PreconditionError
from offbox.runner import Runner
from offbox.timings import measured

TOFU_VERSION = "1.10.6"
MUTAGEN_MIN = (0, 18)
MUTAGEN_VERSION = "0.18.1"
MUTAGEN_FIX = "Run offbox setup to download Mutagen, or install Mutagen 0.18.x."
TOFU_SHA256 = {
    ("darwin", "amd64"): "85115c8ec23b48b425259060347ff932355dad48056fa83ade05f7d890dc43ed",
    ("darwin", "arm64"): "6da8ee57f20113c302baeddc1f0c6340589fa1f7af04d41282e406cfa53c0b59",
    ("linux", "amd64"): "15b7bed76420b50da3e121769c43341df8cd57d751ca14e6dbe9c850124c6dac",
    ("linux", "arm64"): "a32f653d686a8cad9b9be82101eb5b5e834fbfa8d095842fa1820c5d27fad967",
}
MUTAGEN_SHA256 = {
    ("darwin", "amd64"): "7d06f7d8fcfe90bc7e55cc834a2f2f20c2e0af9ea9bc35911fc4341ad56a9bbf",
    ("darwin", "arm64"): "6f810416d9e5fc4fd5e18431146f8b3c5a2056ba5a24f76c1e66da86eb3257e2",
    ("linux", "amd64"): "7735286c778cc438418209f24d03a64f3a0151c8065ef0fe079cfaf093af6f8f",
    ("linux", "arm64"): "bcba735aebf8cbc11da9b3742118a665599ac697fa06bc5751cac8dcd540db8a",
}
_MUTAGEN_MEMBERS = ("mutagen", "mutagen-agents.tar.gz")
_VERSION = re.compile(r"(?:OpenSSH_|(?:OpenTofu|Terraform)\s+v?|(?:git|mutagen)\s+version\s+v?)(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)


class Tool(Model):
    name: str
    path: Path
    version: str


class MutagenMissing(PreconditionError):
    pass


def download(url: str, timeout: float) -> bytes:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            content: object = response.read()
            if not isinstance(content, bytes):
                raise PreconditionError(f"Invalid download response from {url}", "Retry, or install the tool on PATH.")
            return content
    except OSError as exc:
        raise PreconditionError(f"Cannot download {url}", "Check your network and retry, or install the tool on PATH.") from exc


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _platform(system: str | None, machine: str | None) -> tuple[str, str]:
    arch_name = (machine or platform.machine()).lower()
    return (system or platform.system()).lower(), {"x86_64": "amd64", "aarch64": "arm64"}.get(arch_name, arch_name)


def install_tofu(
    cache: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
    fetch: Callable[[str, float], bytes] = download,
) -> Path:
    os_name, arch_name = _platform(system, machine)
    checksum = TOFU_SHA256.get((os_name, arch_name))
    if checksum is None:
        raise PreconditionError(f"Unsupported OpenTofu platform: {os_name}/{arch_name}", "Install OpenTofu >=1.8 on PATH.")
    directory = cache / "tools" / "tofu" / TOFU_VERSION
    archive = directory / f"tofu_{TOFU_VERSION}_{os_name}_{arch_name}.zip"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = (
            archive.read_bytes()
            if archive.exists()
            else fetch(
                f"https://github.com/opentofu/opentofu/releases/download/v{TOFU_VERSION}/{archive.name}",
                60.0,
            )
        )
        if hashlib.sha256(content).hexdigest() != checksum:
            raise PreconditionError("OpenTofu checksum mismatch", f"Remove {archive} and retry, or install OpenTofu on PATH.")
        with zipfile.ZipFile(io.BytesIO(content)) as package:
            binary = package.read("tofu")
        executable = directory / "tofu"
        _atomic_write(archive, content, 0o600)
        _atomic_write(executable, binary, 0o700)
        return executable
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise PreconditionError("Cannot install verified OpenTofu", f"Clear {directory} and retry, or install OpenTofu on PATH.") from exc


def mutagen_directory(cache: Path) -> Path:
    return cache / "tools" / "mutagen" / MUTAGEN_VERSION


def cached_mutagen(cache: Path) -> Path | None:
    directory = mutagen_directory(cache)
    executable = directory / "mutagen"
    return executable if executable.is_file() and (directory / "mutagen-agents.tar.gz").is_file() else None


def download_prompt(cache: Path) -> str:
    return f"Mutagen {MUTAGEN_VERSION} not found. Download it (verified) into {mutagen_directory(cache)}?"


def install_mutagen(
    cache: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
    fetch: Callable[[str, float], bytes] = download,
) -> Path:
    """Download, verify and unpack the pinned Mutagen release; its agent bundle must sit beside the binary."""
    os_name, arch_name = _platform(system, machine)
    checksum = MUTAGEN_SHA256.get((os_name, arch_name))
    if checksum is None:
        raise PreconditionError(f"Unsupported Mutagen platform: {os_name}/{arch_name}", "Install Mutagen 0.18.x on PATH.")
    directory = mutagen_directory(cache)
    name = f"mutagen_{os_name}_{arch_name}_v{MUTAGEN_VERSION}.tar.gz"
    console.step(f"Downloading Mutagen {MUTAGEN_VERSION} (about 100 MB)")
    content = fetch(f"https://github.com/mutagen-io/mutagen/releases/download/v{MUTAGEN_VERSION}/{name}", 300.0)
    if hashlib.sha256(content).hexdigest() != checksum:
        raise PreconditionError("Mutagen checksum mismatch", "Retry offbox setup, or install Mutagen 0.18.x on PATH.")
    executable = directory / "mutagen"
    try:
        members: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as package:
            for member_name in _MUTAGEN_MEMBERS:
                member = package.getmember(member_name)
                extracted = package.extractfile(member) if member.isfile() else None
                if extracted is None:
                    raise PreconditionError(f"Invalid Mutagen archive member: {member_name}", "Install Mutagen 0.18.x on PATH.")
                members[member_name] = extracted.read()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write(directory / "mutagen-agents.tar.gz", members["mutagen-agents.tar.gz"], 0o644)
        _atomic_write(executable, members["mutagen"], 0o755)
    except (OSError, tarfile.TarError, KeyError) as exc:
        raise PreconditionError(
            "Cannot install verified Mutagen", f"Clear {directory} and retry, or install Mutagen 0.18.x on PATH."
        ) from exc
    console.success(f"Installed Mutagen {MUTAGEN_VERSION} at {executable}")
    return executable


def install_hint(name: str, system: str | None = None, release: Mapping[str, str] | None = None) -> str:
    os_name = (system or platform.system()).lower()
    if os_name == "darwin":
        return "Run xcode-select --install or brew install git." if name == "git" else "Run brew install openssh."
    if release is None:
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
    family = {release.get("ID", ""), *release.get("ID_LIKE", "").split()}
    if family & {"debian", "ubuntu"}:
        return "Run sudo apt install git openssh-client."
    if family & {"fedora", "rhel", "centos"}:
        return "Run sudo dnf install git openssh-clients."
    return "Install git >=2.30 and OpenSSH >=8.0 with your package manager."


class ToolDiscovery:
    def __init__(self, runner: Runner, cache: Path, lookup: Callable[[str], str | None] = shutil.which) -> None:
        self.runner = runner
        self.cache = cache
        self.lookup = lookup

    @measured("Local tool checks")
    def check(self, name: str) -> Tool:
        executable = self.lookup(name)
        if name in ("ssh", "git"):
            fix = f"{'OpenSSH >=8.0' if name == 'ssh' else 'git >=2.30'} is required. {install_hint(name)}"
        elif name == "mutagen":
            fix = MUTAGEN_FIX
        elif name == "tofu":
            fix = "Install OpenTofu >=1.8 or Terraform >=1.6."
        else:
            fix = f"Install {name}."
        if name == "mutagen" and executable is None:
            cached = cached_mutagen(self.cache)
            if cached is None:
                raise MutagenMissing("Missing tool: mutagen", fix)
            executable = str(cached)
        if name == "tofu" and executable is None:
            executable = self.lookup("terraform")
            if executable is not None:
                name = "terraform"
            else:
                executable = str(install_tofu(self.cache))
        if executable is None:
            raise PreconditionError(f"Missing tool: {name}", fix)
        result = self.runner.run([executable, "-V" if name == "ssh" else "--version"], check=False, timeout=10)
        match = _VERSION.search(result.stdout + "\n" + result.stderr)
        if result.returncode != 0 or match is None:
            raise PreconditionError(f"Cannot determine {name} version", fix)
        found = (int(match[1]), int(match[2]))
        minimum = {"ssh": (8, 0), "git": (2, 30), "mutagen": MUTAGEN_MIN, "tofu": (1, 8), "terraform": (1, 6)}[name]
        if found < minimum or (name == "mutagen" and found != MUTAGEN_MIN):
            if name == "mutagen":
                fix = f"Install Mutagen 0.18.x, or run mutagen daemon stop, remove {executable} from PATH and run offbox setup."
            raise PreconditionError(f"Unsupported {name} version: {match.group(0)}", fix)
        return Tool(name=name, path=Path(executable), version=".".join(part for part in match.groups() if part is not None))
