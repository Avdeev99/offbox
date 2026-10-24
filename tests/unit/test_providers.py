import hashlib
from pathlib import Path

import pytest

from offbox.errors import PreconditionError
from offbox.providers import Provider, ProviderManifest, copy_provider

MANIFEST = ProviderManifest(name="example", description="Example", driver="tofu")


def provider_at(directory: Path) -> Provider:
    return Provider(manifest=MANIFEST, directory=directory)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_copy_provider_skips_local_tool_state_and_tests(tmp_path: Path) -> None:
    source = tmp_path / "provider"
    write(source / "provider.toml", "name = 'example'\n")
    write(source / "main.tf", "resource {}\n")
    write(source / "modules/network/main.tf", "module {}\n")
    write(source / ".terraform.lock.hcl", "lock\n")
    write(source / ".terraform/providers/registry/plugin", "binary\n")
    (source / ".terraform/providers/cached").symlink_to(tmp_path)
    write(source / ".terraform.tfstate.lock.info", "{}\n")
    write(source / "terraform.tfstate", "{}\n")
    write(source / "terraform.tfstate.backup", "{}\n")
    write(source / "terraform.tfstate.d/dev/terraform.tfstate", "{}\n")
    write(source / "offbox.auto.tfvars.json", "{}\n")
    write(source / "plan.tfplan", "plan\n")
    write(source / ".main.tf.swp", "swap\n")
    write(source / ".gitkeep", "")
    write(source / "tests/provider.tftest.hcl", "run {}\n")
    destination = tmp_path / "workspace"

    digest = copy_provider(provider_at(source), destination)

    copied = sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file())
    assert copied == [".terraform.lock.hcl", "main.tf", "modules/network/main.tf", "provider.toml"]
    expected = hashlib.sha256()
    for name in copied:
        expected.update(name.encode() + b"\0" + (destination / name).read_bytes() + b"\0")
    assert digest == expected.hexdigest()


def test_copy_provider_rejects_symlinks_in_provider_files(tmp_path: Path) -> None:
    source = tmp_path / "provider"
    write(source / "provider.toml", "name = 'example'\n")
    (source / "main.tf").symlink_to(source / "provider.toml")
    with pytest.raises(PreconditionError, match="symbolic links"):
        copy_provider(provider_at(source), tmp_path / "workspace")
    assert not (tmp_path / "workspace").exists()


def test_copy_provider_refuses_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "provider"
    write(source / "provider.toml", "name = 'example'\n")
    destination = tmp_path / "workspace"
    destination.mkdir()
    with pytest.raises(PreconditionError, match="Cannot copy provider"):
        copy_provider(provider_at(source), destination)


@pytest.mark.parametrize("name", ["hetzner", "gcp"])
def test_builtin_cloud_providers_ship_a_multi_platform_lock_file(name: str) -> None:
    lock = (Path(__file__).parents[2] / "providers" / name / ".terraform.lock.hcl").read_text()
    for registry in ("registry.terraform.io", "registry.opentofu.org"):
        assert f'provider "{registry}/' in lock
    assert lock.count("zh:") >= 4
