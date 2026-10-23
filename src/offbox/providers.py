import hashlib
import os
import re
import shutil
import tomllib
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from offbox.config import Model, hetzner_token
from offbox.errors import PreconditionError
from offbox.logging import register_secrets
from offbox.paths import Paths


class ProviderDefaults(Model):
    size: str | None = None
    region: str | None = None
    image: str | None = None


class ProviderManifest(Model):
    name: str
    description: str
    driver: Literal["tofu", "none"]
    credentials_env: list[str] = Field(default_factory=list[str])
    defaults: ProviderDefaults = Field(default_factory=ProviderDefaults)


class Provider(Model):
    manifest: ProviderManifest
    directory: Path


def validate_name(value: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value) is None:
        raise PreconditionError(f"Invalid name: {value!r}", "Use 1–32 lowercase letters, digits or hyphens, starting with a letter.")
    return value


def load_provider(directory: Path, name: str | None = None) -> Provider:
    path = directory / "provider.toml"
    try:
        with path.open("rb") as source:
            manifest = ProviderManifest.model_validate(tomllib.load(source))
        validate_name(manifest.name)
        if name is not None and manifest.name != name:
            raise PreconditionError(f"Provider name does not match {name}: {path}", "Correct the provider manifest name.")
        for variable in manifest.credentials_env:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable) is None:
                raise PreconditionError(f"Invalid credential variable in {path}", "Use valid environment variable names.")
        return Provider(manifest=manifest, directory=directory)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise PreconditionError(f"Cannot load provider {path}", "Check the provider manifest syntax, fields and permissions.") from exc


def discover_provider(name: str, paths: Paths) -> Provider:
    validate_name(name)
    custom = paths.config / "providers" / name
    if custom.exists():
        return load_provider(custom, name)
    resource = files("offbox").joinpath("_data", "providers", name)
    if not resource.is_dir():
        raise PreconditionError(f"Unknown provider: {name}", f"Install a provider in {custom}.")
    return load_provider(Path(str(resource)), name)


def copy_provider(provider: Provider, destination: Path) -> str:
    if any(path.is_symlink() for path in provider.directory.rglob("*")):
        raise PreconditionError("Provider contains symbolic links", "Replace provider symlinks with regular files before creating a host.")
    try:
        shutil.copytree(provider.directory, destination)
        digest = hashlib.sha256()
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                digest.update(path.relative_to(destination).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
        return digest.hexdigest()
    except OSError as exc:
        raise PreconditionError(f"Cannot copy provider to {destination}", "Check free space and state directory permissions.") from exc


def credentials_env(manifest: ProviderManifest, paths: Paths, env: Mapping[str, str] | None = None) -> dict[str, str]:
    values = os.environ if env is None else env
    result: dict[str, str] = {}
    for name in manifest.credentials_env:
        if name == "HCLOUD_TOKEN":
            result[name] = hetzner_token(paths.credentials, values).get_secret_value()
        elif values.get(name):
            result[name] = values[name]
        else:
            raise PreconditionError(f"Missing provider credential: {name}", f"Set {name} before retrying.")
    register_secrets(result)
    return result
