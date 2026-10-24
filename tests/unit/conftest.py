from collections.abc import Generator

import pytest

import offbox.logging as offbox_logging


@pytest.fixture(autouse=True)
def isolated_redaction() -> Generator[None]:
    secrets: set[str] = vars(offbox_logging)["_registered_secrets"]
    secrets.clear()
    yield
    secrets.clear()
