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
