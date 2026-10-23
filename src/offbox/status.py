import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from offbox.config import Model, ProjectConfig, load_config
from offbox.errors import OffboxError, PreconditionError
from offbox.host import host_operation
from offbox.naming import Worktree
from offbox.paths import Paths
from offbox.ports import PortsReport
from offbox.runner import Runner
from offbox.runtime import ContainerInfo, DockerModel, HostResources, Runtime
from offbox.state import HostState
from offbox.sync import SyncHealth, health


class StatusCheck(Model):
    name: str
    level: Literal["ok", "warning", "error"]
    message: str
    fix: str | None = None

    @model_validator(mode="after")
    def require_fix(self) -> "StatusCheck":
        if self.level != "ok" and not self.fix:
            raise ValueError("A problem requires a fix")
        return self


class StackService(Model):
    name: str
    state: str
    health: str | None = None


class StatusReport(Model):
    env: str | None = None
    host: str | None = None
    checks: list[StatusCheck] = Field(default_factory=list[StatusCheck])
    ports: PortsReport = Field(default_factory=PortsReport)
    stack: list[StackService] = Field(default_factory=list[StackService])
    sync: SyncHealth | None = None

    @property
    def exit_code(self) -> int:
        return 3 if any(check.level == "error" for check in self.checks) else 0


class ComposeStatus(DockerModel):
    name: str = Field(alias="Service")
    state: str = Field(alias="State")
    health: str | None = Field(default=None, alias="Health")


class Platform(DockerModel):
    architecture: str
    os: str


class Descriptor(DockerModel):
    platform: Platform | None = None


class ConfigDigest(DockerModel):
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ImageManifest(DockerModel):
    config: ConfigDigest


class ManifestEntry(DockerModel):
    descriptor: Descriptor = Field(alias="Descriptor")
    oci: ImageManifest | None = Field(default=None, alias="OCIManifest")
    docker: ImageManifest | None = Field(default=None, alias="SchemaV2Manifest")


def registry_config_digest(text: str) -> str:
    try:
        entries = (
            TypeAdapter(list[ManifestEntry]).validate_json(text)
            if text.lstrip().startswith("[")
            else [ManifestEntry.model_validate_json(text)]
        )
        digests: set[str] = set()
        for entry in entries:
            platform = entry.descriptor.platform
            if platform is not None and (platform.os, platform.architecture) != ("linux", "amd64"):
                continue
            manifest = entry.oci or entry.docker
            if manifest is not None:
                digests.add(manifest.config.digest)
        if len(digests) != 1:
            raise ValueError("Expected one Linux amd64 image config digest")
        return digests.pop()
    except (ValidationError, ValueError) as exc:
        raise PreconditionError(
            "Cannot determine the registry image version", "Check docker manifest inspect --verbose for the configured image."
        ) from exc


def compose_status(text: str) -> list[StackService]:
    try:
        entries = (
            TypeAdapter(list[ComposeStatus]).validate_json(text)
            if text.lstrip().startswith("[")
            else [ComposeStatus.model_validate_json(line) for line in text.splitlines() if line.strip()]
        )
    except ValidationError as exc:
        raise PreconditionError("Invalid Compose health response", "Check docker compose ps --format json inside the environment.") from exc
    return [StackService(name=item.name, state=item.state, health=item.health or None) for item in entries]


class HostConfig(DockerModel):
    memory: int = Field(alias="Memory", ge=0)


class ResourceContainer(DockerModel):
    limits: HostConfig = Field(alias="HostConfig")


class Status:
    def __init__(self, paths: Paths, runner: Runner) -> None:
        self.runtime = Runtime(paths, runner)
        self.report = StatusReport()

    def capture(self, name: str, probe: Callable[[], StatusCheck]) -> bool:
        try:
            check = probe()
        except OffboxError as exc:
            check = StatusCheck(name=name, level="error", message=exc.message, fix=exc.fix or "Inspect offbox logs and retry.")
        self.report.checks.append(check)
        return check.level != "error"

    def disk(self, host: HostState) -> StatusCheck:
        text = self.runtime.remote(host, ["df", "-P", "/srv/offbox"], timeout=5).stdout
        lines = text.splitlines()
        columns = lines[-1].split() if lines else []
        if len(columns) < 5 or re.fullmatch(r"(?:100|[0-9]{1,2})%", columns[4]) is None:
            raise PreconditionError("Invalid host disk usage response", "Check df -P /srv/offbox on the host.")
        percent = int(columns[4][:-1])
        level: Literal["ok", "warning", "error"] = "error" if percent >= 95 else "warning" if percent >= 80 else "ok"
        return StatusCheck(
            name="disk",
            level=level,
            message=f"Host disk {percent}% used",
            fix="Run offbox gc or offbox rm in unused worktrees." if level != "ok" else None,
        )

    def resources(self, host: HostState) -> StatusCheck:
        try:
            resources = HostResources.model_validate_json(
                self.runtime.remote(host, ["docker", "info", "--format", '{"cpus":{{.NCPU}},"memory":{{.MemTotal}}}']).stdout
            )
            identifiers = self.runtime.remote(host, ["docker", "ps", "-q", "--filter", "label=offbox.env"]).stdout.split()
            if any(re.fullmatch(r"[a-f0-9]{12,64}", item) is None for item in identifiers):
                raise ValueError("Invalid Docker identifiers")
            containers = (
                TypeAdapter(list[ResourceContainer]).validate_json(
                    self.runtime.remote(host, ["docker", "container", "inspect", *identifiers]).stdout
                )
                if identifiers
                else []
            )
            if len(containers) != len(identifiers):
                raise ValueError("Missing running container resource information")
        except (ValidationError, ValueError) as exc:
            raise PreconditionError(
                "Invalid host memory information", "Inspect the host Docker daemon and running environment limits."
            ) from exc
        total = sum(item.limits.memory for item in containers)
        unlimited = any(item.limits.memory == 0 for item in containers)
        warning = unlimited or total > resources.memory
        return StatusCheck(
            name="resources",
            level="warning" if warning else "ok",
            message=(
                "Running environments include unlimited memory"
                if unlimited
                else f"Running memory limits {total} / {resources.memory} bytes"
            ),
            fix="Stop unused environments or reduce env.memory and run offbox up --fresh." if warning else None,
        )

    def image(self, work: Worktree, host: HostState, project: ProjectConfig, container: ContainerInfo) -> StatusCheck:
        try:
            if project.env.dockerfile is not None:
                dockerfile = self.runtime.dockerfile_path(work, project.env.dockerfile)
                image = self.runtime.custom_image_tag(host, work, dockerfile)
                result = self.runtime.remote(host, ["docker", "image", "inspect", "--format", "{{.Id}}", image], check=False, timeout=10)
                if result.returncode:
                    raise PreconditionError("Configured custom image has not been built", "Run offbox up --fresh.")
                digest = result.stdout.strip()
                if re.fullmatch(r"sha256:[a-f0-9]{64}", digest) is None:
                    raise PreconditionError("Cannot identify the cached custom image", "Run offbox up to build the configured Dockerfile.")
            else:
                result = self.runtime.remote(host, ["docker", "manifest", "inspect", "--verbose", project.env.image or ""], timeout=15)
                digest = registry_config_digest(result.stdout)
        except OffboxError as exc:
            return StatusCheck(
                name="image",
                level="warning",
                message=f"Image update check unavailable: {exc.message}",
                fix=exc.fix or "Check the image registry and retry.",
            )
        different = digest != container.image
        return StatusCheck(
            name="image",
            level="warning" if different else "ok",
            message="Newer image available" if different else "Image matches the configured image version",
            fix="Run offbox up --fresh." if different else None,
        )

    def diagnose(self, *, cwd: Path | None = None) -> StatusReport:
        try:
            work, _env, host = self.runtime.current(cwd)
        except OffboxError as exc:
            self.report.checks.append(StatusCheck(name="environment", level="error", message=exc.message, fix=exc.fix or "Run offbox up."))
            return self.report
        self.report.env, self.report.host = work.name, host.name
        project: ProjectConfig | None = None
        try:
            project = load_config(work.root / "offbox.toml", ProjectConfig)
        except OffboxError as exc:
            self.report.checks.append(StatusCheck(name="config", level="error", message=exc.message, fix=exc.fix or "Correct offbox.toml."))

        def reachable() -> StatusCheck:
            version = self.runtime.remote(host, ["cat", "/var/lib/offbox/version"], timeout=5).stdout.strip()
            if not version or re.fullmatch(r"[A-Za-z0-9.+_-]+", version) is None:
                raise PreconditionError("Invalid host offbox version", "Reinitialize the host with offbox host init.")
            return StatusCheck(name="host", level="ok", message=f"Host {host.name} reachable, offbox {version}")

        if not self.capture("host", reachable):
            return self.report
        self.capture("disk", lambda: self.disk(host))
        self.capture("resources", lambda: self.resources(host))
        container: ContainerInfo | None = None

        def environment() -> StatusCheck:
            nonlocal container
            container = self.runtime.inspect(host, work)
            if container is None or not container.state.running:
                raise PreconditionError("Environment is not running", "Run offbox up.")
            labels = container.config.labels or {}
            if (
                container.name.lstrip("/") != work.container
                or labels.get("offbox.env") != work.name
                or labels.get("offbox.worktree") != str(work.root)
            ):
                raise PreconditionError("Environment ownership does not match", "Inspect offbox list and the container ownership labels.")
            result = self.runtime.remote(
                host,
                [
                    "docker",
                    "exec",
                    work.container,
                    "sh",
                    "-c",
                    "test -f /run/offbox/ready && docker --host=unix:///var/run/docker.sock info >/dev/null 2>&1",
                ],
                check=False,
                timeout=5,
            )
            if result.returncode:
                raise PreconditionError(
                    "Environment Docker daemon is not ready", "Run offbox up and inspect offbox logs if readiness fails."
                )
            return StatusCheck(name="environment", level="ok", message="Environment is running and ready")

        ready = self.capture("environment", environment)

        def synchronization() -> StatusCheck:
            session = self.runtime.sync.status(work, host)
            if session is None:
                raise PreconditionError("Sync session is missing", "Run offbox up to create source sync.")
            self.report.sync = health(session)
            result = self.report.sync
            return StatusCheck(
                name="sync",
                level="ok" if result.ok else "error",
                message=f"Sync {result.state}; conflicts={result.conflicts}, problems={result.problems}",
                fix=result.fix,
            )

        self.capture("sync", synchronization)
        if ready and container is not None and project is not None:
            self.report.checks.append(self.image(work, host, project, container))

            def ports() -> StatusCheck:
                try:
                    with host_operation(self.runtime.paths, host.name):
                        self.report.ports = self.runtime.ports.restore(work, host, project.ports)
                except OffboxError:
                    current = self.runtime.store.read().envs.get(work.name)
                    if current is not None and current.host == host.name and current.worktree_path == work.root:
                        self.report.ports = self.runtime.ports.report(current)
                    raise
                return StatusCheck(name="ports", level="ok", message=f"{len(self.report.ports.forwards)} active port forwards")

            self.capture("ports", ports)
            if any((work.root / name).is_file() for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")):

                def stack() -> StatusCheck:
                    text = self.runtime.remote(
                        host,
                        [
                            "docker",
                            "exec",
                            "-u",
                            "dev",
                            "-w",
                            "/work",
                            work.container,
                            "docker",
                            "compose",
                            "ps",
                            "--all",
                            "--format",
                            "json",
                        ],
                    ).stdout
                    self.report.stack = compose_status(text)
                    failures = [
                        service.name
                        for service in self.report.stack
                        if service.state != "running" or service.health not in (None, "healthy")
                    ]
                    return StatusCheck(
                        name="stack",
                        level="error" if failures else "ok",
                        message="Stack: "
                        + (", ".join(failures) + " not healthy" if failures else f"{len(self.report.stack)} services healthy"),
                        fix="Run offbox logs --stack and offbox up." if failures else None,
                    )

                self.capture("stack", stack)
            else:
                self.report.checks.append(StatusCheck(name="stack", level="ok", message="No Compose stack configured"))
        return self.report
