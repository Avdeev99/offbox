import os
import re
import shlex
import tempfile
from collections.abc import Sequence
from pathlib import Path

from offbox import console
from offbox.errors import PreconditionError
from offbox.paths import Paths
from offbox.providers import validate_name
from offbox.state import HostState, State


def write_private(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise PreconditionError(f"Cannot write {path}", "Check directory permissions and free disk space.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_address(address: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", address) is None:
        raise PreconditionError("Invalid SSH address", "Use a hostname or IP address without SSH options or whitespace.")
    return address


def validate_user(user: str) -> str:
    if re.fullmatch(r"[a-z_][a-z0-9_.-]*", user) is None:
        raise PreconditionError("Invalid SSH user", "Use a valid SSH username.")
    return user


def _quote(value: Path) -> str:
    text = str(value)
    if any(char in text for char in "\r\n\0"):
        raise PreconditionError("Unsafe path in SSH configuration", "Choose a path without control characters.")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def ensure_include(paths: Paths, config: Path | None = None) -> None:
    target = config or Path.home() / ".ssh/config"
    include = f"Include {_quote(paths.config / 'ssh_config')}"
    try:
        previous = target.read_text(encoding="utf-8") if target.exists() else ""
    except (OSError, UnicodeError) as exc:
        raise PreconditionError(f"Cannot read SSH configuration {target}", "Check SSH configuration permissions.") from exc
    remaining = [line for line in previous.splitlines(keepends=True) if line.strip() != include]
    updated = include + "\n" + "".join(remaining)
    if updated != previous:
        write_private(target, updated)
        console.info(f"Added offbox Include at the top of {target}")


def sync_config(paths: Paths, state: State) -> None:
    control = paths.ensure_control()
    blocks: list[str] = []
    for name, host in sorted(state.hosts.items()):
        validate_name(name)
        if not host.address:
            continue
        validate_address(host.address)
        validate_user(host.ssh_user)
        key = (host.ssh_key or Path.home() / ".ssh/id_ed25519").expanduser()
        blocks.append(
            f"Host offbox-{name}\n"
            f"  HostName {host.address}\n"
            f"  User {host.ssh_user}\n"
            f"  Port {host.ssh_port}\n"
            f"  IdentityFile {_quote(key)}\n"
            f"  UserKnownHostsFile {_quote(host.known_hosts_path)}\n"
            f"  HostKeyAlias offbox-{name}\n"
            "  HostKeyAlgorithms ssh-ed25519\n"
            "  StrictHostKeyChecking yes\n"
            "  BatchMode yes\n"
            "  ControlMaster auto\n"
            f"  ControlPath {_quote(control / '%C').replace('%%C', '%C')}\n"
            "  ControlPersist 30m\n"
            "  ServerAliveInterval 15\n"
            "  ServerAliveCountMax 4\n"
        )
    write_private(paths.config / "ssh_config", "\n".join(blocks))


def ssh_argv(name: str, argv: Sequence[str] = (), *, options: Sequence[str] = ()) -> list[str]:
    validate_name(name)
    command = ["ssh", *options, f"offbox-{name}"]
    return [*command, "--", shlex.join(argv)] if argv else command


def known_host(name: str, public_key: str) -> str:
    validate_name(name)
    fields = public_key.strip().split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519" or re.fullmatch(r"[A-Za-z0-9+/=]+", fields[1]) is None:
        raise PreconditionError("Invalid ed25519 SSH public key", "Use an ed25519 host key and retry.")
    return f"offbox-{name} {fields[0]} {fields[1]}\n"


def host_key_path(host: HostState) -> Path:
    return host.known_hosts_path.parent / "ssh_host_ed25519_key"
