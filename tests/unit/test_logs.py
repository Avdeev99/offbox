import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from offbox.errors import CommandFailed, ConnectionError_
from offbox.logging import LogEntry, bind_environment, close_logs, configure
from offbox.logs import environment_records, follow_lines, stack_logs
from offbox.naming import worktree_name
from offbox.paths import Paths
from offbox.runner import FakeRunner, Script
from offbox.state import EnvState, HostState, State, StateStore


def paths_at(root: Path) -> Paths:
    return Paths(root / "config", root / "state", root / "cache", root / "control")


def record(run: str, env: str | None, message: str) -> str:
    return (
        LogEntry(
            timestamp=datetime(2026, 10, 23, tzinfo=UTC), invocation=run, env=env, level="INFO", logger="offbox.test", message=message
        ).model_dump_json()
        + "\n"
    )


def test_log_filter_uses_invocation_and_env_metadata_and_preserves_redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = paths_at(tmp_path)
    day = date(2026, 10, 23)
    monkeypatch.setenv("API_TOKEN", "synthetic-log-token")
    configure(paths, ["offbox", "up"], day)
    bind_environment("one-abcd")
    logging.getLogger("offbox.test").error("multiline\nAPI_TOKEN=synthetic-log-token")
    configure(paths, ["offbox", "up", "mentions-one-abcd"], day)
    bind_environment("two-abcd")
    logging.getLogger("offbox.test").info("one-abcd is only text, not ownership")
    close_logs()
    selected = environment_records(paths.log_file(day), "one-abcd")
    entries = [LogEntry.model_validate_json(line) for _index, line in selected]
    assert len(entries) == 3
    assert entries[0].env is None
    assert entries[-1].message == "multiline\nAPI_TOKEN=[REDACTED]"
    assert all(entry.env in (None, "one-abcd") for entry in entries)
    assert len({entry.invocation for entry in entries}) == 1
    assert "synthetic-log-token" not in paths.log_file(day).read_text()


def test_follow_adds_late_bound_invocations_without_duplicate_lines_and_rolls_date(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    paths.logs.mkdir(parents=True)
    days = [date(2026, 10, 23), date(2026, 10, 24)]
    selected = [days[0]]
    first, unbound = record("a", "one-abcd", "first"), record("b", None, "early invocation")
    paths.log_file(days[0]).write_text(first + unbound)

    class Clock:
        steps = 0

        def now(self) -> float:
            return float(self.steps)

        def sleep(self, seconds: float) -> None:
            self.steps += 1
            if self.steps == 1:
                with paths.log_file(days[0]).open("a") as file:
                    file.write(record("b", "one-abcd", "bound later"))
            elif self.steps == 2:
                selected[0] = days[1]
                paths.log_file(days[1]).write_text(record("c", "one-abcd", "next day"))
            else:
                raise AssertionError("Unexpected extra polling")

    lines = follow_lines(paths, "one-abcd", follow=True, clock=Clock(), today=lambda: selected[0])
    assert next(lines) == first
    assert next(lines) == unbound
    assert LogEntry.model_validate_json(next(lines)).message == "bound later"
    assert LogEntry.model_validate_json(next(lines)).message == "next day"
    lines.close()


def test_missing_log_is_empty_success(tmp_path: Path) -> None:
    assert list(follow_lines(paths_at(tmp_path), "one-abcd")) == []


@pytest.mark.parametrize("status", [0, 7, 255])
def test_compose_logs_streams_service_without_sync_install_or_ports(tmp_path: Path, status: int) -> None:
    paths = paths_at(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    name = worktree_name(root)
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime.now(UTC),
        known_hosts_path=tmp_path / "known_hosts",
    )
    env = EnvState(name=name, host=host.name, worktree_path=root, created_at=datetime.now(UTC))
    StateStore(paths.state).write(State(hosts={host.name: host}, envs={name: env}))
    container = [
        {
            "Name": f"/offbox-{name}",
            "Image": "sha256:actual",
            "State": {"Running": True},
            "Config": {"Labels": {"offbox.env": name, "offbox.worktree": str(root)}},
        }
    ]
    runner = FakeRunner(
        [
            Script(prefix=["git"], stdout=str(root)),
            Script(prefix=["ssh"], stdout=json.dumps(container)),
            Script(prefix=["ssh", "-n"], method="stream", returncode=status),
        ]
    )
    if status:
        with pytest.raises(ConnectionError_ if status == 255 else CommandFailed):
            stack_logs(paths, runner, follow=True, service="web", cwd=root)
    else:
        assert stack_logs(paths, runner, follow=True, service="web", cwd=root) == 0
    assert runner.calls[-1].argv == [
        "ssh",
        "-n",
        "offbox-main",
        "--",
        f"docker exec -u dev -w /work offbox-{name} docker compose logs --follow web",
    ]
    assert not runner.calls[-1].stdin
    assert len(runner.calls) == 3
