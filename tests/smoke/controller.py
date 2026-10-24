import fcntl
import hashlib
import json
import logging
import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from pydantic import BaseModel, ConfigDict, Field

from offbox import console
from offbox.config import Model
from offbox.drivers.none import NoneDriver
from offbox.errors import OffboxError, PreconditionError, ProviderError
from offbox.execute import INSTALL_PROBE_SCRIPT
from offbox.host.ready import SystemClock
from offbox.naming import Worktree, worktree_name
from offbox.paths import Paths
from offbox.ports import PORT_DISCOVERY_SCRIPT, PortSnapshot
from offbox.runner import Result, StreamProcess, SubprocessRunner
from offbox.ssh import ensure_include, known_host, ssh_argv, sync_config, write_private
from offbox.state import HostState, State, StateStore
from offbox.sync import SyncManager

logger = logging.getLogger(__name__)


class FixtureManifest(Model):
    envs: list[Worktree]


class SmokeReport(Model):
    completed: list[str] = Field(default_factory=list[str])
    errors: list[str] = Field(default_factory=list[str])


class PortForward(BaseModel):
    model_config = ConfigDict(extra="ignore")
    env: str
    remote: int
    local: int
    active: bool


class PortsReport(BaseModel):
    model_config = ConfigDict(extra="ignore")
    forwards: list[PortForward]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PreconditionError(message, "Inspect the smoke report and fixture logs.")


class NestedHostRunner(SubprocessRunner):
    def __init__(self, host: HostState, work: Worktree) -> None:
        super().__init__()
        self.host = host
        self.work = work

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
    ) -> Result:
        require(argv[0] == "ssh", "Nested-host probe received a non-SSH command")
        nested = ssh_argv(self.host.name, ["docker", "exec", "-u", "root", self.work.container, "sh", "-c", argv[-1]])
        return super().run(nested, check=check, capture=capture, input=input, env=env, cwd=cwd, timeout=timeout)


class Smoke:
    def __init__(self) -> None:
        self.runner = SubprocessRunner()
        self.clock = SystemClock()
        self.paths = Paths.discover()
        self.fixture_id = os.environ["SMOKE_ID"]
        self.base_image = os.environ["SMOKE_BASE_IMAGE"]
        self.fixture_image = os.environ["SMOKE_FIXTURE_IMAGE"]
        self.root = Path(f"/tmp/offbox-smoke-{self.fixture_id}")
        self.artifacts = Path("/artifacts")
        self.worktrees: list[Worktree] = []
        self.report = SmokeReport()
        self.sequence = 0
        self.host = HostState(
            name="smoke",
            provider="ssh",
            address=os.environ["SMOKE_HOST"],
            ssh_user="dev",
            created_at=datetime.now(UTC),
            known_hosts_path=self.paths.state / "known_hosts",
            ssh_key=Path.home() / ".ssh/id_ed25519",
        )

    def run_command(
        self, argv: list[str], *, cwd: Path | None = None, input: str | None = None, expected: int = 0, timeout: float = 120
    ) -> Result:
        self.sequence += 1
        result = self.runner.run(argv, cwd=cwd, input=input, check=False, timeout=timeout)
        prefix = self.artifacts / f"command-{self.sequence:03}"
        prefix.with_suffix(".stdout.log").write_text(result.stdout)
        prefix.with_suffix(".stderr.log").write_text(result.stderr)
        require(result.returncode == expected, f"Smoke command {self.sequence} exited {result.returncode}, expected {expected}: {argv}")
        return result

    def cli(self, work: Worktree, *args: str, expected: int = 0, input: str | None = None, timeout: float = 180) -> Result:
        return self.run_command([sys.executable, "-m", "offbox", *args], cwd=work.root, expected=expected, input=input, timeout=timeout)

    def complete(self, message: str) -> None:
        self.report.completed.append(message)
        console.success(message)
        self.write_report()

    def write_report(self) -> None:
        (self.artifacts / "report.json").write_text(self.report.model_dump_json(indent=2) + "\n")

    def batched_probe_guards(self) -> None:
        directory = self.root / "probe-guards"
        work = directory / "work"
        work.mkdir(parents=True)
        lock = work / "lock"
        lock.write_text("lock contents\n")
        digest = hashlib.sha256(f"{hashlib.sha256(lock.read_bytes()).hexdigest()}  lock\n".encode()).hexdigest()
        command = ["sh", "-s", "--", str(work), str(directory), "lock"]
        result = self.run_command(command, input=INSTALL_PROBE_SCRIPT)
        require(
            result.stdout.splitlines() == [digest + "  -", "missing"], "Combined install probe changed its hash/missing-marker contract"
        )
        metadata = directory / ".offbox"
        metadata.mkdir()
        marker = metadata / "install.sha256"
        marker.write_text(digest + "\n")
        result = self.run_command(command, input=INSTALL_PROBE_SCRIPT)
        require(result.stdout.splitlines() == [digest + "  -", digest], "Combined install probe lost the existing marker")
        lock.unlink()
        lock.symlink_to(marker)
        self.run_command(command, input=INSTALL_PROBE_SCRIPT, expected=3)
        lock.unlink()
        lock.write_text("lock contents\n")
        marker.unlink()
        marker.symlink_to(lock)
        self.run_command(command, input=INSTALL_PROBE_SCRIPT, expected=3)
        require(lock.read_text() == "lock contents\n", "A refused probe changed its symlink target")
        bin_dir = directory / "bin"
        bin_dir.mkdir()
        write_private(
            bin_dir / "docker",
            '#!/bin/sh\nset -eu\ncase "$1" in\n'
            'container) cat "$OFFBOX_PROBE_INSPECTION" ;;\n'
            'exec) touch "$OFFBOX_PROBE_EXECUTED"; cat "$OFFBOX_PROBE_COMPOSE" ;;\n'
            "*) exit 9 ;;\nesac\n",
        )
        (bin_dir / "docker").chmod(0o700)
        inspection_file = directory / "inspection.json"
        compose_file = directory / "compose.json"
        compose_file.write_text("[]\n")
        for name, running, label in (("owned", True, "probe-abcd"), ("unowned", True, "other-abcd"), ("stopped", False, "probe-abcd")):
            executed = directory / f"executed-{name}"
            inspection = [
                {
                    "Name": "/offbox-probe-abcd",
                    "State": {"Running": running},
                    "Config": {"Labels": {"offbox.env": label, "offbox.worktree": str(work)}},
                    "NetworkSettings": {"Networks": {"offbox": {"IPAddress": "10.89.0.2"}}},
                }
            ]
            inspection_file.write_text(json.dumps(inspection))
            result = self.runner.run(
                ["sh", "-s", "--", "probe-abcd", str(work), "offbox-probe-abcd"],
                input=PORT_DISCOVERY_SCRIPT,
                env={
                    "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                    "OFFBOX_PROBE_INSPECTION": str(inspection_file),
                    "OFFBOX_PROBE_COMPOSE": str(compose_file),
                    "OFFBOX_PROBE_EXECUTED": str(executed),
                },
                timeout=10,
            )
            snapshot = PortSnapshot.model_validate_json(result.stdout)
            require(executed.exists() == (name == "owned"), "Port discovery queried Compose before validating ownership/running state")
            require(snapshot.compose == ("[]" if name == "owned" else ""), "Port discovery returned unexpected Compose output")
        for oversized in ("inspection", "compose"):
            inspection_file.write_text(
                json.dumps(
                    [
                        {
                            "Name": "/offbox-probe-abcd",
                            "State": {"Running": True},
                            "Config": {"Labels": {"offbox.env": "probe-abcd", "offbox.worktree": str(work)}},
                            "NetworkSettings": {"Networks": {"offbox": {"IPAddress": "10.89.0.2"}}},
                            "LogPath": "x" * 200_000 if oversized == "inspection" else "",
                        }
                    ]
                )
            )
            compose = json.dumps([{"Publishers": None, "Labels": "x" * 200_000 if oversized == "compose" else ""}])
            compose_file.write_text(compose)
            result = self.runner.run(
                ["sh", "-s", "--", "probe-abcd", str(work), "offbox-probe-abcd"],
                input=PORT_DISCOVERY_SCRIPT,
                env={
                    "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                    "OFFBOX_PROBE_INSPECTION": str(inspection_file),
                    "OFFBOX_PROBE_COMPOSE": str(compose_file),
                    "OFFBOX_PROBE_EXECUTED": str(directory / "executed-large"),
                },
                timeout=10,
            )
            snapshot = PortSnapshot.model_validate_json(result.stdout)
            require(snapshot.compose == compose, "Port discovery lost oversized Compose data")
            require(snapshot.containers[0].state.running, "Port discovery lost oversized inspection data")
        self.complete("Batched probes preserve hash/marker contracts, refuse symlinks, and guard ownership before Compose execution")

    def prepare(self) -> None:
        self.root.mkdir()
        write_private(self.host.known_hosts_path, known_host(self.host.name, Path("/keys/host.pub").read_text()))
        state = State(default_host=self.host.name, hosts={self.host.name: self.host})
        StateStore(self.paths.state).write(state)
        sync_config(self.paths, state)
        ensure_include(self.paths)
        deadline = self.clock.now() + 30
        while self.clock.now() < deadline:
            result = self.runner.run(
                ssh_argv(self.host.name, ["docker", "info"], options=["-o", "ConnectTimeout=2"]), check=False, timeout=3
            )
            if result.returncode == 0:
                break
            self.clock.sleep(0.2)
        else:
            raise PreconditionError("SSH fixture did not become ready", "Inspect the host fixture log and socket permissions.")
        source = self.root / "source"
        source.mkdir()
        (source / "site").mkdir()
        (source / "site/index.html").write_text("base\n")
        (source / "note.txt").write_text("initial\n")
        (source / "lockfile").write_text("initial\n")
        (source / "Dockerfile").write_text(f"FROM {self.base_image}\n")
        (source / "offbox.toml").write_text(
            '[env]\ndockerfile="Dockerfile"\ncpus=1\nmemory="1536m"\nshm_size="128m"\n'
            '[install]\ncommand=\'printf "setup-noise\\n"; touch /work/install-complete\'\nwhen=["lockfile"]\n'
            '[ports]\nforward=[8000]\nauto=true\n[sync]\nignore=["!.git"]\n'
        )
        (source / "compose.yaml").write_text(
            "services:\n  web:\n    image: python:3.13-alpine\n    container_name: offbox-smoke-web\n"
            '    ports: ["8000:8000"]\n    volumes: ["./site:/site:ro"]\n'
            '    command: ["python", "-m", "http.server", "8000", "--directory", "/site"]\n'
        )
        self.run_command(["git", "init", "-b", "main"], cwd=source)
        self.run_command(["git", "add", "."], cwd=source)
        self.run_command(
            ["git", "-c", "user.email=smoke@example.invalid", "-c", "user.name=Offbox smoke", "commit", "-qm", "fixture"], cwd=source
        )
        (self.root / "worktrees").mkdir()
        for name, response in (("first", "alpha"), ("second", "beta")):
            root = self.root / "worktrees" / name
            self.run_command(["git", "worktree", "add", "--detach", str(root), "HEAD"], cwd=source)
            require((root / ".git").is_file(), "Smoke fixture must use linked worktrees with .git pointer files")
            (root / "site/index.html").write_text(response + "\n")
            (root / "node_modules").mkdir()
            (root / "node_modules/ignored").write_text("must stay local\n")
            self.worktrees.append(Worktree(root=root, name=worktree_name(root)))
        manifest = FixtureManifest(envs=self.worktrees)
        (self.artifacts / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n")
        self.run_command(
            ["docker", "volume", "create", "--label", f"offbox.smoke={self.fixture_id}", f"offbox-smoke-sentinel-{self.fixture_id}"]
        )
        self.run_command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                f"offbox-smoke-sentinel-{self.fixture_id}",
                "--label",
                f"offbox.smoke={self.fixture_id}",
                "--network",
                "none",
                "--entrypoint",
                "sleep",
                self.fixture_image,
                "infinity",
            ]
        )
        self.complete("Pinned SSH host fixture and two real linked git worktrees prepared")

    def synchronization_and_execution(self) -> None:
        first, second = self.worktrees
        for work in self.worktrees:
            result = self.cli(work, "up", timeout=600)
            require(result.stdout == "", "up wrote setup output to stdout")
            result = self.cli(work, "exec", "--", "cat", "note.txt")
            require(result.stdout == "initial\n", "Initial source file did not sync")
            self.cli(
                work, "exec", "--", "sh", "-c", "test ! -e /work/.git && test ! -e /work/node_modules && test -f /work/install-complete"
            )
        require(
            (first.root / "compose.yaml").read_bytes() == (second.root / "compose.yaml").read_bytes(),
            "Compose files differ between worktrees",
        )
        (first.root / "note.txt").write_text("updated\n")
        require(self.cli(first, "exec", "--", "cat", "note.txt").stdout == "updated\n", "Updated source did not flush before exec")
        self.cli(first, "exec", "--", "sh", "-c", "printf 'generated\\n' > generated.txt")
        self.cli(first, "exec", "--", "true")
        require((first.root / "generated.txt").read_text() == "generated\n", "Remote generated file did not sync back")
        self.complete("Real Mutagen flushes preserve linked .git/node_modules ignores and two-way updates")
        result = self.cli(first, "exec", "--", "sh", "-c", "printf user-out; printf user-err >&2")
        require(result.stdout == "user-out" and result.stderr == "user-err", "Command stdout/stderr were changed or mixed")
        require(
            self.cli(first, "exec", "--", "sh", "-c", "if read -r value; then exit 99; fi; printf eof").stdout == "eof",
            "Default non-TTY stdin was not closed",
        )
        require(
            self.cli(first, "exec", "--stdin", "--", "cat", input="pipe payload\n").stdout == "pipe payload\n",
            "Explicit stdin was not preserved",
        )
        for status in (0, 42, 255):
            self.cli(first, "exec", "--", "sh", "-c", f"exit {status}", expected=status)
        (first.root / "lockfile").write_text("changed\n")
        result = self.cli(first, "exec", "--", "printf", "only-user-output")
        require(
            result.stdout == "only-user-output" and "setup-noise" in result.stderr, "Automatic reinstall polluted stdout or did not run"
        )
        self.complete("Real exec preserves stdout/stderr/stdin and statuses0/42/255; reinstall stays on stderr")

    def parallel_commands(self) -> None:
        processes: list[StreamProcess] = []
        try:
            with (self.paths.state / "ports.lock").open("a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                processes = [
                    self.runner.start_stream(
                        ["sh", "-c", f"cd {shlex.quote(str(work.root))} && exec {shlex.quote(sys.executable)} -m offbox exec -- true"]
                    )
                    for work in self.worktrees
                ]
                self.clock.sleep(10)
                require(all(process.poll() is None for process in processes), "Parallel commands did not wait for the held forwarding lock")
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            for process in processes:
                require(process.wait(timeout=30) == 0, "Parallel worktree command failed during lock contention")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
        self.complete("Parallel worktree commands wait for contended forwarding locks and both succeed")

    def existing_host_ownership(self) -> None:
        work = self.worktrees[0]
        nested = NestedHostRunner(self.host, work)
        prefix = ["docker", "exec", work.container, "docker"]
        labels = ["--label", "offbox.env=probe-abcd", "--label", "offbox.worktree=/fixture/probe"]
        self.run_command([*prefix, "volume", "create", *labels, "offbox-probe-abcd-docker"])
        self.run_command([*prefix, "volume", "create", "offbox-backup"])
        self.run_command([*prefix, "network", "create", "offbox"])
        self.run_command([*prefix, "run", "-d", "--name", "offbox-probe-abcd", *labels, "python:3.13-alpine", "sleep", "infinity"])
        NoneDriver(nested, self.host).destroy()
        self.run_command([*prefix, "volume", "inspect", "offbox-probe-abcd-docker"], expected=1)
        self.run_command([*prefix, "container", "inspect", "offbox-probe-abcd"], expected=1)
        self.run_command([*prefix, "volume", "inspect", "offbox-backup"])
        self.run_command([*prefix, "network", "inspect", "offbox"])
        self.run_command([*prefix, "network", "rm", "offbox"])
        self.run_command([*prefix, "network", "create", "--label", "offbox.managed=1", "offbox"])
        self.run_command([*prefix, "volume", "create", *labels, "offbox-probe-abcd-docker"])
        self.run_command([*prefix, "network", "connect", "offbox", "offbox-smoke-web"])
        try:
            NoneDriver(nested, self.host).destroy()
        except ProviderError as exc:
            require("unrelated attached" in exc.message, "Teardown failed for an unexpected reason")
        else:
            require(False, "Teardown did not refuse an unrelated network endpoint")
        self.run_command([*prefix, "volume", "inspect", "offbox-probe-abcd-docker"])
        self.run_command([*prefix, "network", "disconnect", "offbox", "offbox-smoke-web"])
        NoneDriver(nested, self.host).destroy()
        self.run_command([*prefix, "network", "inspect", "offbox"], expected=1)
        self.run_command([*prefix, "volume", "inspect", "offbox-backup"])
        self.run_command([*prefix, "volume", "rm", "offbox-backup"])
        self.complete("Existing-host destruction removes labelled resources and preserves unowned volumes/adopted networks")

    def shutdown_probes(self, work: Worktree) -> None:
        prefix = ["docker", "exec", work.container, "docker"]
        self.run_command([*prefix, "volume", "create", "shutdown-proof"])
        self.run_command(
            [
                *prefix,
                "run",
                "-d",
                "--name",
                "shutdown-proof",
                "--stop-timeout",
                "2",
                "-v",
                "shutdown-proof:/proof",
                "python:3.13-alpine",
                "sh",
                "-c",
                "trap 'echo terminated > /proof/term; exit 0' TERM; touch /proof/ready; while :; do sleep 1; done",
            ]
        )
        self.run_command([*prefix, "exec", "shutdown-proof", "sh", "-c", "until test -f /proof/ready; do sleep 0.1; done"])
        self.run_command(
            [
                *prefix,
                "run",
                "-d",
                "--name",
                "shutdown-stubborn",
                "--stop-timeout",
                "1",
                "python:3.13-alpine",
                "sh",
                "-c",
                "trap '' TERM; touch /tmp/ready; while :; do sleep 1; done",
            ]
        )
        self.run_command([*prefix, "exec", "shutdown-stubborn", "sh", "-c", "until test -f /tmp/ready; do sleep 0.1; done"])

    def verify_shutdown(self, work: Worktree) -> None:
        prefix = ["docker", "exec", work.container]
        self.run_command(
            [
                *prefix,
                "docker",
                "run",
                "--rm",
                "-v",
                "shutdown-proof:/proof",
                "python:3.13-alpine",
                "sh",
                "-c",
                'test -f /proof/term && test "$(cat /proof/term)" = terminated',
            ]
        )
        result = self.run_command([*prefix, "docker", "inspect", "--format", "{{.State.Running}}", "shutdown-stubborn"])
        require(result.stdout.strip() == "false", "Signal-ignoring inner container survived bounded outer shutdown")
        result = self.run_command([*prefix, "docker", "inspect", "--format", "{{.State.ExitCode}}", "shutdown-stubborn"])
        require(result.stdout.strip() == "137", "dockerd did not enforce the stubborn service stop deadline")
        self.complete("Outer stop delivers TERM to inner services, waits for dockerd, and bounds stubborn service shutdown")

    def root_owned_source(self, work: Worktree) -> None:
        self.cli(
            work,
            "exec",
            "--",
            "docker",
            "run",
            "--rm",
            "--user",
            "0",
            "-v",
            "/work:/source",
            "python:3.13-alpine",
            "sh",
            "-c",
            "mkdir -p /source/root-cleanup/nested; echo fixture > /source/root-cleanup/nested/file; chmod 755 /source/root-cleanup /source/root-cleanup/nested",
        )
        result = self.run_command(["stat", "-c", "%u", work.work + "/root-cleanup/nested"])
        require(result.stdout.strip() == "0", "Cleanup fixture did not create root-owned bind-mounted directories")

    def signals(self) -> None:
        work = self.worktrees[0]
        for name, status in (("INT", 130), ("TERM", 143)):
            output = self.artifacts / f"signal-{name}.stdout.log"
            errors = self.artifacts / f"signal-{name}.stderr.log"
            command = f'trap "exit 130" INT; trap "exit 143" TERM; echo $$ > /work/signal-{name}.pid; sleep 300'
            result = self.run_command(
                [
                    "bash",
                    "/checkout/tests/smoke/signal.sh",
                    work.container,
                    name,
                    str(output),
                    str(errors),
                    sys.executable,
                    "-m",
                    "offbox",
                    "exec",
                    "--no-sync",
                    "--no-tty",
                    "--",
                    "sh",
                    "-c",
                    command,
                ],
                cwd=work.root,
                timeout=45,
            )
            fields = result.stdout.split()
            require(
                len(fields) == 2 and fields[0] == str(status) and fields[1].isdigit() and int(fields[1]) <= 10000,
                f"SIG{name} did not preserve exit{status} within ten seconds: {result.stdout}",
            )
            require(output.read_text() == "", "Signal handling added command stdout")
            self.run_command(
                [
                    "docker",
                    "exec",
                    work.container,
                    "sh",
                    "-c",
                    'pid=$(cat "/work/signal-$1.pid"); ! /bin/kill -0 -- "-$pid" 2>/dev/null',
                    "sh",
                    name,
                ]
            )
        self.complete("Real CLI SIGINT/SIGTERM exit130/143 within ten seconds and leave no remote process group")

    def local_port(self, work: Worktree) -> int:
        result = self.cli(work, "ports", "--json")
        report = PortsReport.model_validate_json(result.stdout)
        mappings = [item.local for item in report.forwards if item.env == work.name and item.remote == 8000 and item.active]
        require(len(mappings) == 1, "Expected one active forward for remote8000")
        return mappings[0]

    def response(self, port: int, expected: str) -> None:
        deadline = self.clock.now() + 45
        observed = "no response"
        while self.clock.now() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    observed = response.read().decode()
                if observed == expected:
                    return
            except (OSError, URLError) as exc:
                observed = str(exc)
            self.clock.sleep(0.2)
        raise PreconditionError(f"Forwarded HTTP response mismatch on {port}: {observed}", "Inspect Compose, forward and container logs.")

    def forwarding_and_lifecycle(self) -> None:
        first, second = self.worktrees
        ports = [self.local_port(first), self.local_port(second)]
        require(ports == [8000, 8001], f"Isolated client should allocate local8000/8001: {ports}")
        self.response(ports[0], "alpha\n")
        self.response(ports[1], "beta\n")
        self.complete("Identical Compose names/remote8000 stay isolated through localhost8000/8001")
        self.cli(first, "exec", "--", "docker", "volume", "create", "smoke-data-marker")
        self.shutdown_probes(first)
        stopped = self.clock.now()
        self.cli(first, "stop")
        require(self.clock.now() - stopped < 25, "Outer stop exceeded the bounded inner shutdown budget")
        exit_code = self.run_command(["docker", "inspect", "--format", "{{.State.ExitCode}}", first.container])
        require(exit_code.stdout.strip() == "0", "Outer container was force-killed rather than completing graceful shutdown")
        self.cli(first, "up", timeout=600)
        self.verify_shutdown(first)
        require(self.local_port(first) == ports[0], "stop/up changed the stable local mapping")
        self.cli(first, "exec", "--", "docker", "volume", "inspect", "smoke-data-marker")
        self.response(ports[0], "alpha\n")
        self.cli(first, "up", "--fresh", timeout=600)
        require(self.cli(first, "exec", "--", "cat", "note.txt").stdout == "updated\n", "fresh removed synced code")
        self.cli(first, "exec", "--", "docker", "volume", "inspect", "smoke-data-marker", expected=1)
        self.response(self.local_port(first), "alpha\n")
        self.response(self.local_port(second), "beta\n")
        self.complete("stop/up resumes existing Docker data; fresh preserves code and replaces isolated Docker data")
        self.root_owned_source(first)
        for work in self.worktrees:
            self.cli(work, "rm", "--yes")
            require(SyncManager(self.runner).status(work, self.host) is None, "rm retained a Mutagen session")
            require(not Path(work.directory).exists(), "rm retained the host environment directory")
            self.run_command(["docker", "container", "inspect", work.container], expected=1)
            self.run_command(["docker", "volume", "inspect", work.volume], expected=1)
        require(StateStore(self.paths.state).read().envs == {}, "rm retained environment state")
        self.cli(first, "gc", "--yes")
        self.run_command(["docker", "container", "inspect", f"offbox-smoke-sentinel-{self.fixture_id}"])
        self.run_command(["docker", "volume", "inspect", f"offbox-smoke-sentinel-{self.fixture_id}"])
        self.complete("rm removes owned state/resources/sync; unowned sentinel container and volume survive rm/gc")

    def cleanup(self) -> None:
        failed: list[str] = []
        for work in self.worktrees:
            if work.name not in StateStore(self.paths.state).read().envs:
                continue
            try:
                self.cli(work, "rm", "--yes", timeout=120)
            except OffboxError as exc:
                failed.append(f"Environment cleanup failed: {exc}")
        for command in (["ssh", "-O", "exit", "offbox-smoke"], ["mutagen", "daemon", "stop"]):
            result = self.runner.run(command, check=False, timeout=10)
            if result.returncode:
                logger.warning("Fixture shutdown status%s: %s", result.returncode, result.stderr)
        for path in self.paths.logs.glob("*.log"):
            (self.artifacts / f"offbox-{path.name}").write_bytes(path.read_bytes())
        self.report.errors.extend(failed)
        self.write_report()


def main() -> int:
    smoke = Smoke()
    try:
        smoke.prepare()
        smoke.batched_probe_guards()
        smoke.synchronization_and_execution()
        smoke.parallel_commands()
        smoke.existing_host_ownership()
        smoke.signals()
        smoke.forwarding_and_lifecycle()
    except OffboxError as exc:
        smoke.report.errors.append(str(exc))
        console.error(str(exc), exc.fix)
    finally:
        smoke.cleanup()
    return 1 if smoke.report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
