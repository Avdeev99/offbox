import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.config import SyncConfig
from offbox.errors import CommandFailed, PreconditionError
from offbox.naming import Worktree
from offbox.runner import Result
from offbox.state import HostState
from offbox.sync import DEFAULT_IGNORES, Endpoint, IgnoreConfig, Permissions, SyncManager, SyncSession, create_command, health
from offbox.tools import MutagenMissing, mutagen_directory


def work_and_host() -> tuple[Worktree, HostState]:
    return Worktree(root=Path("/src/app"), name="app-abcd"), HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
        known_hosts_path=Path("/state/known_hosts"),
    )


def session() -> SyncSession:
    work, _host = work_and_host()
    return SyncSession(
        identifier="sync_owned",
        version=1,
        creationTime=datetime(2026, 10, 23, tzinfo=UTC),
        creatingVersion="0.18.1",
        name="offbox-app-abcd",
        labels={"offbox-env": "app-abcd"},
        alpha=Endpoint(protocol="local", path=str(work.root), connected=True),
        beta=Endpoint(
            protocol="ssh",
            host="offbox-main",
            path=work.work,
            connected=True,
            permissions=Permissions(defaultFileMode="0644", defaultDirectoryMode="0755"),
        ),
        paused=False,
        status="watching",
        mode="two-way-safe",
        ignore=IgnoreConfig(paths=list(DEFAULT_IGNORES), vcs=True),
    )


def payload(value: SyncSession) -> str:
    return "[" + value.model_dump_json(by_alias=True) + "]"


def listing(value: SyncSession | None = None) -> Script:
    return Script(prefix=["mutagen", "sync", "list"], stdout="[]" if value is None else payload(value))


def test_exact_create_command() -> None:
    work, host = work_and_host()
    expected = [
        "mutagen",
        "sync",
        "create",
        "--no-global-configuration",
        "--name",
        "offbox-app-abcd",
        "--label",
        "offbox-env=app-abcd",
        "--mode",
        "two-way-safe",
        "--ignore-vcs",
        "--default-file-mode-beta=0644",
        "--default-directory-mode-beta=0755",
    ]
    for pattern in (*(pattern for pattern in DEFAULT_IGNORES if pattern != ".git"), "data/", "*.log", "node_modules", ".git"):
        expected.extend(["--ignore", pattern])
    expected.extend(["/src/app", "offbox-main:/srv/offbox/envs/app-abcd/work"])
    assert create_command(work, host, SyncConfig(ignore=["data/", "*.log", "node_modules"])) == expected
    runner = FakeRunner([listing(), Script(prefix=["mutagen", "sync", "create"])])
    SyncManager(runner).ensure(work, host, SyncConfig(ignore=["data/", "*.log", "node_modules"]))
    assert runner.calls[0].argv == ["mutagen", "sync", "list", "--label-selector", "offbox-env=app-abcd", "--template", "{{ json . }}"]
    assert runner.calls[1].argv == expected


def test_warm_flush_uses_label_flush_then_one_health_listing() -> None:
    work, host = work_and_host()
    runner = FakeRunner([Script(prefix=["mutagen", "sync", "flush"]), listing(session())])
    SyncManager(runner).flush(work, host)
    assert [call.argv for call in runner.calls] == [
        ["mutagen", "sync", "flush", "--label-selector", "offbox-env=app-abcd"],
        ["mutagen", "sync", "list", "--label-selector", "offbox-env=app-abcd", "--template", "{{ json . }}"],
    ]
    assert runner.calls[0].timeout == 120


def test_paused_session_resumed_before_flush_by_identifier() -> None:
    work, host = work_and_host()
    paused = session().model_copy(update={"paused": True, "status": "disconnected"})
    runner = FakeRunner(
        [
            Script(prefix=["mutagen", "sync", "flush", "--label-selector"], returncode=1, stderr="session is paused"),
            listing(paused),
            Script(prefix=["mutagen", "sync", "resume"]),
            Script(prefix=["mutagen", "sync", "flush"]),
            listing(session()),
        ]
    )
    SyncManager(runner).flush(work, host)
    assert [call.argv[2:] for call in runner.calls] == [
        ["flush", "--label-selector", "offbox-env=app-abcd"],
        ["list", "--label-selector", "offbox-env=app-abcd", "--template", "{{ json . }}"],
        ["resume", "sync_owned"],
        ["flush", "sync_owned"],
        ["list", "--label-selector", "offbox-env=app-abcd", "--template", "{{ json . }}"],
    ]
    assert runner.calls[3].timeout == 120


def test_failed_label_flush_reports_conflicts_without_flushing_again() -> None:
    work, host = work_and_host()
    value = session()
    value.excluded_conflicts = 2
    runner = FakeRunner([Script(prefix=["mutagen", "sync", "flush"], returncode=1, stderr="halted"), listing(value)])
    with pytest.raises(PreconditionError, match="2 conflicts") as caught:
        SyncManager(runner).flush(work, host)
    assert caught.value.fix is not None and caught.value.fix.startswith("Resolve sync conflicts locally")
    assert len(runner.calls) == 2


def test_failed_label_flush_without_session_has_fix() -> None:
    work, host = work_and_host()
    runner = FakeRunner([Script(prefix=["mutagen", "sync", "flush"], returncode=1, stderr="no sessions"), listing()])
    with pytest.raises(PreconditionError, match="No source sync session") as caught:
        SyncManager(runner).flush(work, host)
    assert caught.value.fix == "Run offbox up in this worktree."


@pytest.mark.parametrize("action", ["pause", "terminate"])
def test_cleanup_uses_owned_identifier_and_is_absence_idempotent(action: str) -> None:
    work, host = work_and_host()
    runner = FakeRunner([listing(session()), Script(prefix=["mutagen", "sync", action]), listing()])
    manager = SyncManager(runner)
    callback = manager.pause if action == "pause" else manager.terminate
    callback(work, host)
    callback(work, host)
    assert runner.calls[1].argv == ["mutagen", "sync", action, "sync_owned"]
    assert len(runner.calls) == 3


def test_nonzero_daemon_error_is_never_absence() -> None:
    work, host = work_and_host()
    runner = FakeRunner([Script(prefix=["mutagen"], returncode=1, stderr="daemon not found: permission denied")])
    with pytest.raises(CommandFailed, match="daemon not found") as caught:
        SyncManager(runner).terminate(work, host)
    assert caught.value.fix == "Check the Mutagen daemon and SSH connectivity, then run offbox up."


@pytest.mark.parametrize("body", ['{"sessions":[]}', '[{"name":"legacy"}]', "not json"])
def test_invalid_public_envelope_rejected(body: str) -> None:
    work, _host = work_and_host()
    with pytest.raises(PreconditionError, match="Invalid Mutagen"):
        SyncManager(FakeRunner([Script(prefix=["mutagen"], stdout=body)])).status(work)


def test_duplicate_sessions_rejected() -> None:
    work, _host = work_and_host()
    body = json.dumps([session().model_dump(mode="json", by_alias=True)] * 2)
    with pytest.raises(PreconditionError, match="Several sync sessions"):
        SyncManager(FakeRunner([Script(prefix=["mutagen"], stdout=body)])).status(work)


@pytest.mark.parametrize("side", ["local", "remote", "host", "name", "label", "user", "port", "environment", "parameters"])
def test_conflicting_session_binding_never_mutates(side: str) -> None:
    work, host = work_and_host()
    value = session()
    if side == "local":
        value.alpha.path = "/foreign"
    elif side == "remote":
        value.beta.path = "/foreign"
    elif side == "host":
        value.beta.host = "offbox-foreign"
    elif side == "name":
        value.name = "foreign"
    elif side == "label":
        value.labels = {"offbox-env": "foreign"}
    elif side == "user":
        value.beta.user = "root"
    elif side == "port":
        value.beta.port = 2222
    elif side == "environment":
        value.beta.environment = {"SSH_AUTH_SOCK": "/foreign"}
    else:
        value.beta.parameters = {"sshCommand": "foreign"}
    runner = FakeRunner([listing(value)])
    with pytest.raises(PreconditionError, match="conflicting ownership"):
        SyncManager(runner).ensure(work, host, SyncConfig())
    assert len(runner.calls) == 1


def test_config_change_requires_explicit_session_recreation() -> None:
    work, host = work_and_host()
    runner = FakeRunner([listing(session())])
    with pytest.raises(PreconditionError, match="configuration changed") as caught:
        SyncManager(runner).ensure(work, host, SyncConfig(ignore=["new/"]))
    assert caught.value.fix == "Run mutagen sync terminate sync_owned, then offbox up to apply the new configuration."
    assert len(runner.calls) == 1


def test_source_derived_full_v0181_array_and_truncation_counts() -> None:
    work, _host = work_and_host()
    body = (Path(__file__).parents[1] / "fixtures/mutagen/v0.18.1-problems.json").read_text()
    result = SyncManager(FakeRunner([Script(prefix=["mutagen"], stdout=body)])).status(work)
    assert result is not None
    report = health(result)
    assert report.conflicts == 5
    assert report.problems == 7
    assert report.ok is False
    assert "config.txt" in (report.fix or "")
    assert result.ignore.vcs is True
    assert result.beta.permissions.file_mode == "0644"


@pytest.mark.parametrize("state", ["halted-on-root-emptied", "halted-on-root-deletion", "halted-on-root-type-change", "unknown"])
def test_halted_and_unknown_are_unhealthy(state: str) -> None:
    value = SyncSession.model_validate({**session().model_dump(by_alias=True), "status": state})
    result = health(value)
    assert result.ok is False
    assert result.fix and "offbox up" in result.fix


def test_disconnected_can_recover_through_flush() -> None:
    work, host = work_and_host()
    value = session()
    value.status = "disconnected"
    value.last_error = "synthetic connection failure"
    value.beta.connected = False
    runner = FakeRunner(
        [
            Script(prefix=["mutagen", "sync", "flush"], returncode=1, stderr="disconnected"),
            listing(value),
            Script(prefix=["mutagen", "sync", "flush"]),
            listing(session()),
        ]
    )
    SyncManager(runner).flush(work, host)
    assert runner.calls[2].argv == ["mutagen", "sync", "flush", "sync_owned"]


def test_post_flush_problem_prevents_success() -> None:
    work, host = work_and_host()
    value = session()
    value.beta.connected = False
    runner = FakeRunner([Script(prefix=["mutagen", "sync", "flush"]), listing(value)])
    with pytest.raises(PreconditionError, match="Source sync is watching"):
        SyncManager(runner).flush(work, host)


def test_flush_missing_session_has_fix() -> None:
    work, host = work_and_host()
    with pytest.raises(PreconditionError, match="No source sync session") as caught:
        SyncManager(FakeRunner([Script(prefix=["mutagen", "sync", "flush"]), listing()])).flush(work, host)
    assert caught.value.fix == "Run offbox up in this worktree."


def test_flush_timeout_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    work, host = work_and_host()
    runner = FakeRunner([listing(session())])
    original = runner.run

    def run(argv: list[str], *, check: bool = True, timeout: float | None = None) -> object:
        if argv[2] == "flush":
            try:
                raise subprocess.TimeoutExpired(argv, timeout or 0)
            except subprocess.TimeoutExpired as exc:
                raise PreconditionError("timeout") from exc
        return original(argv, check=check, timeout=timeout)

    monkeypatch.setattr(runner, "run", run)
    with pytest.raises(PreconditionError, match="Source sync timed out") as caught:
        SyncManager(runner).flush(work, host)
    assert caught.value.fix == "Check sync conflicts and host connectivity, then run offbox up."


@pytest.mark.parametrize(("pattern", "encoded"), [("a,b", '"a,b"'), ('a"b', '"a""b"'), ('"quoted"', '"""quoted"""')])
def test_custom_ignore_is_exactly_one_csv_field(pattern: str, encoded: str) -> None:
    work, host = work_and_host()
    argv = create_command(work, host, SyncConfig(ignore=[pattern]))
    assert argv[-6:] == ["--ignore", encoded, "--ignore", ".git", str(work.root), f"offbox-{host.name}:{work.work}"]
    assert "--no-global-configuration" in argv
    assert argv[argv.index(".git") - 1 : argv.index(".git") + 1] == ["--ignore", ".git"]


@pytest.mark.parametrize("pattern", ["", "line\nbreak", "carriage\rreturn", "nul\x00byte"])
def test_invalid_ignore_is_refused_before_session_creation(pattern: str) -> None:
    work, host = work_and_host()
    runner = FakeRunner([listing()])
    with pytest.raises(PreconditionError, match="Invalid sync ignore"):
        SyncManager(runner).ensure(work, host, SyncConfig(ignore=[pattern]))
    assert [call.argv[2] for call in runner.calls] == ["list"]


@pytest.mark.parametrize("patterns", [[".env", "!.env", ".env"], ["!node_modules", "node_modules"]])
def test_project_ignore_override_order_survives_repeated_patterns(patterns: list[str]) -> None:
    from offbox.sync import ignores

    work, host = work_and_host()
    config = SyncConfig(ignore=patterns)
    assert ignores(config)[-len(patterns) - 1 :] == [*patterns, ".git"]
    argv = create_command(work, host, config)
    emitted = [argv[index + 1] for index, value in enumerate(argv) if value == "--ignore"]
    assert emitted[-len(patterns) - 1 :] == [*patterns, ".git"]


def test_linked_git_file_ignore_cannot_be_overridden_by_project_negation_or_deduplication() -> None:
    from offbox.sync import ignores

    work, host = work_and_host()
    config = SyncConfig(ignore=[".git", "!.git", "cache,quoted/", ".git"])
    patterns = ignores(config)
    assert patterns[-1] == ".git"
    assert patterns.count(".git") == 1
    assert patterns[-3:] == ["!.git", "cache,quoted/", ".git"]
    command = create_command(work, host, config)
    assert command[-8:] == [
        "--ignore",
        "!.git",
        "--ignore",
        '"cache,quoted/"',
        "--ignore",
        ".git",
        str(work.root),
        f"offbox-{host.name}:{work.work}",
    ]


class MissingPathRunner(FakeRunner):
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
        interruptible: bool = False,
    ) -> Result:
        if argv[0] == "mutagen":
            try:
                raise FileNotFoundError(2, "No such file or directory", "mutagen")
            except FileNotFoundError as exc:
                raise PreconditionError("Cannot run mutagen", "Install mutagen and check its permissions.") from exc
        return super().run(argv, check=check, capture=capture, input=input, env=env, cwd=cwd, timeout=timeout, interruptible=interruptible)


def cached_install(cache: Path) -> Path:
    directory = mutagen_directory(cache)
    directory.mkdir(parents=True)
    (directory / "mutagen-agents.tar.gz").write_bytes(b"agents")
    executable = directory / "mutagen"
    executable.write_bytes(b"binary")
    return executable


def test_missing_path_mutagen_uses_verified_cache(tmp_path: Path) -> None:
    work, host = work_and_host()
    executable = str(cached_install(tmp_path))
    runner = MissingPathRunner(
        [Script(prefix=[executable, "sync", "flush"]), Script(prefix=[executable, "sync", "list"], stdout=payload(session()))]
    )
    SyncManager(runner, cache=tmp_path, offer=lambda _message: pytest.fail("a cached binary needs no download")).flush(work, host)
    assert [call.argv[0] for call in runner.calls] == [executable, executable]


def test_missing_mutagen_without_tty_fails_with_setup_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work, host = work_and_host()
    monkeypatch.setattr("offbox.sync.console.interactive", lambda: False)
    with pytest.raises(MutagenMissing) as caught:
        SyncManager(MissingPathRunner(), cache=tmp_path).flush(work, host)
    assert caught.value.fix == "Run offbox setup to download Mutagen, or install Mutagen 0.18.x."
    assert list(tmp_path.iterdir()) == []


def test_missing_mutagen_offers_verified_download_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work, host = work_and_host()
    prompts: list[str] = []
    installs: list[Path] = []

    def install(cache: Path) -> Path:
        installs.append(cache)
        return cached_install(cache)

    def offer(message: str) -> bool:
        prompts.append(message)
        return True

    monkeypatch.setattr("offbox.sync.install_mutagen", install)
    executable = str(mutagen_directory(tmp_path) / "mutagen")
    runner = MissingPathRunner(
        [Script(prefix=[executable, "sync", "flush"]), Script(prefix=[executable, "sync", "list"], stdout=payload(session()))]
    )
    SyncManager(runner, cache=tmp_path, offer=offer).flush(work, host)
    assert prompts == [f"Mutagen 0.18.1 not found. Download it (verified) into {mutagen_directory(tmp_path)}?"]
    assert installs == [tmp_path]


def test_cleanup_without_mutagen_never_offers_download(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work, host = work_and_host()
    SyncManager(MissingPathRunner(), cache=tmp_path, offer=lambda _message: pytest.fail("cleanup must not download")).terminate(work, host)
    assert "Mutagen is not installed; continuing cleanup without sync terminate." in capsys.readouterr().err
