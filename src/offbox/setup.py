from collections.abc import Callable
from pathlib import Path
from typing import Literal

from offbox import console
from offbox.config import Model
from offbox.doctor import Check, diagnose
from offbox.errors import OffboxError, PreconditionError
from offbox.host import select_host
from offbox.host.create import CreateOptions, create_host
from offbox.host.init import InitOptions, init_host
from offbox.paths import Paths
from offbox.providers import credentials_env, discover_provider
from offbox.runner import Runner
from offbox.ssh import ensure_include, ssh_argv, sync_config
from offbox.state import StateStore


class SetupOptions(Model):
    provider: Literal["hetzner", "ssh"] | None = None
    action: Literal["check", "create", "init"] | None = None
    name: str = "main"
    ssh_key: Path | None = None
    size: str | None = None
    region: str | None = None
    target: str | None = None
    port: int = 22
    accept_host_key: bool = False


def setup(
    paths: Paths,
    runner: Runner,
    options: SetupOptions,
    *,
    ask: Callable[[str, str], str] | None = None,
    confirm: Callable[[str], bool] | None = None,
    ssh_config: Path | None = None,
) -> None:
    report = diagnose(paths, runner, ssh_key=options.ssh_key)
    ensure_include(paths, ssh_config)
    sync_config(paths, StateStore(paths.state).read())
    provider = options.provider
    if provider is None:
        if ask is None:
            raise PreconditionError(
                "Setup needs a provider", "Run offbox setup --provider ssh --action check, or --provider hetzner --action create."
            )
        choice = ask("Provider (hetzner or ssh)", "hetzner")
        if choice not in ("hetzner", "ssh"):
            raise PreconditionError("Unknown setup provider", "Pass --provider hetzner or --provider ssh.")
        provider = "hetzner" if choice == "hetzner" else "ssh"
    action = options.action
    if action is None:
        if confirm is None:
            raise PreconditionError("Setup needs an explicit action", "Pass --action check, --action create or --action init.")
        action = (
            ("create" if provider == "hetzner" else "init")
            if confirm("Create a billable cloud host?" if provider == "hetzner" else "Bootstrap an existing SSH host?")
            else "check"
        )
    if action == "check" and any(check.name == "host" and not check.ok for check in report.checks):
        try:
            host = select_host(paths)
            result = runner.run(ssh_argv(host.name, ["true"], options=["-o", "ConnectTimeout=3"]), check=False, timeout=5)
            if result.returncode == 0:
                report.checks = [
                    Check(name="host", ok=True, message=f"Default host {host.name} is reachable") if check.name == "host" else check
                    for check in report.checks
                ]
        except OffboxError as exc:
            report.checks = [
                Check(name="host", ok=False, message=exc.message, fix=exc.fix or "Check the default host registration.")
                if check.name == "host"
                else check
                for check in report.checks
            ]
    fatal: list[Check] = []
    for check in report.checks:
        required = not (check.name == "host" and action != "check") and not (
            check.name == "tofu" and not (provider == "hetzner" and action == "create")
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
    credentials_env(manifest.manifest, paths)
    if action == "create":
        if provider != "hetzner":
            raise PreconditionError(
                "The SSH provider cannot create a cloud host", "Use --provider ssh --action init --target USER@ADDRESS."
            )
        create_host(
            paths,
            runner,
            CreateOptions(name=options.name, provider=provider, ssh_key=options.ssh_key, size=options.size, region=options.region),
            ssh_config=ssh_config,
        )
    elif action == "init":
        if provider != "ssh":
            raise PreconditionError("Existing host initialization requires the SSH provider", "Use --provider ssh --action init.")
        target = options.target or (ask("Existing host USER@ADDRESS (--target)", "") if ask else None)
        if not target:
            raise PreconditionError(
                "Setup needs an SSH target", "Pass --provider ssh --action init --target USER@ADDRESS --accept-host-key."
            )
        init_host(
            paths,
            runner,
            InitOptions(
                target=target, name=options.name, port=options.port, ssh_key=options.ssh_key, accept_host_key=options.accept_host_key
            ),
            confirm=confirm,
            ssh_config=ssh_config,
        )
    console.info("Next: cd <your repo> && offbox up")
