import hashlib
import io
import os
import platform
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from offbox.config import Model
from offbox.errors import PreconditionError
from offbox.runner import Runner

TOFU_VERSION = "1.10.6"
MUTAGEN_MIN = (0, 18)
TOFU_SHA256 = {
    ("darwin", "amd64"): "85115c8ec23b48b425259060347ff932355dad48056fa83ade05f7d890dc43ed",
    ("darwin", "arm64"): "6da8ee57f20113c302baeddc1f0c6340589fa1f7af04d41282e406cfa53c0b59",
    ("linux", "amd64"): "15b7bed76420b50da3e121769c43341df8cd57d751ca14e6dbe9c850124c6dac",
    ("linux", "arm64"): "a32f653d686a8cad9b9be82101eb5b5e834fbfa8d095842fa1820c5d27fad967",
}
_VERSION = re.compile(r"(?:OpenSSH_|(?:OpenTofu|Terraform)\s+v?|(?:git|mutagen)\s+version\s+v?)(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)


class Tool(Model):
    name: str
    path: Path
    version: str


def download(url: str, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            content: object = response.read()
            if not isinstance(content, bytes):
                raise PreconditionError("Invalid OpenTofu response", "Install OpenTofu >=1.8 on PATH.")
            return content
    except (OSError, urllib.error.URLError) as exc:
        raise PreconditionError("Cannot download OpenTofu", "Check your network or install OpenTofu >=1.8 on PATH.") from exc


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


def install_tofu(
    cache: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
    fetch: Callable[[str, float], bytes] = download,
) -> Path:
    os_name = (system or platform.system()).lower()
    arch_name = (machine or platform.machine()).lower()
    arch_name = {"x86_64": "amd64", "aarch64": "arm64"}.get(arch_name, arch_name)
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


class ToolDiscovery:
    def __init__(self, runner: Runner, cache: Path, lookup: Callable[[str], str | None] = shutil.which) -> None:
        self.runner = runner
        self.cache = cache
        self.lookup = lookup

    def check(self, name: str) -> Tool:
        executable = self.lookup(name)
        fix = {
            "ssh": "Install OpenSSH >=8.0.",
            "git": "Install git >=2.30.",
            "mutagen": "Install Mutagen 0.18.x: brew install mutagen-io/mutagen/mutagen (macOS), or https://github.com/mutagen-io/mutagen/releases.",
            "tofu": "Install OpenTofu >=1.8 or Terraform >=1.6.",
        }.get(name, f"Install {name}.")
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
            raise PreconditionError(f"Unsupported {name} version: {match.group(0)}", fix)
        return Tool(name=name, path=Path(executable), version=".".join(part for part in match.groups() if part is not None))
