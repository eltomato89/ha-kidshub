"""Test fixtures for KidsHub."""

from pathlib import Path

import custom_components
import pytest

# pytest-homeassistant-custom-component ships its own ``custom_components``
# package which is imported first and shadows this repository's one. Add the
# repository directory to its search path so ``custom_components.kidshub``
# is discoverable.
_REPO_CC = str(Path(__file__).parent.parent / "custom_components")
if _REPO_CC not in custom_components.__path__:
    custom_components.__path__.append(_REPO_CC)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of custom integrations in all tests."""
    yield
