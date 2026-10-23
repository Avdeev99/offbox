import csv
import io
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from offbox import console
from offbox.config import Model, SyncConfig
from offbox.errors import CommandFailed, PreconditionError
from offbox.naming import Worktree, validate_env_name
from offbox.providers import validate_name
from offbox.runner import Result, Runner
from offbox.state import HostState

logger = logging.getLogger(__name__)

DEFAULT_IGNORES = (
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "target",
    ".terraform",
    ".DS_Store",
    ".git",
)
SyncState = Literal[
    "disconnected",
    "halted-on-root-emptied",
    "halted-on-root-deletion",
    "halted-on-root-type-change",
    "connecting-alpha",
    "connecting-beta",
    "watching",
    "scanning",
    "waiting-for-rescan",
    "reconciling",
    "staging-alpha",
    "staging-beta",
    "transitioning",
    "saving",
    "unknown",
]


class MutagenModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, hide_input_in_errors=True)


class IgnoreConfig(MutagenModel):
    paths: list[str] = Field(default_factory=list[str])
    vcs: bool | None = None


class Permissions(MutagenModel):
    file_mode: str | None = Field(default=None, alias="defaultFileMode")
    directory_mode: str | None = Field(default=None, alias="defaultDirectoryMode")


class SyncProblem(MutagenModel):
    path: str
    error: str


class Conflict(MutagenModel):
    root: str


class Endpoint(MutagenModel):
    protocol: Literal["local", "ssh", "docker"]
    path: str
    host: str | None = None
    user: str | None = None
    port: int | None = None
    environment: dict[str, str] = Field(default_factory=dict[str, str])
    parameters: dict[str, str] = Field(default_factory=dict[str, str])
    connected: bool
    permissions: Permissions = Field(default_factory=Permissions)
    scan_problems: list[SyncProblem] = Field(default_factory=list[SyncProblem], alias="scanProblems")
    transition_problems: list[SyncProblem] = Field(default_factory=list[SyncProblem], alias="transitionProblems")
    excluded_scan_problems: int = Field(default=0, ge=0, alias="excludedScanProblems")
    excluded_transition_problems: int = Field(default=0, ge=0, alias="excludedTransitionProblems")

    @property
    def problem_count(self) -> int:
        return len(self.scan_problems) + len(self.transition_problems) + self.excluded_scan_problems + self.excluded_transition_problems


class SyncSession(MutagenModel):
    identifier: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    version: Literal[1]
    creation_time: datetime = Field(alias="creationTime")
    creating_version: str = Field(alias="creatingVersion")
    name: str = ""
    labels: dict[str, str] = Field(default_factory=dict[str, str])
    alpha: Endpoint
    beta: Endpoint
    paused: bool
    status: SyncState
    mode: str | None = None
    ignore: IgnoreConfig = Field(default_factory=IgnoreConfig)
    conflicts: list[Conflict] = Field(default_factory=list[Conflict])
    excluded_conflicts: int = Field(default=0, ge=0, alias="excludedConflicts")
    last_error: str | None = Field(default=None, alias="lastError")


class SyncHealth(Model):
    state: SyncState
    paused: bool
    conflicts: int
    problems: int
    last_error: str | None = None
    ok: bool
    fix: str | None = None


def health(session: SyncSession) -> SyncHealth:
    conflicts = len(session.conflicts) + session.excluded_conflicts
    problems = session.alpha.problem_count + session.beta.problem_count
    fix: str | None = None
    if session.paused:
        fix = "Run offbox up to resume source sync."
    elif conflicts:
        paths = ", ".join(conflict.root or "<root>" for conflict in session.conflicts) or "excluded paths"
        fix = f"Resolve sync conflicts locally ({paths}), then run mutagen sync reset {session.identifier} and offbox up."
    elif session.status.startswith("halted-on-root-"):
        fix = f"Restore the worktree root, then run mutagen sync reset {session.identifier} and offbox up."
    elif session.status == "unknown":
        fix = "Check the Mutagen daemon and install a supported Mutagen 0.18.x version, then run offbox up."
    elif problems:
        details = [
            problem for endpoint in (session.alpha, session.beta) for problem in (*endpoint.scan_problems, *endpoint.transition_problems)
        ]
        paths = ", ".join(problem.path or "<root>" for problem in details) or "excluded paths"
        fix = f"Correct sync access problems ({paths}) and run offbox up."
    elif session.last_error or not session.alpha.connected or not session.beta.connected:
        fix = "Check host connectivity with offbox host ssh, then run offbox up."
    return SyncHealth(
        state=session.status,
        paused=session.paused,
        conflicts=conflicts,
        problems=problems,
        last_error=session.last_error,
        ok=fix is None,
        fix=fix,
    )


def ignores(config: SyncConfig) -> list[str]:
    if any(not pattern or any(character in pattern for character in "\x00\r\n") for pattern in config.ignore):
        raise PreconditionError("Invalid sync ignore pattern", "Set sync.ignore to nonempty, single-line patterns without NUL characters.")
    return [*(pattern for pattern in (*DEFAULT_IGNORES, *config.ignore) if pattern != ".git"), ".git"]


def _csv_pattern(pattern: str) -> str:
    output = io.StringIO()
    csv.writer(output, lineterminator="\n").writerow([pattern])
    return output.getvalue()[:-1]


def create_command(work: Worktree, host: HostState, config: SyncConfig) -> list[str]:
    argv = [
        "mutagen",
        "sync",
        "create",
        "--no-global-configuration",
        "--name",
        f"offbox-{work.name}",
        "--label",
        f"offbox-env={work.name}",
        "--mode",
        "two-way-safe",
        "--ignore-vcs",
        "--default-file-mode-beta=0644",
        "--default-directory-mode-beta=0755",
    ]
    for pattern in ignores(config):
        argv.extend(["--ignore", _csv_pattern(pattern)])
    return [*argv, str(work.root), f"offbox-{host.name}:{work.work}"]


class SyncManager:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def _run(self, argv: list[str], *, timeout: float = 30) -> Result:
        try:
            result = self.runner.run(argv, check=False, timeout=timeout)
        except PreconditionError as exc:
            if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                raise PreconditionError("Source sync timed out", "Check sync conflicts and host connectivity, then run offbox up.") from exc
            raise
        if result.returncode:
            failure = CommandFailed(argv, result.returncode, result.stderr)
            failure.fix = "Check the Mutagen daemon and SSH connectivity, then run offbox up."
            raise failure
        return result

    def status(self, work: Worktree, host: HostState | None = None) -> SyncSession | None:
        result = self._run(["mutagen", "sync", "list", "--label-selector", f"offbox-env={work.name}", "--template", "{{ json . }}"])
        try:
            sessions = TypeAdapter(list[SyncSession]).validate_json(result.stdout)
        except ValidationError as exc:
            raise PreconditionError("Invalid Mutagen session response", "Install Mutagen 0.18.x and inspect the sync daemon.") from exc
        if not sessions:
            return None
        if len(sessions) != 1:
            raise PreconditionError(
                f"Several sync sessions claim {work.name}", "Inspect mutagen sync list and remove duplicate managed sessions."
            )
        session = sessions[0]
        if (
            session.name != f"offbox-{work.name}"
            or session.labels.get("offbox-env") != work.name
            or session.alpha.user
            or session.alpha.host
            or session.alpha.port
            or session.alpha.environment
            or session.alpha.parameters
            or session.beta.user
            or session.beta.port
            or session.beta.environment
            or session.beta.parameters
            or session.alpha.protocol != "local"
            or Path(session.alpha.path) != work.root
            or session.beta.protocol != "ssh"
            or session.beta.path != work.work
            or (host is not None and session.beta.host != f"offbox-{host.name}")
        ):
            raise PreconditionError(
                "Sync session has conflicting ownership or endpoints", "Inspect mutagen sync list before changing this environment."
            )
        return session

    def ensure(self, work: Worktree, host: HostState, config: SyncConfig) -> None:
        session = self.status(work, host)
        if session is None:
            self._run(create_command(work, host, config))
            return
        if (
            session.mode != "two-way-safe"
            or session.ignore.vcs is not True
            or session.ignore.paths != ignores(config)
            or session.beta.permissions.file_mode != "0644"
            or session.beta.permissions.directory_mode != "0755"
        ):
            raise PreconditionError(
                "Sync configuration changed",
                f"Run mutagen sync terminate {session.identifier}, then offbox up to apply the new configuration.",
            )
        if session.paused:
            self._run(["mutagen", "sync", "resume", session.identifier])

    def flush(self, work: Worktree, host: HostState | None = None) -> None:
        session = self.status(work, host)
        if session is None:
            raise PreconditionError("No source sync session", "Run offbox up in this worktree.")
        if session.paused:
            self._run(["mutagen", "sync", "resume", session.identifier])
        elif (
            session.conflicts
            or session.excluded_conflicts
            or session.status.startswith("halted-on-root-")
            or session.alpha.problem_count
            or session.beta.problem_count
        ):
            self._raise_health(session)
        self._run(["mutagen", "sync", "flush", session.identifier], timeout=120)
        current = self.status(work, host)
        if current is None:
            raise PreconditionError("Source sync session disappeared during flush", "Run offbox up again.")
        if current.identifier != session.identifier:
            raise PreconditionError("Source sync session changed during flush", "Wait for other sync operations and retry offbox up.")
        self._raise_health(current)

    def _raise_health(self, session: SyncSession) -> None:
        report = health(session)
        if not report.ok:
            detail = session.last_error or f"{report.conflicts} conflicts, {report.problems} problems"
            raise PreconditionError(f"Source sync is {session.status}: {detail}", report.fix)

    def _cleanup(self, action: Literal["pause", "terminate"], work: Worktree, host: HostState | None) -> None:
        try:
            session = self.status(work, host)
            if session is not None and (action == "terminate" or not session.paused):
                self._run(["mutagen", "sync", action, session.identifier])
        except PreconditionError as exc:
            if isinstance(exc.__cause__, FileNotFoundError) and shutil.which("mutagen") is None:
                console.warn(f"Mutagen is not installed; continuing cleanup without sync {action}.")
                return
            raise

    def pause(self, work: Worktree, host: HostState | None = None) -> None:
        self._cleanup("pause", work, host)

    def terminate(self, work: Worktree, host: HostState | None = None) -> None:
        self._cleanup("terminate", work, host)

    def terminate_all(self) -> None:
        try:
            result = self._run(["mutagen", "sync", "list", "--label-selector", "offbox-env", "--template", "{{ json . }}"])
        except PreconditionError as exc:
            if isinstance(exc.__cause__, FileNotFoundError) and shutil.which("mutagen") is None:
                console.warn("Mutagen is not installed; continuing local cleanup without terminating sync sessions.")
                return
            raise
        try:
            sessions = TypeAdapter(list[SyncSession]).validate_json(result.stdout)
        except ValidationError as exc:
            raise PreconditionError("Invalid Mutagen session response", "Check mutagen sync list and retry uninstall.") from exc
        owned: list[SyncSession] = []
        for session in sessions:
            name = session.labels.get("offbox-env")
            if name is None or session.name != f"offbox-{name}":
                continue
            validate_env_name(name)
            if session.beta.host is not None and session.beta.host.startswith("offbox-"):
                validate_name(session.beta.host[len("offbox-") :])
            if (
                session.alpha.protocol != "local"
                or not Path(session.alpha.path).is_absolute()
                or session.beta.protocol != "ssh"
                or not (session.beta.host or "").startswith("offbox-")
                or session.beta.path != f"/srv/offbox/envs/{name}/work"
                or session.alpha.user
                or session.alpha.host
                or session.alpha.port
                or session.alpha.environment
                or session.alpha.parameters
                or session.beta.user
                or session.beta.port
                or session.beta.environment
                or session.beta.parameters
            ):
                raise PreconditionError(
                    f"Cannot verify ownership of sync session {session.identifier}",
                    f"Inspect mutagen sync list; terminate {session.identifier} explicitly only if it belongs to offbox.",
                )
            owned.append(session)
        for session in owned:
            self._run(["mutagen", "sync", "terminate", session.identifier])
