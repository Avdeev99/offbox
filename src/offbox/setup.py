from collections.abc import Callable
from pathlib import Path
from typing import Literal

from offbox import console
from offbox.config import Model
from offbox.doctor import NO_DEFAULT_HOST, Check, diagnose
from offbox.errors import OffboxError, PreconditionError
from offbox.host.create import CreateOptions, create_host
from offbox.host.init import InitOptions, init_host
from offbox.paths import Paths
from offbox.providers import credentials_env, discover_provider
from offbox.runner import Runner
from offbox.ssh import default_ssh_key, ensure_include, sync_config
from offbox.state import StateStore
from offbox.tools import ToolDiscovery, download_prompt, install_mutagen

INSTALLATION_DOCS = "https://github.com/o-avdieiev/offbox/blob/master/docs/installation.md"
KEYGEN_COMMAND = 'ssh-keygen -t ed25519 -N "" -C offbox -f ~/.ssh/offbox_ed25519'


class SetupOptions(Model):
    provider: Literal["hetzner", "gcp", "ssh"] | None = None
    action: Literal["check", "create", "init"] | None = None
    name: str = "main"
    ssh_key: Path | None = None
    size: str | None = None
    region: str | None = None
    target: str | None = None
    port: int = 22
    accept_host_key: bool = False
    host_key_fingerprint: str | None = None


def credential_fix(provider: str, paths: Paths) -> str | None:
    if provider == "hetzner":
        return (
            f"export HCLOUD_TOKEN=... in your shell profile, or set token under [hetzner] in {paths.credentials} (chmod 600); "
            f"see {INSTALLATION_DOCS}#hetzner"
        )
    if provider == "gcp":
        return f"export GOOGLE_PROJECT=your-project-id and run gcloud auth application-default login; see {INSTALLATION_DOCS}#google-cloud"
    return None


def select_key(runner: Runner, requested: Path | None, offer: Callable[[str], bool] | None, *, create: bool) -> Path | None:
    """Return the SSH key setup should use, or None to keep the ~/.ssh/id_ed25519 default."""
    if requested is not None:
        return requested
    key = default_ssh_key()
    if key.name == "offbox_ed25519":
        console.info(f"Using SSH key {key}")
        return key
    if key.exists() or key.with_name(key.name + ".pub").exists():
        return None
    directory = key.parent
    dedicated = directory / "offbox_ed25519"
    if not create:
        return None
    if offer is None:
        raise PreconditionError("No SSH key found", f"Run {KEYGEN_COMMAND}, then rerun offbox setup.")
    if not offer("No SSH key found. Create a dedicated key ~/.ssh/offbox_ed25519 for offbox hosts?"):
        raise PreconditionError("No SSH key found", f"Run {KEYGEN_COMMAND}, or pass --ssh-key PATH.")
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise PreconditionError(f"Cannot create {directory}", f"Create {directory} with mode 0700 and retry.") from exc
    runner.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "offbox", "-f", str(dedicated)], timeout=30)
    console.success(f"Created SSH key {dedicated}")
    return dedicated


def _offer_mutagen(paths: Paths, tools: ToolDiscovery, checks: list[Check], offer: Callable[[str], bool] | None) -> list[Check]:
    if offer is None or not any(check.name == "mutagen" and check.message == "Missing tool: mutagen" for check in checks):
        return checks
    if not offer(download_prompt(paths.cache)):
        return checks
    install_mutagen(paths.cache)
    try:
        tool = tools.check("mutagen")
        replacement = Check(name="mutagen", ok=True, message=f"{tool.name} {tool.version} ({tool.path})")
    except OffboxError as exc:
        replacement = Check(name="mutagen", ok=False, message=exc.message, fix=exc.fix)
    return [replacement if check.name == "mutagen" else check for check in checks]


def setup(
    paths: Paths,
    runner: Runner,
    options: SetupOptions,
    *,
    ask: Callable[[str, str], str] | None = None,
    confirm: Callable[[str], bool] | None = None,
    offer: Callable[[str], bool] | None = None,
    ssh_config: Path | None = None,
    tools: ToolDiscovery | None = None,
) -> None:
    provider = options.provider
    if provider is None:
        if ask is None:
            raise PreconditionError(
                "Setup needs a provider", "Pass --provider hetzner, --provider gcp or --provider ssh with an explicit --action."
            )
        choice = ask("Provider (hetzner, gcp or ssh)", "hetzner")
        if choice not in ("hetzner", "gcp", "ssh"):
            raise PreconditionError("Unknown setup provider", "Pass --provider hetzner, --provider gcp or --provider ssh.")
        provider = "hetzner" if choice == "hetzner" else "gcp" if choice == "gcp" else "ssh"
    action = options.action
    if action is None:
        if confirm is None:
            raise PreconditionError("Setup needs an explicit action", "Pass --action check, --action create or --action init.")
        action = (
            ("create" if provider != "ssh" else "init")
            if confirm("Create a billable cloud host?" if provider != "ssh" else "Bootstrap an existing SSH host?")
            else "check"
        )
    discovery = tools or ToolDiscovery(runner, paths.cache)
    ssh_key = select_key(runner, options.ssh_key, offer, create=action != "check")
    report = diagnose(paths, runner, ssh_key=ssh_key, tools=discovery)
    report.checks = _offer_mutagen(paths, discovery, report.checks, offer)
    fatal: list[Check] = []
    for check in report.checks:
        required = not (check.name == "host" and (action != "check" or check.message == NO_DEFAULT_HOST)) and not (
            check.name == "tofu" and not (provider != "ssh" and action == "create")
        )
        if check.ok:
            console.success(check.message)
        elif required:
            fatal.append(check)
            console.error(check.message, check.fix)
        else:
            console.warn(f"{check.message}. Fix: {check.fix}")
    if fatal:
        raise PreconditionError("Setup prerequisites are not satisfied", fatal[0].fix or "Run offbox doctor and follow its Fix lines.")
    manifest = discover_provider(provider, paths)
    try:
        credentials_env(manifest.manifest, paths)
    except PreconditionError as exc:
        fix = credential_fix(provider, paths)
        if fix is None or not exc.message.startswith("Missing"):
            raise
        raise PreconditionError(exc.message, fix) from exc
    if action == "create":
        if provider == "ssh":
            raise PreconditionError(
                "The SSH provider cannot create a cloud host", "Use --provider ssh --action init --target USER@ADDRESS."
            )
        ensure_include(paths, ssh_config)
        sync_config(paths, StateStore(paths.state).read())
        create_host(
            paths,
            runner,
            CreateOptions(name=options.name, provider=provider, ssh_key=ssh_key, size=options.size, region=options.region),
            ssh_config=ssh_config,
        )
    elif action == "init":
        if provider != "ssh":
            raise PreconditionError("Existing host initialization requires the SSH provider", "Use --provider ssh --action init.")
        target = options.target or (ask("Existing host USER@ADDRESS (--target)", "") if ask else None)
        if not target:
            raise PreconditionError(
                "Setup needs an SSH target",
                "Pass --provider ssh --action init --target USER@ADDRESS --host-key-fingerprint SHA256:...",
            )
        ensure_include(paths, ssh_config)
        sync_config(paths, StateStore(paths.state).read())
        init_host(
            paths,
            runner,
            InitOptions(
                target=target,
                name=options.name,
                port=options.port,
                ssh_key=ssh_key,
                accept_host_key=options.accept_host_key,
                host_key_fingerprint=options.host_key_fingerprint,
            ),
            confirm=confirm,
            ssh_config=ssh_config,
        )
    console.info("Next: cd <your repo> && offbox up")
