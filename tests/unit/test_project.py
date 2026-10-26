import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from offbox.cli import app
from offbox.errors import PreconditionError
from offbox.naming import Worktree
from offbox.paths import Paths
from offbox.project import ConfigReport, env_name, parse_value, report, resolve, setting_keys
from offbox.runner import Runner


def layout(tmp_path: Path) -> tuple[Paths, Path]:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    paths.config.mkdir(parents=True)
    root = tmp_path / "repo"
    root.mkdir()
    return paths, root


def test_defaults_apply_without_any_configuration(tmp_path: Path) -> None:
    paths, root = layout(tmp_path)
    resolved = resolve(paths, root, {})
    assert resolved.config.git.mirror is True
    assert resolved.config.files.copy_ == []
    assert set(resolved.sources.values()) == {"default"}


def test_layers_merge_per_key_with_later_sources_winning(tmp_path: Path) -> None:
    paths, root = layout(tmp_path)
    paths.user_config.write_text('[project.sync]\nignore = [".agent"]\n[project.env]\ncpus = 2\nmemory = "4g"\n')
    (root / "offbox.toml").write_text('[env]\ncpus = 3\n[install]\ncommand = "make deps"\n')
    (root / "offbox.local.toml").write_text('[env]\nmemory = "8g"\n[sync]\nignore = ["reports"]\n')
    resolved = resolve(paths, root, {"OFFBOX_ENV_CPUS": "6", "OFFBOX_GIT_MIRROR": "false"})
    config = resolved.config
    assert (config.env.cpus, config.env.memory, config.install.command, config.git.mirror) == (6, "8g", "make deps", False)
    assert config.sync.ignore == ["reports"]
    assert {
        key: resolved.sources[key] for key in ("env.cpus", "env.memory", "install.command", "sync.ignore", "git.mirror", "git.refs")
    } == {
        "env.cpus": "env",
        "env.memory": "local",
        "install.command": "project",
        "sync.ignore": "local",
        "git.mirror": "env",
        "git.refs": "default",
    }


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("true", True),
        ("0", 0),
        ("2.5", 2.5),
        ('"8g"', "8g"),
        ("8g", "8g"),
        ('[".env", "a,b"]', [".env", "a,b"]),
        ("make deps", "make deps"),
    ],
)
def test_env_values_are_toml_or_plain_text(raw: str, value: object) -> None:
    assert parse_value(raw) == value


def test_env_variables_cover_every_setting_and_a_plain_path_is_a_one_item_list(tmp_path: Path) -> None:
    paths, root = layout(tmp_path)
    assert env_name("files.copy") == "OFFBOX_FILES_COPY"
    assert env_name("env.shm_size") == "OFFBOX_ENV_SHM_SIZE"
    assert {env_name(key) for key in setting_keys()} >= {"OFFBOX_GIT_MIRROR", "OFFBOX_GIT_REFS", "OFFBOX_EXEC_ENV", "OFFBOX_STACK_UP"}
    config = resolve(
        paths, root, {"OFFBOX_FILES_COPY": "~/.config/gcloud/application_default_credentials.json", "OFFBOX_GIT_REFS": '["origin/a", "b"]'}
    ).config
    assert config.files.copy_ == ["~/.config/gcloud/application_default_credentials.json"]
    assert config.git.refs == ["origin/a", "b"]


def test_unrelated_and_marker_variables_are_ignored_but_misspelled_settings_fail(tmp_path: Path) -> None:
    paths, root = layout(tmp_path)
    resolve(paths, root, {"OFFBOX_ENV": "app-abcd", "OFFBOX_HARDEN": "1", "OFFBOX_VERSION": "0.1.0"})
    with pytest.raises(PreconditionError, match="Unknown setting OFFBOX_GIT_MIRORR") as caught:
        resolve(paths, root, {"OFFBOX_GIT_MIRORR": "false"})
    assert (
        caught.value.fix == "Unset OFFBOX_GIT_MIRORR; setting variables are OFFBOX_<SECTION>_<KEY> for keys in the configuration reference."
    )


def test_invalid_values_name_the_layer_that_set_them(tmp_path: Path) -> None:
    paths, root = layout(tmp_path)
    with pytest.raises(PreconditionError, match=r"OFFBOX_\* environment variables: git.mirror"):
        resolve(paths, root, {"OFFBOX_GIT_MIRROR": "maybe"})
    paths.user_config.write_text("[project.git]\nmirorr = true\n")
    with pytest.raises(PreconditionError, match=r"config.toml \[project\]: git.mirorr"):
        resolve(paths, root, {})
    paths.user_config.write_text("")
    (root / "offbox.local.toml").write_text("git = 1\n")
    with pytest.raises(PreconditionError, match="offbox.local.toml"):
        resolve(paths, root, {})


def test_image_and_dockerfile_are_one_setting_for_precedence(tmp_path: Path) -> None:
    paths, root = layout(tmp_path)
    paths.user_config.write_text('[project.env]\nimage = "example/image:1"\n')
    (root / "offbox.toml").write_text('[env]\ndockerfile = "Dockerfile"\n')
    resolved = resolve(paths, root, {})
    assert (resolved.config.env.image, resolved.config.env.dockerfile) == (None, Path("Dockerfile"))
    assert (resolved.sources["env.image"], resolved.sources["env.dockerfile"]) == ("default", "project")
    image = resolve(paths, root, {"OFFBOX_ENV_IMAGE": "example/other:2"})
    assert (image.config.env.image, image.config.env.dockerfile) == ("example/other:2", None)


@pytest.mark.parametrize(
    ("name", "raw", "key", "value"),
    [
        ("OFFBOX_ENV_MEMORY", "4294967296", "memory", "4294967296"),
        ("OFFBOX_INSTALL_COMMAND", "true", "command", "true"),
        ("OFFBOX_ENV_IMAGE", "1234", "image", "1234"),
        ("OFFBOX_PORTS_FORWARD", "3000", "forward", [3000]),
        ("OFFBOX_SYNC_IGNORE", "2024", "ignore", ["2024"]),
        ("OFFBOX_ENV_CPUS", "1.5", "cpus", 1.5),
        ("OFFBOX_FILES_COPY", "", "copy", []),
    ],
)
def test_env_values_follow_the_setting_type(tmp_path: Path, name: str, raw: str, key: str, value: object) -> None:
    paths, root = layout(tmp_path)
    dumped = resolve(paths, root, {name: raw}).config.model_dump(by_alias=True)
    section = {"MEMORY": "env", "COMMAND": "install", "IMAGE": "env", "FORWARD": "ports", "IGNORE": "sync", "CPUS": "env", "COPY": "files"}[
        name.rsplit("_", 1)[1]
    ]
    assert dumped[section][key] == value


@pytest.mark.parametrize("raw", ["inf", "nan", "-1"])
def test_cpu_limits_must_be_finite_and_positive(tmp_path: Path, raw: str) -> None:
    paths, root = layout(tmp_path)
    with pytest.raises(PreconditionError, match="env.cpus"):
        resolve(paths, root, {"OFFBOX_ENV_CPUS": raw})


def test_report_lists_every_setting_with_source_and_variable(tmp_path: Path) -> None:
    paths, root = layout(tmp_path)
    (root / "offbox.toml").write_text('[files]\ncopy = ["~/token"]\n')
    result = report(paths, root, {})
    settings = {setting.key: setting for setting in result.settings}
    assert set(settings) == set(setting_keys())
    assert (settings["files.copy"].value, settings["files.copy"].source, settings["files.copy"].env) == (
        ["~/token"],
        "project",
        "OFFBOX_FILES_COPY",
    )
    assert (settings["env.image"].source, settings["env.memory"].value) == ("default", None)
    assert result.files.local == root / "offbox.local.toml"


@pytest.mark.parametrize("json_output", [False, True])
def test_config_cli_prints_values_sources_and_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_output: bool) -> None:
    monkeypatch.delenv("OFFBOX_GIT_MIRROR")
    paths, root = layout(tmp_path)
    (root / "offbox.toml").write_text("[git]\nmirror = false\n")

    def worktree(runner: Runner, cwd: Path | None = None) -> Worktree:
        return Worktree(root=root, name="repo-abcd")

    monkeypatch.setattr("offbox.cli.config.Paths.discover", lambda: paths)
    monkeypatch.setattr("offbox.cli.config.find_worktree", worktree)
    result = CliRunner().invoke(app, ["config", *(["--json"] if json_output else [])])
    assert result.exit_code == 0
    if json_output:
        parsed = ConfigReport.model_validate_json(result.stdout)
        assert next(setting for setting in parsed.settings if setting.key == "git.mirror").source == "project"
        assert json.loads(result.stdout)["files"]["project"] == str(root / "offbox.toml")
    else:
        lines = result.stdout.splitlines()
        assert lines[0] == "Sources, later wins: default < user < project < local < env"
        assert f"  local    {root / 'offbox.local.toml'} (not present)" in lines
        assert any(line.split() == ["git.mirror", "false", "project", "OFFBOX_GIT_MIRROR"] for line in lines)
        assert any(line.split() == ["env.memory", "unset", "default", "OFFBOX_ENV_MEMORY"] for line in lines)
