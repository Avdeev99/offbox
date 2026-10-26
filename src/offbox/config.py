import os
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, ValidationError, model_validator

from offbox import DEFAULT_IMAGE
from offbox.errors import PreconditionError


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class HostPreset(Model):
    provider: str = "hetzner"
    size: str | None = None
    region: str | None = None
    image: str | None = None
    address: str | None = None
    port: int = Field(default=22, ge=1, le=65535)
    ssh_key: Path | None = None


class UserConfig(Model):
    default_host: str | None = None
    hosts: dict[str, HostPreset] = Field(default_factory=dict)
    project: dict[str, dict[str, JsonValue]] = Field(default_factory=dict[str, dict[str, JsonValue]])


class EnvConfig(Model):
    image: str | None = None
    dockerfile: Path | None = None
    cpus: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    memory: str | None = None
    shm_size: str = "2g"

    @model_validator(mode="after")
    def validate_image(self) -> Self:
        if self.image is not None and self.dockerfile is not None:
            raise ValueError("env.image and env.dockerfile are mutually exclusive")
        if self.image is None and self.dockerfile is None:
            self.image = DEFAULT_IMAGE
        return self

    def resource_limits(self, host_cpus: int, host_memory: int) -> tuple[float, str]:
        if host_cpus < 1 or host_memory < 1:
            raise PreconditionError("Invalid host resource information", "Check the host CPU and memory information.")
        return self.cpus if self.cpus is not None else float(max(1, host_cpus - 1)), self.memory or str(host_memory * 3 // 4)


class InstallConfig(Model):
    command: str | None = None
    when: list[str] = Field(default_factory=list)


class StackConfig(Model):
    up: str | None = None
    down: str | None = None

    def commands(self, root: Path) -> tuple[str | None, str | None]:
        compose = any(
            (root / filename).is_file() for filename in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
        )
        return self.up or ("docker compose up -d" if compose else None), self.down or ("docker compose down" if compose else None)


class PortsConfig(Model):
    forward: list[int] = Field(default_factory=list[int])
    auto: bool = True

    @model_validator(mode="after")
    def validate_ports(self) -> Self:
        if any(port < 1 or port > 65535 for port in self.forward):
            raise ValueError("ports.forward entries must be between 1 and 65535")
        return self


class SyncConfig(Model):
    ignore: list[str] = Field(default_factory=list)


class ExecConfig(Model):
    env: list[str] = Field(default_factory=list)


class FilesConfig(Model):
    copy_: list[str] = Field(default_factory=list, alias="copy")


class GitConfig(Model):
    mirror: bool = True
    refs: list[str] = Field(default_factory=list)


class ProjectConfig(Model):
    env: EnvConfig = Field(default_factory=EnvConfig)
    install: InstallConfig = Field(default_factory=InstallConfig)
    stack: StackConfig = Field(default_factory=StackConfig)
    ports: PortsConfig = Field(default_factory=PortsConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    exec: ExecConfig = Field(default_factory=ExecConfig)
    files: FilesConfig = Field(default_factory=FilesConfig)
    git: GitConfig = Field(default_factory=GitConfig)


class HetznerCredentials(Model):
    token: SecretStr | None = None


class Credentials(Model):
    hetzner: HetznerCredentials = Field(default_factory=HetznerCredentials)


def load_config[T: BaseModel](path: Path, model: type[T]) -> T:
    try:
        with path.open("rb") as source:
            return model.model_validate(tomllib.load(source))
    except FileNotFoundError:
        return model()
    except ValidationError as exc:
        locations = [".".join(str(part) for part in error["loc"]) or "<root>" for error in exc.errors(include_input=False)]
        raise PreconditionError(f"Invalid configuration in {path}: {', '.join(locations)}", f"Correct these keys in {path}.") from exc
    except (tomllib.TOMLDecodeError, UnicodeError, OSError) as exc:
        raise PreconditionError(f"Cannot read configuration {path}", f"Fix the TOML syntax and permissions in {path}.") from exc


def load_credentials(path: Path) -> Credentials:
    try:
        with path.open("rb") as source:
            if stat.S_IMODE(os.fstat(source.fileno()).st_mode) & 0o177:
                raise PreconditionError(f"Credentials file {path} is more permissive than 0600", f"chmod 600 {path}")
            return Credentials.model_validate(tomllib.load(source))
    except FileNotFoundError:
        return Credentials()
    except ValidationError as exc:
        locations = [".".join(str(part) for part in error["loc"]) or "<root>" for error in exc.errors(include_input=False)]
        raise PreconditionError(
            f"Invalid credentials file {path}: {', '.join(locations)}", f"Correct {path}; keep permissions at 0600."
        ) from exc
    except (tomllib.TOMLDecodeError, UnicodeError, OSError) as exc:
        raise PreconditionError(f"Invalid credentials file {path}", f"Correct {path}; keep permissions at 0600.") from exc


def hetzner_token(path: Path, env: Mapping[str, str] | None = None) -> SecretStr:
    values = os.environ if env is None else env
    token = values.get("HCLOUD_TOKEN")
    if token:
        return SecretStr(token)
    saved = load_credentials(path).hetzner.token
    if saved is not None and saved.get_secret_value():
        return saved
    raise PreconditionError(
        "Missing Hetzner credentials",
        "Set HCLOUD_TOKEN or hetzner.token in credentials.toml (mode 0600); see https://github.com/Avdeev99/offbox/blob/master/docs/installation.md#hetzner.",
    )


def default_host(user: UserConfig, state_default: str | None) -> str | None:
    return state_default if state_default is not None else user.default_host
