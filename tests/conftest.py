"""Shared fixtures for Annex F unit tests."""

import pytest

from tests.annex_f_helpers import MockNode


@pytest.fixture
def mock_node():
    return MockNode()
