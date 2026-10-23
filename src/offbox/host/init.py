import base64
import binascii
import hashlib
import shlex
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, field_validator

from offbox import DEFAULT_IMAGE, __version__, console
from offbox.config import Model
from offbox.errors import OffboxError, PreconditionError
from offbox.host import host_operation, public_key
from offbox.host.cloudinit import bootstrap_script
from offbox.host.ready import Clock, tcp_probe, wait_ready
from offbox.paths import Paths
from offbox.providers import copy_provider, discover_provider, validate_name
from offbox.runner import Runner
from offbox.ssh import ensure_include, known_host, sync_config, validate_address, validate_user, write_private
from offbox.state import HostState, StateStore


class InitOptions(Model):
    target: str
    name: str = "main"
    port: int = Field(default=22, ge=1, le=65535)
    ssh_key: Path | None = None
    accept_host_key: bool = False

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return validate_name(value)


def _init_host(
    paths: Paths,
    runner: Runner,
    options: InitOptions,
    *,
    confirm: Callable[[str], bool] | None = None,
    ssh_config: Path | None = None,
    clock: Clock | None = None,
    probe: Callable[[str, int, float], bool] = tcp_probe,
) -> HostState:
    if not options.accept_host_key and confirm is None:
        raise PreconditionError(
            "Host key confirmation is required: --accept-host-key", "Review the host fingerprint and pass --accept-host-key."
        )
    if options.target.count("@") != 1:
        raise PreconditionError("Expected USER@ADDRESS", "Run offbox host init USER@ADDRESS.")
    owner, address = options.target.split("@", 1)
    validate_user(owner)
    address = validate_address(address.strip("[]"))
    key = (options.ssh_key or Path.home() / ".ssh/id_ed25519").expanduser().resolve()
    scan = runner.run(["ssh-keyscan", "-T", "5", "-p", str(options.port), "-t", "ed25519", address], timeout=10)
    keys = {
        " ".join(line.split()[1:3])
        for line in scan.stdout.splitlines()
        if line.strip() and not line.startswith("#") and len(line.split()) >= 3
    }
    if len(keys) != 1:
        raise PreconditionError("Cannot determine a unique ed25519 host key", "Check the SSH address and its advertised host keys.")
    scanned_key = keys.pop()
    pinned = known_host(options.name, scanned_key)
    try:
        digest = base64.b64encode(hashlib.sha256(base64.b64decode(scanned_key.split()[1], validate=True)).digest()).decode().rstrip("=")
    except (ValueError, binascii.Error) as exc:
        raise PreconditionError("Invalid SSH host key", "Check the server's ed25519 key.") from exc
    console.info(f"Host {address} fingerprint SHA256:{digest}")
    if not options.accept_host_key and (confirm is None or not confirm(f"Trust SHA256:{digest} for {address}?")):
        raise PreconditionError("Host key was not accepted", "Verify the fingerprint and rerun offbox host init.")
    authorized_key = public_key(runner, key)
    directory = paths.state / "hosts" / options.name
    provider = discover_provider("ssh", paths)
    if provider.manifest.driver != "none":
        raise PreconditionError("The ssh provider must use driver none", "Correct the ssh provider manifest.")
    store = StateStore(paths.state)
    with store.transaction() as state:
        previous = state.hosts.get(options.name)
        if previous is not None:
            if previous.provider != "ssh" or previous.address != address or previous.ssh_port != options.port or previous.phase == "ready":
                raise PreconditionError(
                    f"Host {options.name} already exists", f"Run offbox host destroy {options.name} or choose another name."
                )
            if previous.known_hosts_path.read_text(encoding="utf-8") != pinned:
                raise PreconditionError(
                    "The existing host key has changed", "Verify the server identity before changing its stored host key."
                )
            host = previous
            host.ssh_key = key
        else:
            if directory.exists():
                raise PreconditionError(
                    f"Host directory already exists: {directory}", "Inspect the existing directory or choose another name."
                )
            directory.mkdir(parents=True, mode=0o700)
            workspace = directory / "provider"
            digest_files = copy_provider(provider, workspace)
            host = HostState(
                name=options.name,
                provider="ssh",
                address=address,
                ssh_user="offbox",
                ssh_port=options.port,
                created_at=datetime.now(UTC),
                provider_dir=workspace,
                provider_files_hash=digest_files,
                known_hosts_path=directory / "known_hosts",
                ssh_key=key,
                phase="initializing",
            )
        state.hosts[host.name] = host
        write_private(host.known_hosts_path, pinned)
        sync_config(paths, state)
        ensure_include(paths, ssh_config)
    command = ["sudo", "-n", "env", "OFFBOX_HARDEN=0", f"OFFBOX_VERSION={__version__}", "sh", "-s", "--", DEFAULT_IMAGE, authorized_key]
    try:
        runner.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "HostKeyAlgorithms=ssh-ed25519",
                "-o",
                f"HostKeyAlias=offbox-{host.name}",
                "-o",
                f"UserKnownHostsFile={host.known_hosts_path}",
                "-i",
                str(key),
                "-p",
                str(options.port),
                f"{owner}@{address}",
                "--",
                shlex.join(command),
            ],
            input=bootstrap_script(),
            timeout=900,
        )
        wait_ready(host.name, host.address, host.ssh_port, runner, paths.log_file(), clock=clock, probe=probe)
        host.phase = "ready"
        with store.transaction() as state:
            state.hosts[host.name] = host
            if state.default_host is None:
                state.default_host = host.name
    except OffboxError as exc:
        raise OffboxError(
            f"Initialising host {host.name} failed: {exc.message}",
            "Correct the reported problem and rerun the same host init command; its registration was retained.",
            exc.exit_code,
        ) from exc
    console.success(f"Host {host.name}: {host.address}")
    return host


def init_host(
    paths: Paths,
    runner: Runner,
    options: InitOptions,
    *,
    confirm: Callable[[str], bool] | None = None,
    ssh_config: Path | None = None,
    clock: Clock | None = None,
    probe: Callable[[str, int, float], bool] = tcp_probe,
) -> HostState:
    with host_operation(paths, options.name):
        return _init_host(paths, runner, options, confirm=confirm, ssh_config=ssh_config, clock=clock, probe=probe)
