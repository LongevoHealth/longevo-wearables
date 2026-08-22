"""Override global fixtures for service-level tests."""

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def mock_external_apis() -> Generator[dict[str, MagicMock], None, None]:
    """No-op override of the global mock_external_apis fixture.

    Unit tests in this directory test the actual implementations,
    not the mocked versions used in API integration tests.
    """
    yield {}
