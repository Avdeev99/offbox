from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeRunner, Script

from offbox.config import InstallConfig, ProjectConfig
from offbox.errors import CommandFailed, OffboxError, PreconditionError
from offbox.execute import Executor, install_paths
from offbox.naming import Worktree
from offbox.paths import Paths
from offbox.state import HostState


def context(root: Path) -> tuple[Executor, Worktree, HostState]:
    work = Worktree(root=root, name="app-abcd")
    host = HostState(
        name="main",
        provider="ssh",
        address="192.0.2.1",
        ssh_user="offbox",
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
        known_hosts_path=root / "known_hosts",
    )
    executor = Executor(Paths(root / "config", root / "state", root / "cache", root / "control"), FakeRunner(), id_factory=lambda: "a" * 32)
    return executor, work, host


def test_changed_install_hash_runs_with_scoped_env_then_atomically_records_postinstall_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    executor, work, host = context(tmp_path)
    before, after = "a" * 64, "b" * 64
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], stdout=before + "  -\nmissing\n"),
            Script(prefix=["ssh"], method="start_stream"),
            Script(prefix=["ssh"], stdout=after + "  -\n"),
            Script(prefix=["ssh"]),
        ]
    )
    executor.runner = runner
    executor.install(
        work, host, InstallConfig(command="npm ci", when=["package-lock.json", "nested/lock file"]), env={"NPM_TOKEN": "private"}
    )
    assert len(runner.calls) == 4
    assert runner.calls[0].argv[-1].endswith("package-lock.json 'nested/lock file'")
    assert "-e NPM_TOKEN=private" in runner.calls[1].argv[-1]
    assert runner.calls[3].argv[-1].endswith(after)
    assert runner.calls[3].input is not None and 'mv -f -- "$tmp" "$path/install.sha256"' in runner.calls[3].input
    output = capsys.readouterr()
    assert output.out == ""
    assert "lockfile changed, running install" in output.err


def test_matching_hash_does_not_install_or_rewrite_marker(tmp_path: Path) -> None:
    executor, work, host = context(tmp_path)
    runner = FakeRunner([Script(prefix=["ssh"], stdout="a" * 64 + "  -\n" + "a" * 64 + "\n")])
    executor.runner = runner
    executor.install(work, host, InstallConfig(command="false", when=["lock"]))
    assert [call.method for call in runner.calls] == ["run"]


@pytest.mark.parametrize(
    "response",
    [
        "",
        "a" * 64 + "  -",
        "not-a-hash\nmissing",
        "A" * 64 + "  -\nmissing",
        "a" * 64 + "  -\ninvalid-marker",
        "a" * 64 + "  -\nmissing\nunexpected",
        "a" * 64 + "  -\n" + "b" * 63,
        "a" * 64 + "  -\n " + "b" * 64,
    ],
)
def test_invalid_combined_probe_stops_before_install_or_marker_write(tmp_path: Path, response: str) -> None:
    executor, work, host = context(tmp_path)
    runner = FakeRunner([Script(prefix=["ssh"], stdout=response)])
    executor.runner = runner
    with pytest.raises(PreconditionError):
        executor.install(work, host, InstallConfig(command="npm ci"))
    assert len(runner.calls) == 1
    assert runner.calls[0].method == "run"


def test_changed_existing_marker_triggers_install(tmp_path: Path) -> None:
    executor, work, host = context(tmp_path)
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], stdout="a" * 64 + "  -\n" + "b" * 64),
            Script(prefix=["ssh"], method="start_stream"),
            Script(prefix=["ssh"], stdout="a" * 64 + "  -\n"),
            Script(prefix=["ssh"]),
        ]
    )
    executor.runner = runner
    executor.install(work, host, InstallConfig(command="npm ci", when=["lock"]))
    assert len(runner.calls) == 4
    assert runner.calls[1].method == "start_stream"
    assert "npm ci" in runner.calls[1].argv[-1]
    assert runner.calls[-1].argv[-1].endswith("a" * 64)


@pytest.mark.parametrize("marker", ["", "\n", " \t\n"])
def test_empty_marker_reports_marker_recovery_without_installing(tmp_path: Path, marker: str) -> None:
    executor, work, host = context(tmp_path)
    runner = FakeRunner([Script(prefix=["ssh"], stdout="a" * 64 + "  -\n" + marker)])
    executor.runner = runner
    with pytest.raises(PreconditionError, match="Invalid installation marker") as caught:
        executor.install(work, host, InstallConfig(command="npm ci"))
    assert caught.value.fix == "Remove the host .offbox/install.sha256 marker and retry."
    assert len(runner.calls) == 1


@pytest.mark.parametrize("status", [1, 42, 130, 143])
def test_failed_install_never_writes_marker_and_preserves_signal_status(tmp_path: Path, status: int) -> None:
    executor, work, host = context(tmp_path)
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], stdout="a" * 64 + "  -\nmissing\n"),
            Script(prefix=["ssh"], method="start_stream", returncode=status),
        ]
    )
    executor.runner = runner
    with pytest.raises(OffboxError) as caught:
        executor.install(work, host, InstallConfig(command="npm ci"))
    assert caught.value.exit_code == (status if status in (130, 143) else 1)
    assert len(runner.calls) == 2


def test_marker_read_io_error_stops_before_install(tmp_path: Path) -> None:
    executor, work, host = context(tmp_path)
    runner = FakeRunner([Script(prefix=["ssh"], returncode=3, stderr="Input/output error")])
    executor.runner = runner
    with pytest.raises(CommandFailed, match="Input/output error"):
        executor.install(work, host, InstallConfig(command="npm ci"))
    assert len(runner.calls) == 1
    script = runner.calls[-1].input or ""
    assert "export LC_ALL=C" in script
    assert "No such file or directory" in script and "Not a directory" in script
    assert "*) printf" in script


@pytest.mark.parametrize("name", ["/etc/passwd", "../lock", "nested/../lock", "./lock", "a//b", "a\\b", "line\nbreak", "", "a\x00b"])
def test_unsafe_hash_paths_fail_before_network(tmp_path: Path, name: str) -> None:
    executor, work, host = context(tmp_path)
    with pytest.raises(PreconditionError, match="Unsafe install.when"):
        executor.install(work, host, InstallConfig(command="npm ci", when=[name]))
    assert isinstance(executor.runner, FakeRunner) and executor.runner.calls == []


def test_hash_contract_preserves_configured_order_and_empty_file_semantics(tmp_path: Path) -> None:
    executor, work, host = context(tmp_path)
    runner = FakeRunner([Script(prefix=["ssh"], stdout="a" * 64 + "  -\n")])
    executor.runner = runner
    assert executor.install_hash(work, host, ["z.lock", "empty.lock", "missing.lock"]) == "a" * 64
    call = runner.calls[0]
    assert call.argv[-1].endswith("z.lock empty.lock missing.lock")
    script = call.input or ""
    assert "'regular file'|'regular empty file'" in script
    assert "digest=$(printf '' | sha256sum)" in script
    assert 'printf \'%s  %s\\n\' "$digest" "$file" >> "$tmp"' in script
    assert 'sha256sum < "$tmp"' in script
    assert install_paths(["z.lock", "empty.lock", "missing.lock"]) == ["z.lock", "empty.lock", "missing.lock"]


def test_per_environment_lock_refuses_concurrent_install_but_allows_other_worktrees(tmp_path: Path) -> None:
    executor, work, _ = context(tmp_path)
    with executor.installation_lock(work):
        with pytest.raises(PreconditionError, match="already running") as caught, executor.installation_lock(work):
            pytest.fail("same environment lock acquired twice")
        assert caught.value.fix == "Retry after the current installation finishes."
        with executor.installation_lock(Worktree(root=tmp_path / "other", name="other-abcd")):
            assert (tmp_path / "state/install-locks/other-abcd").is_file()
    with executor.installation_lock(work):
        assert (tmp_path / "state/install-locks/app-abcd").stat().st_mode & 0o777 == 0o600


def test_up_stages_order_mise_install_stack_and_forwards_configured_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor, work, host = context(tmp_path)
    (tmp_path / "mise.toml").write_text('[tools]\npython="3.12"\n')
    monkeypatch.setenv("NPM_TOKEN", "private")
    project = ProjectConfig.model_validate(
        {"install": {"command": "npm ci", "when": ["lock"]}, "stack": {"up": "docker compose up -d"}, "exec": {"env": ["NPM_TOKEN"]}}
    )
    runner = FakeRunner(
        [
            Script(prefix=["ssh"], method="start_stream"),
            Script(prefix=["ssh"], method="start_stream"),
            Script(prefix=["ssh"], stdout="b" * 64 + "  -\n"),
            Script(prefix=["ssh"]),
            Script(prefix=["ssh"], method="start_stream"),
        ]
    )
    executor.runner = runner
    executor.up_stages(work, host, project)
    stages = [call for call in runner.calls if call.method == "start_stream"]
    assert len(stages) == 3
    assert "mise install" in stages[0].argv[-1] and "MISE_YES=1" in stages[0].argv[-1]
    assert "npm ci" in stages[1].argv[-1] and "MISE_YES" not in stages[1].argv[-1]
    assert "docker compose up -d" in stages[2].argv[-1]
    assert all("NPM_TOKEN=private" in call.argv[-1] for call in stages)


@pytest.mark.parametrize("no_stack", [False, True])
def test_compose_fallback_and_no_stack(tmp_path: Path, no_stack: bool) -> None:
    executor, work, host = context(tmp_path)
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    runner = FakeRunner([] if no_stack else [Script(prefix=["ssh"], method="start_stream")])
    executor.runner = runner
    executor.up_stages(work, host, ProjectConfig(), no_stack=no_stack)
    assert len(runner.calls) == (0 if no_stack else 1)
    if not no_stack:
        assert "docker compose up -d" in runner.calls[0].argv[-1]


def test_no_implicit_package_detection_or_stack_down(tmp_path: Path) -> None:
    executor, work, host = context(tmp_path)
    (tmp_path / "package.json").write_text("{}")
    executor.up_stages(work, host, ProjectConfig.model_validate({"stack": {"down": "must-not-run"}}))
    assert isinstance(executor.runner, FakeRunner) and executor.runner.calls == []
