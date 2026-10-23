from pathlib import Path

import pytest

from offbox.errors import PreconditionError
from offbox.logging import redact
from offbox.paths import Paths
from offbox.providers import copy_provider, credentials_env, discover_provider


def test_gcp_provider_is_discoverable_and_copied_for_recoverable_teardown(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "cache", tmp_path / "control")
    provider = discover_provider("gcp", paths)
    assert provider.manifest.driver == "tofu"
    assert provider.manifest.defaults.size == "e2-standard-4"
    assert provider.manifest.defaults.region == "us-east4-c"
    assert provider.manifest.defaults.image == "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
    workspace = tmp_path / "saved-provider"
    assert len(copy_provider(provider, workspace)) == 64
    assert (workspace / "main.tf").is_file()
    assert (workspace / "tests/provider.tftest.hcl").is_file()
    assert credentials_env(provider.manifest, paths, {"GOOGLE_PROJECT": "synthetic-project"}) == {"GOOGLE_PROJECT": "synthetic-project"}
    with pytest.raises(PreconditionError, match="GOOGLE_PROJECT") as caught:
        credentials_env(provider.manifest, paths, {})
    assert caught.value.fix == "Set GOOGLE_PROJECT before retrying."


@pytest.mark.parametrize("name", ["GOOGLE_CREDENTIALS", "GOOGLE_CLOUD_KEYFILE_JSON", "GCLOUD_KEYFILE_JSON", "GOOGLE_OAUTH_ACCESS_TOKEN"])
def test_google_credential_values_are_redacted(name: str) -> None:
    value = "synthetic-google-credential-value-79285"
    assert redact("provider diagnostic: " + value, {name: value}) == "provider diagnostic: [REDACTED]"
