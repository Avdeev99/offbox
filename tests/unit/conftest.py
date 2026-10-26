import os
from collections.abc import Generator

import pytest

import offbox.logging as offbox_logging


@pytest.fixture(autouse=True)
def isolated_redaction() -> Generator[None]:
    secrets: set[str] = vars(offbox_logging)["_registered_secrets"]
    secrets.clear()
    yield
    secrets.clear()


@pytest.fixture(autouse=True)
def isolated_offbox_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests start from the built-in defaults with the git mirror off; mirror tests enable it explicitly."""
    for name in [name for name in os.environ if name.startswith("OFFBOX_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("OFFBOX_GIT_MIRROR", "false")


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """No unit test may read or write the real home directory, such as scheduler units or SSH files."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
