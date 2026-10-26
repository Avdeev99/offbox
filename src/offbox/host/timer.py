import os
import platform
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from html import escape as _escape
from html import unescape
from pathlib import Path

from offbox import console
from offbox.config import UserConfig, load_config
from offbox.errors import PreconditionError
from offbox.paths import Paths
from offbox.providers import credentials_env, discover_provider
from offbox.runner import Runner
from offbox.ssh import write_private

INTERVAL_SECONDS = 900


def escape(value: str) -> str:
    return _escape(value, quote=False)


STALE_AFTER = timedelta(minutes=60)
LAUNCHD_LABEL = "dev.offbox.reap"
SYSTEMD_UNIT = "offbox-reap"
_PASSED_THROUGH = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "GOOGLE_PROJECT")
_TOOLS = ("tofu", "terraform", "ssh", "git", "mutagen")


def unit_files(home: Path, system: str | None = None) -> list[Path]:
    if (system or platform.system()) == "Darwin":
        return [home / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"]
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "systemd/user"
    return [base / f"{SYSTEMD_UNIT}.service", base / f"{SYSTEMD_UNIT}.timer"]


def installed(paths: Paths, *, home: Path | None = None, system: str | None = None) -> bool:
    del paths
    return all(path.is_file() for path in unit_files(home or Path.home(), system))


def executable_path() -> Path:
    """The installed offbox command the timer runs; ephemeral uvx or temporary environments are refused."""
    found = shutil.which("offbox")
    candidate = Path(found).resolve() if found else Path(sys.argv[0]).resolve()
    text = str(candidate)
    temporary = Path(tempfile.gettempdir()).resolve()
    if (
        not candidate.is_file()
        or candidate.is_relative_to(temporary)
        or "/.cache/uv/" in text
        or "/uv/cache/" in text
        or text.startswith("/tmp/")
    ):
        raise PreconditionError(
            f"The idle reaper needs an installed offbox, not {candidate}", "Install it with uv tool install offbox, then retry."
        )
    return candidate


def unit_environment(paths: Paths, environ: Mapping[str, str], home: Path) -> dict[str, str]:
    """The environment a scheduled reap runs with: tool directories and settings locations, never credentials."""
    directories: list[str] = []
    for tool in _TOOLS:
        found = shutil.which(tool, path=environ.get("PATH"))
        if found:
            directories.append(str(Path(found).parent))
    from offbox.tools import cached_mutagen

    mutagen = cached_mutagen(paths.cache)
    if mutagen is not None:
        directories.append(str(mutagen.parent))
    directories.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    environment = {"HOME": str(home), "PATH": ":".join(dict.fromkeys(directories))}
    environment.update({name: environ[name] for name in _PASSED_THROUGH if environ.get(name)})
    return environment


def preflight(paths: Paths, environment: Mapping[str, str], home: Path) -> None:
    """Fail unless every auto-managed preset's credentials are readable without the current shell's environment."""
    for name, preset in sorted(load_config(paths.user_config, UserConfig).hosts.items()):
        if not preset.auto:
            continue
        provider = discover_provider(preset.provider, paths)
        try:
            credentials_env(provider.manifest, paths, environment)
        except PreconditionError as exc:
            fix = (
                f"Store the Hetzner token in {paths.credentials} (mode 0600) instead of only exporting HCLOUD_TOKEN."
                if provider.manifest.name == "hetzner"
                else f"Export them in the shell that runs --install-timer, which copies non-secret provider variables into the timer: {exc.fix}"
            )
            raise PreconditionError(f"The idle reaper cannot read credentials for host {name}: {exc.message}", fix) from exc
        if provider.manifest.name == "gcp" and not (home / ".config/gcloud/application_default_credentials.json").is_file():
            raise PreconditionError(
                f"The idle reaper cannot find Google application default credentials for host {name}",
                "Run gcloud auth application-default login, then retry.",
            )


def render_launchd(executable: Path, environment: Mapping[str, str], log: Path) -> str:
    variables = "".join(f"<key>{escape(key)}</key><string>{escape(value)}</string>" for key, value in sorted(environment.items()))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>'
        f"<key>Label</key><string>{LAUNCHD_LABEL}</string>"
        f"<key>ProgramArguments</key><array><string>{escape(str(executable))}</string><string>host</string>"
        "<string>reap</string><string>--from-timer</string></array>"
        f"<key>EnvironmentVariables</key><dict>{variables}</dict>"
        f"<key>StartInterval</key><integer>{INTERVAL_SECONDS}</integer>"
        "<key>RunAtLoad</key><true/>"
        f"<key>StandardOutPath</key><string>{escape(str(log))}</string>"
        f"<key>StandardErrorPath</key><string>{escape(str(log))}</string>"
        "</dict></plist>\n"
    )


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return '"' + escaped + '"'


def render_systemd(executable: Path, environment: Mapping[str, str]) -> tuple[str, str]:
    variables = "".join(f"Environment={_quote(f'{key}={value}')}\n" for key, value in sorted(environment.items()))
    service = (
        "[Unit]\nDescription=Destroy idle offbox hosts\n\n"
        f"[Service]\nType=oneshot\n{variables}ExecStart={_quote(str(executable))} host reap --from-timer\n"
    )
    timer = "[Unit]\nDescription=Run the offbox idle reaper every 15 minutes\n\n[Timer]\nOnCalendar=*:0/15\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    return service, timer


def install(
    paths: Paths, runner: Runner, *, home: Path | None = None, system: str | None = None, environ: Mapping[str, str] | None = None
) -> list[Path]:
    root = home or Path.home()
    current = system or platform.system()
    executable = executable_path()
    environment = unit_environment(paths, os.environ if environ is None else environ, root)
    preflight(paths, environment, root)
    files = unit_files(root, current)
    if current == "Darwin":
        write_private(files[0], render_launchd(executable, environment, paths.logs / "reaper.log"))
        domain = f"gui/{os.getuid()}"
        runner.run(["launchctl", "bootout", domain, str(files[0])], check=False, timeout=30)
        runner.run(["launchctl", "bootstrap", domain, str(files[0])], timeout=30)
    elif current == "Linux":
        service, timer = render_systemd(executable, environment)
        write_private(files[0], service)
        write_private(files[1], timer)
        runner.run(["systemctl", "--user", "daemon-reload"], timeout=30)
        runner.run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"], timeout=30)
    else:
        raise PreconditionError(f"The idle reaper timer is not supported on {current}", "Run offbox host reap from your own scheduler.")
    console.success(f"Idle reaper installed: offbox host reap runs every {INTERVAL_SECONDS // 60} minutes")
    return files


def remove(paths: Paths, runner: Runner, *, home: Path | None = None, system: str | None = None) -> bool:
    del paths
    root = home or Path.home()
    current = system or platform.system()
    files = unit_files(root, current)
    if not any(path.exists() for path in files):
        return False
    if current == "Darwin":
        runner.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(files[0])], check=False, timeout=30)
    elif current == "Linux":
        runner.run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"], check=False, timeout=30)
    for path in files:
        path.unlink(missing_ok=True)
    if current == "Linux":
        runner.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=30)
    return True


def health(paths: Paths, now: datetime, last_run: datetime | None, failure: str | None, *, home: Path | None = None) -> str | None:
    """A warning when the installed timer has stopped running or its last run failed."""
    files = unit_files(home or Path.home())
    if not all(path.is_file() for path in files):
        return None
    if failure:
        return f"The last scheduled reap failed: {failure}"
    reference = last_run
    if reference is None:
        try:
            reference = datetime.fromtimestamp(files[0].stat().st_mtime, now.tzinfo)
        except OSError:
            return None
    if now - reference > STALE_AFTER:
        return "The idle reaper has not run for over an hour (a sleeping laptop also delays it). Fix: offbox doctor, or reinstall with offbox host reap --install-timer."
    return None


def unit_problem(*, home: Path | None = None) -> str | None:
    """A problem with an installed timer's target executable, such as an uninstalled offbox."""
    files = unit_files(home or Path.home())
    if not all(path.is_file() for path in files):
        return None
    try:
        text = files[0].read_text()
    except OSError:
        return "The idle reaper unit cannot be read"
    found = re.search(r"<key>ProgramArguments</key><array><string>([^<]+)</string>", text) or re.search(
        r'^ExecStart="([^"]+)"', text, re.MULTILINE
    )
    if found is None:
        return "The idle reaper unit has no executable"
    executable = Path(unescape(found[1]).replace("%%", "%").replace("$$", "$"))
    if not executable.is_file():
        return f"The idle reaper runs {executable}, which no longer exists"
    return None


def check_installed_credentials(paths: Paths, *, home: Path | None = None) -> str | None:
    """Re-run the timer's credential preflight; None when every auto-managed preset can be destroyed by the timer."""
    root = home or Path.home()
    try:
        preflight(paths, unit_environment(paths, os.environ, root), root)
    except PreconditionError as exc:
        return f"{exc.message}. Fix: {exc.fix}"
    return None
