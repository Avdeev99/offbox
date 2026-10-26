import os
import tomllib
import typing
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from offbox.config import Model, ProjectConfig, UserConfig, load_config
from offbox.errors import PreconditionError
from offbox.paths import Paths

PROJECT_FILE = "offbox.toml"
LOCAL_FILE = "offbox.local.toml"
Source = Literal["default", "user", "project", "local", "env"]
type Layer = dict[str, dict[str, JsonValue]]
_JSON = TypeAdapter[JsonValue](JsonValue)
_LAYER = TypeAdapter[Layer](dict[str, dict[str, JsonValue]])


class SettingKey(Model):
    section: str
    field: str
    is_list: bool


class Setting(Model):
    key: str
    value: JsonValue
    source: Source
    env: str


class ConfigFiles(Model):
    user: Path
    project: Path
    local: Path


class ConfigReport(Model):
    files: ConfigFiles
    settings: list[Setting]


class ResolvedProject(Model):
    config: ProjectConfig
    sources: dict[str, Source]


def setting_keys() -> dict[str, SettingKey]:
    """Every project setting as section.key, with its TOML section and field name."""
    keys: dict[str, SettingKey] = {}
    for section, info in ProjectConfig.model_fields.items():
        model = info.annotation
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise PreconditionError(f"Invalid project section {section}", "Report this offbox bug.")
        for name, field in model.model_fields.items():
            key = field.alias or name
            keys[f"{section}.{key}"] = SettingKey(section=section, field=key, is_list=typing.get_origin(field.annotation) is list)
    return keys


def env_name(key: str) -> str:
    return "OFFBOX_" + key.upper().replace(".", "_")


def parse_value(raw: str) -> JsonValue:
    """A TOML value such as true, 2, "8g" or [".env"]; text that is not a TOML value is taken as a plain string."""
    try:
        value: object = tomllib.loads(f"value = {raw}")["value"]
        return _JSON.validate_python(value, strict=True)
    except (tomllib.TOMLDecodeError, ValidationError):
        return raw


def _locations(exc: ValidationError) -> str:
    return ", ".join(".".join(str(part) for part in error["loc"]) or "<root>" for error in exc.errors(include_input=False))


def _validate(layer: Layer, origin: str) -> None:
    try:
        ProjectConfig.model_validate(layer)
    except ValidationError as exc:
        raise PreconditionError(f"Invalid configuration in {origin}: {_locations(exc)}", f"Correct these keys in {origin}.") from exc


def read_layer(path: Path) -> Layer:
    try:
        with path.open("rb") as source:
            data: object = tomllib.load(source)
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, UnicodeError, OSError) as exc:
        raise PreconditionError(f"Cannot read configuration {path}", f"Fix the TOML syntax and permissions in {path}.") from exc
    try:
        layer = _LAYER.validate_python(data, strict=True)
    except ValidationError as exc:
        raise PreconditionError(
            f"Invalid configuration in {path}: {_locations(exc)}", f"Use [section] tables with plain TOML values in {path}."
        ) from exc
    _validate(layer, str(path))
    return layer


def env_layer(environ: Mapping[str, str]) -> Layer:
    keys = setting_keys()
    names = {env_name(key): setting for key, setting in keys.items()}
    prefixes = {f"OFFBOX_{setting.section.upper()}_" for setting in keys.values()}
    layer: Layer = {}
    for name, raw in sorted(environ.items()):
        setting = names.get(name)
        if setting is not None:
            value = parse_value(raw)
            layer.setdefault(setting.section, {})[setting.field] = [value] if setting.is_list and isinstance(value, str) else value
        elif any(name.startswith(prefix) for prefix in prefixes):
            raise PreconditionError(f"Unknown setting {name}", "Run offbox config to list setting names and their variables.")
    _validate(layer, "OFFBOX_* environment variables")
    return layer


def user_layer(paths: Paths) -> Layer:
    project = load_config(paths.user_config, UserConfig).project
    _validate(project, f"{paths.user_config} [project]")
    return project


def resolve(paths: Paths, root: Path, environ: Mapping[str, str] | None = None) -> ResolvedProject:
    """Merge defaults, user [project] defaults, offbox.toml, offbox.local.toml and OFFBOX_* variables; later sources win per key."""
    layers: list[tuple[Source, Layer]] = [
        ("user", user_layer(paths)),
        ("project", read_layer(root / PROJECT_FILE)),
        ("local", read_layer(root / LOCAL_FILE)),
        ("env", env_layer(os.environ if environ is None else environ)),
    ]
    merged: Layer = {}
    sources: dict[str, Source] = dict.fromkeys(setting_keys(), "default")
    for source, layer in layers:
        for section, values in layer.items():
            for key, value in values.items():
                merged.setdefault(section, {})[key] = value
                sources[f"{section}.{key}"] = source
    try:
        config = ProjectConfig.model_validate(merged)
    except ValidationError as exc:
        details = "; ".join(error["msg"] for error in exc.errors(include_input=False))
        raise PreconditionError(
            f"Merged configuration is invalid: {details}", "Run offbox config to see which source sets each value."
        ) from exc
    return ResolvedProject(config=config, sources=sources)


def load_project(paths: Paths, root: Path, environ: Mapping[str, str] | None = None) -> ProjectConfig:
    return resolve(paths, root, environ).config


def report(paths: Paths, root: Path, environ: Mapping[str, str] | None = None) -> ConfigReport:
    resolved = resolve(paths, root, environ)
    dumped = resolved.config.model_dump(mode="json", by_alias=True)
    settings = [
        Setting(
            key=key, value=_JSON.validate_python(dumped[setting.section][setting.field]), source=resolved.sources[key], env=env_name(key)
        )
        for key, setting in setting_keys().items()
    ]
    return ConfigReport(files=ConfigFiles(user=paths.user_config, project=root / PROJECT_FILE, local=root / LOCAL_FILE), settings=settings)
