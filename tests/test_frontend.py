"""Serving the dashboard card: the static path and the extra module URL with the version and a hash
of the file, once per run."""

from __future__ import annotations

import hashlib
from http import HTTPStatus
import json
from pathlib import Path
from unittest.mock import patch

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.steam_engine.const import DOMAIN, merged_options

INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / DOMAIN
CARD_FILE = INTEGRATION_DIR / "frontend" / "steam-engine-card.js"
CARD_URL = "/steam_engine/steam-engine-card.js"
VERSION = json.loads((INTEGRATION_DIR / "manifest.json").read_text(encoding="utf-8"))["version"]
# The card changes, so does its URL: browsers fetch it anew without clearing their cache
V = f"{VERSION}-{hashlib.sha256(CARD_FILE.read_bytes()).hexdigest()[:8]}"


async def test_card_registered_once_per_run(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})
    with (
        patch.object(
            hass.http, "async_register_static_paths", wraps=hass.http.async_register_static_paths
        ) as register,
        patch("custom_components.steam_engine.add_extra_js_url", wraps=add_extra_js_url) as add_js,
    ):
        entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # A reload (as after saving the options) registers nothing again
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    configs = [config for call in register.call_args_list for config in call.args[0]]
    card = [config for config in configs if config.url_path == CARD_URL]
    assert len(card) == 1
    assert isinstance(card[0], StaticPathConfig)
    assert Path(card[0].path).resolve() == CARD_FILE.resolve()
    assert card[0].cache_headers is False
    add_js.assert_called_once_with(hass, f"{CARD_URL}?v={V}")
    assert hass.data[DATA_EXTRA_MODULE_URL].urls == {f"{CARD_URL}?v={V}"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_card_is_served(hass: HomeAssistant, hass_client: ClientSessionGenerator) -> None:
    # Without a config entry too: the card then shows that Steam Engine is not set up
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_client()
    response = await client.get(f"{CARD_URL}?v={V}")
    assert response.status == HTTPStatus.OK
    assert "javascript" in response.content_type
    assert await response.read() == await hass.async_add_executor_job(CARD_FILE.read_bytes)
