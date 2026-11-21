"""Shared fixtures: the integration is loaded from custom_components/ of this repository."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.frontend import DATA_EXTRA_JS_URL_ES5, DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import mock_component

from custom_components.steam_engine.steam_images import StoreUnavailable

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/steam_engine in every test."""


@pytest.fixture(autouse=True)
def mock_frontend(hass: HomeAssistant) -> None:
    """The integration depends on the frontend (it serves the dashboard card). The real frontend needs
    the package home-assistant-frontend, which the test requirements do not install (its setup
    fails on "import hass_frontend"), and pulls in many other integrations. So it counts as set up
    here, with the lists of extra module URLs it would create (add_extra_js_url adds to them)."""
    mock_component(hass, "frontend")
    hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda *_: None, [])
    hass.data[DATA_EXTRA_JS_URL_ES5] = UrlManager(lambda *_: None, [])


@pytest.fixture(autouse=True)
def store_offline() -> Generator[AsyncMock]:
    """The game images (steam_images.py) ask the Steam store once time moves past their start delay.
    Here the store is not reachable unless a test answers for it through the returned mock (or
    patches async_get_images itself), so no test sends a request by accident."""
    with patch(
        "custom_components.steam_engine.steam_images.async_get_images",
        AsyncMock(side_effect=StoreUnavailable("offline in tests")),
    ) as get_images:
        yield get_images
