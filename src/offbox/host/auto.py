import logging
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from offbox import console
from offbox.config import Model, UserConfig, duration_seconds, load_config
from offbox.errors import OffboxError, PreconditionError
from offbox.host import host_operation
from offbox.host.activity import forget, last_activity, worktree_gone
from offbox.host.destroy import destroy_host_locked
from offbox.host.ready import Clock, SystemClock, tcp_probe
from offbox.host.usage import WallClock, describe, exclusive_usage, holders, utc_now
from offbox.paths import Paths
from offbox.providers import validate_name
from offbox.runner import Runner
from offbox.ssh import write_private
from offbox.state import HostState, State, StateStore

logger = logging.getLogger(__name__)
CREATE_WAIT_SECONDS = 900
RELEASE_WAIT_SECONDS = 60
CREATING_GRACE = timedelta(minutes=30)
REAP_LOCK_WAIT_SECONDS = 5
Action = Literal["kept", "destroy", "destroyed", "skipped", "failed"]


class ReapOutcome(Model):
    name: str
    action: Action
    reason: str
    lost_environments: list[str] = Field(default_factory=list[str])
    error: str | None = None


class ReapReport(Model):
    dry_run: bool = False
    hosts: list[ReapOutcome] = Field(default_factory=list[ReapOutcome])

    @property
    def exit_code(self) -> int:
        return 1 if any(outcome.action == "failed" for outcome in self.hosts) else 0


class ReapRecord(Model):
    finished_at: datetime
    source: Literal["timer", "manual"]
    report: ReapReport


class ReapHistory(Model):
    timer: ReapRecord | None = None
    manual: ReapRecord | None = None


class Decision(Model):
    destroy: bool
    reason: str


def _record_path(paths: Paths) -> Path:
    return paths.state / "reap.json"


def read_history(paths: Paths) -> ReapHistory:
    try:
        return ReapHistory.model_validate_json(_record_path(paths).read_text())
    except FileNotFoundError:
        return ReapHistory()
    except (OSError, ValidationError):
        logger.warning("ignoring unreadable reap record", exc_info=True)
        return ReapHistory()


def timer_status(paths: Paths) -> tuple[datetime | None, str | None]:
    """When the timer last reaped and the first failure it reported, for health checks."""
    record = read_history(paths).timer
    if record is None:
        return None, None
    failed = next((outcome for outcome in record.report.hosts if outcome.action == "failed"), None)
    return record.finished_at, None if failed is None else f"{failed.name}: {failed.error}"


def lost_environment_hint(paths: Paths, env: str) -> str | None:
    """How a missing environment record was lost, when the last reap removed it."""
    history = read_history(paths)
    records = sorted(
        (record for record in (history.timer, history.manual) if record is not None), key=lambda item: item.finished_at, reverse=True
    )
    for record in records:
        for outcome in record.report.hosts:
            if outcome.action == "destroyed" and env in outcome.lost_environments:
                stamp = record.finished_at.astimezone().strftime("%Y-%m-%d %H:%M")
                return f"host {outcome.name} was reaped at {stamp} ({outcome.reason}); its Docker data is gone"
    return None


def _format(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def host_activity(paths: Paths, state: State, host: HostState) -> datetime:
    return max([host.created_at, *(last_activity(paths, env) for env in state.envs.values() if env.host == host.name)])


def reap_decision(host: HostState, activity: datetime, now: datetime, *, operation_free: bool, empty: bool = False) -> Decision:
    """Whether an idle, empty or abandoned auto-managed host should be destroyed now; running commands are guarded by the usage lock."""
    if not host.auto:
        return Decision(destroy=False, reason="manual host")
    if host.phase == "ready" and empty:
        return Decision(destroy=True, reason="no environments use it")
    if host.phase != "ready":
        if not operation_free:
            return Decision(destroy=False, reason="creation in progress")
        if now - host.created_at > CREATING_GRACE:
            return Decision(destroy=True, reason="creation did not finish")
        return Decision(destroy=False, reason="creation started recently")
    if host.idle_seconds is None:
        return Decision(destroy=False, reason="idle reaping is off")
    idle = (now - activity).total_seconds()
    if idle > host.idle_seconds:
        return Decision(destroy=True, reason=f"idle for {_format(idle)}")
    return Decision(destroy=False, reason=f"last used {_format(max(idle, 0))} ago; reaped after {_format(host.idle_seconds)} idle")


def _destroy(paths: Paths, runner: Runner, host: HostState, *, executable: Path | None) -> list[str]:
    state = StateStore(paths.state).read()
    lost = sorted(name for name, env in state.envs.items() if env.host == host.name)
    destroy_host_locked(paths, runner, host.name, executable=executable)
    for name in lost:
        forget(paths, name)
    return lost


def ensure_host(
    paths: Paths,
    runner: Runner,
    name: str,
    *,
    clock: Clock | None = None,
    executable: Path | None = None,
    probe: Callable[[str, int, float], bool] = tcp_probe,
    ssh_config: Path | None = None,
    timer_installed: Callable[[Paths], bool] | None = None,
) -> HostState:
    """Return the ready host NAME, creating it when its user preset is auto-managed; parallel callers create it once."""
    validate_name(name)
    host = StateStore(paths.state).read().hosts.get(name)
    if host is not None and host.phase == "ready":
        return host
    preset = load_config(paths.user_config, UserConfig).hosts.get(name)
    if host is None and (preset is None or not preset.auto):
        raise PreconditionError(f"Unknown host: {name}", "Run offbox host list or create this host.")
    with host_operation(paths, name, wait_timeout=CREATE_WAIT_SECONDS, clock=clock or SystemClock()):
        host = StateStore(paths.state).read().hosts.get(name)
        if host is not None and host.phase == "ready":
            return host
        if host is not None:
            raise PreconditionError(f"Host {name} did not finish creating", f"Run offbox host destroy {name}, then retry.")
        if preset is None or not preset.auto:
            raise PreconditionError(f"Unknown host: {name}", "Run offbox host list or create this host.")
        idle = duration_seconds(preset.idle)
        limit = (
            f"destroyed after {_format(idle)} idle or when its last environment is removed"
            if idle
            else "destroyed when its last environment is removed"
        )
        console.warn(f"Creating auto-managed host {name}: billable until destroyed; {limit}")
        if idle and timer_installed is not None:
            if not timer_installed(paths):
                console.info("Idle reaping needs the timer: offbox host reap --install-timer")
            else:
                from offbox.host.timer import check_installed_credentials

                problem = check_installed_credentials(paths)
                if problem is not None:
                    console.warn(f"The idle reaper cannot destroy {name}: {problem}")
        from offbox.host.create import CreateOptions, create_host_locked

        return create_host_locked(
            paths,
            runner,
            CreateOptions(name=name, auto=True, idle_seconds=idle),
            executable=executable,
            clock=clock,
            probe=probe,
            ssh_config=ssh_config,
        )


def release_if_unused(
    paths: Paths,
    runner: Runner,
    name: str,
    *,
    clock: Clock | None = None,
    executable: Path | None = None,
    timer_installed: Callable[[Paths], bool] | None = None,
) -> bool:
    """Destroy an auto-managed host whose last environment is gone; called after rm released the host lock."""
    store = StateStore(paths.state)
    host = store.read().hosts.get(name)
    if host is None or not host.auto:
        return False
    if any(env.host == name and not worktree_gone(env.worktree_path) for env in store.read().envs.values()):
        return False
    timer = clock or SystemClock()
    with exclusive_usage(paths, name, wait=RELEASE_WAIT_SECONDS, clock=timer) as free:
        if not free:
            fix = f"run offbox host reap {name} once they finish"
            if timer_installed is not None and not timer_installed(paths):
                fix += ", or install the idle reaper with offbox host reap --install-timer"
            console.warn(f"Host {name} is still in use by: {describe(holders(paths, name))}. Fix: {fix}.")
            return False
        with host_operation(paths, name, wait_timeout=RELEASE_WAIT_SECONDS, clock=timer):
            state = store.read()
            host = state.hosts.get(name)
            if host is None or not host.auto:
                return False
            if any(env.host == name and not worktree_gone(env.worktree_path) for env in state.envs.values()):
                return False
            console.step(f"Destroying auto-managed host {name}: its last environment was removed")
            lost = _destroy(paths, runner, host, executable=executable)
            previous = read_history(paths).manual
            earlier = [outcome for outcome in previous.report.hosts if outcome.name != name] if previous is not None else []
            outcome = ReapOutcome(name=name, action="destroyed", reason="last environment removed", lost_environments=lost)
            _record(paths, ReapReport(hosts=[*earlier, outcome]), "manual", utc_now())
            return True


def reap(
    paths: Paths,
    runner: Runner,
    name: str | None = None,
    *,
    dry_run: bool = False,
    source: Literal["timer", "manual"] = "manual",
    clock: Clock | None = None,
    now: WallClock = utc_now,
    executable: Path | None = None,
) -> ReapReport:
    store = StateStore(paths.state)
    state = store.read()
    if name is not None:
        validate_name(name)
        if name not in state.hosts:
            raise PreconditionError(f"Unknown host: {name}", "Run offbox host list.")
        names = [name]
    else:
        names = sorted(host.name for host in state.hosts.values() if host.auto)
    report = ReapReport(dry_run=dry_run)
    timer = clock or SystemClock()
    for current in names:
        try:
            report.hosts.append(_reap_one(paths, runner, current, dry_run=dry_run, clock=timer, now=now, executable=executable))
        except OffboxError as exc:
            logger.exception("reaping %s failed", current)
            report.hosts.append(ReapOutcome(name=current, action="failed", reason="destroy failed", error=f"{exc.message} Fix: {exc.fix}"))
    if not dry_run:
        _record(paths, report, source, now())
    return report


def _record(paths: Paths, report: ReapReport, source: Literal["timer", "manual"], finished_at: datetime) -> None:
    history = read_history(paths).model_copy(update={source: ReapRecord(finished_at=finished_at, source=source, report=report)})
    write_private(_record_path(paths), history.model_dump_json(indent=2) + "\n")


def clear_lost(paths: Paths, env: str) -> None:
    """Forget that ENV was lost to a reap once it is registered again or removed, so later hints stay true."""
    history = read_history(paths)
    changed = False
    for record in (history.timer, history.manual):
        if record is None:
            continue
        for outcome in record.report.hosts:
            if env in outcome.lost_environments:
                outcome.lost_environments.remove(env)
                changed = True
    if changed:
        write_private(_record_path(paths), history.model_dump_json(indent=2) + "\n")


def _reap_one(
    paths: Paths, runner: Runner, name: str, *, dry_run: bool, clock: Clock, now: WallClock, executable: Path | None
) -> ReapOutcome:
    store = StateStore(paths.state)
    host = store.read().hosts.get(name)
    if host is None:
        return ReapOutcome(name=name, action="skipped", reason="host no longer registered")
    if not host.auto:
        return ReapOutcome(name=name, action="skipped", reason="manual host")
    with ExitStack() as stack:
        if not stack.enter_context(exclusive_usage(paths, name, clock=clock)):
            return ReapOutcome(name=name, action="skipped", reason=f"in use by {describe(holders(paths, name))}")
        try:
            stack.enter_context(host_operation(paths, name, wait_timeout=REAP_LOCK_WAIT_SECONDS, clock=clock))
            operation_free = True
        except PreconditionError:
            operation_free = False
        state = store.read()
        host = state.hosts.get(name)
        if host is None:
            return ReapOutcome(name=name, action="skipped", reason="host no longer registered")
        if not operation_free and host.phase == "ready":
            return ReapOutcome(name=name, action="skipped", reason="another host operation is running")
        empty = not any(env.host == name and not worktree_gone(env.worktree_path) for env in state.envs.values())
        decision = reap_decision(host, host_activity(paths, state, host), now(), operation_free=operation_free, empty=empty)
        lost = sorted(env.name for env in state.envs.values() if env.host == name)
        if not decision.destroy:
            return ReapOutcome(name=name, action="kept", reason=decision.reason)
        if dry_run:
            return ReapOutcome(name=name, action="destroy", reason=decision.reason, lost_environments=lost)
        console.step(f"Reaping auto-managed host {name}: {decision.reason}")
        lost = _destroy(paths, runner, host, executable=executable)
        return ReapOutcome(name=name, action="destroyed", reason=decision.reason, lost_environments=lost)
    return ReapOutcome(name=name, action="skipped", reason="unreachable")
