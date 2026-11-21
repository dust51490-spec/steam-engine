"""The WebSocket commands for the dashboard card: steam_engine/entities (the current entity IDs),
steam_engine/images (the image URLs the Steam store gave) and steam_engine/subscribe_images (the same
as a subscription)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.steam_engine.const import (
    DOMAIN,
    ENTITY_INSTALLED_GAME,
    ENTITY_PC,
    UPDATES_SENSOR_ENTITY_ID,
    merged_options,
)
from custom_components.steam_engine.steam_images import (
    NEW_GAME_DELAY,
    START_DELAY,
    STORAGE_KEY as IMAGES_STORAGE_KEY,
    STORAGE_VERSION as IMAGES_STORAGE_VERSION,
)

LIBRARY = "sensor.steam_pc_library"
CDN = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps"


def _store_images(appid: str) -> dict[str, str]:
    return {"header": f"{CDN}/{appid}/abc/header.jpg", "capsule": f"{CDN}/{appid}/abc/capsule_231x87.jpg"}


def _library(hass: HomeAssistant, appids: list[str]) -> None:
    hass.states.async_set(LIBRARY, str(len(appids)), {"games": [{"appid": a, "name": f"Game {a}"} for a in appids]})


async def _entities(hass_ws_client: WebSocketGenerator, hass: HomeAssistant) -> dict:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "steam_engine/entities"})
    return await client.receive_json()


async def test_entities_by_unique_id(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    msg = await _entities(hass_ws_client, hass)
    assert msg["success"]
    own, agent = msg["result"]["entities"], msg["result"]["agent"]
    assert len(own) == 20
    assert own["pc"] == ENTITY_PC
    assert own["updates"] == UPDATES_SENSOR_ENTITY_ID
    assert own["installed_game"] == ENTITY_INSTALLED_GAME
    # The agent has not reported: its default entity IDs
    assert agent["library"] == "sensor.steam_pc_library"
    assert agent["library_free"] == "sensor.steam_pc_library_free"
    assert agent["restart_button"] == "button.steam_pc_restart"
    assert agent["lock_button"] == "button.steam_pc_lock"
    assert agent["close_games_button"] == "button.steam_pc_close_games"

    # A renamed entity is answered with its new entity ID
    registry = er.async_get(hass)
    registry.async_update_entity(UPDATES_SENSOR_ENTITY_ID, new_entity_id="sensor.my_updates")
    await hass.async_block_till_done()
    msg = await _entities(hass_ws_client, hass)
    assert msg["result"]["entities"]["updates"] == "sensor.my_updates"

    # An agent button is found by its unique ID, under whatever entity ID it has
    lock = registry.async_get_or_create("button", "mqtt", "steam_pc_lock")
    registry.async_update_entity(lock.entity_id, new_entity_id="button.my_pc_lock")
    await hass.async_block_till_done()
    msg = await _entities(hass_ws_client, hass)
    assert msg["result"]["agent"]["lock_button"] == "button.my_pc_lock"
    assert msg["result"]["agent"]["restart_button"] == "button.steam_pc_restart"
    close = registry.async_get_or_create("button", "mqtt", "steam_pc_close_games")
    registry.async_update_entity(close.entity_id, new_entity_id="button.my_pc_close_games")
    await hass.async_block_till_done()
    msg = await _entities(hass_ws_client, hass)
    assert msg["result"]["agent"]["close_games_button"] == "button.my_pc_close_games"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_one_card_per_pc(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    """steam_engine/pcs names the PCs (a found PC that was ignored is none); entities answers the PC
    asked for (pc), without one the PC without a name, an unknown PC is refused, and so is a PC that
    is not loaded: never with another PC's entities."""
    MockConfigEntry(domain=DOMAIN, source=SOURCE_IGNORE, unique_id="office", data={}).add_to_hass(hass)
    first = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    first.add_to_hass(hass)
    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    kids = MockConfigEntry(domain=DOMAIN, data={"pc_name": "Kinder-PC"}, options=merged_options({}), unique_id="kinderpc")
    kids.add_to_hass(hass)
    assert await hass.config_entries.async_setup(kids.entry_id)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "steam_engine/pcs"})
    msg = await client.receive_json()
    assert msg["result"] == {"pcs": [{"pc": ""}, {"pc": "Kinder-PC"}]}

    await client.send_json_auto_id({"type": "steam_engine/entities"})
    msg = await client.receive_json()
    assert msg["result"]["entities"]["pc"] == ENTITY_PC
    assert msg["result"]["agent"]["library"] == "sensor.steam_pc_library"

    await client.send_json_auto_id({"type": "steam_engine/entities", "pc": "kinder-pc"})
    msg = await client.receive_json()
    assert msg["result"]["entities"]["pc"] != ENTITY_PC
    assert msg["result"]["agent"]["library"] != "sensor.steam_pc_library"

    await client.send_json_auto_id({"type": "steam_engine/entities", "pc": "Büro"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "unknown_pc"

    # The PC without a name not loaded (a reload, disabled): a card without pc is refused, not
    # answered with the Kinder-PC
    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    await client.send_json_auto_id({"type": "steam_engine/entities"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_loaded"
    await client.send_json_auto_id({"type": "steam_engine/entities", "pc": "Kinder-PC"})
    msg = await client.receive_json()
    assert msg["success"]

    assert await hass.config_entries.async_unload(kids.entry_id)
    await hass.async_block_till_done()


async def test_error_without_entry(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    msg = await _entities(hass_ws_client, hass)
    assert not msg["success"]
    assert msg["error"]["code"] == "not_loaded"


async def test_images(hass: HomeAssistant, hass_ws_client: WebSocketGenerator, hass_storage: dict) -> None:
    header = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/10/abc/header.jpg"
    capsule = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/10/abc/capsule_231x87.jpg"
    fetched = dt_util.utcnow().isoformat()
    hass_storage[IMAGES_STORAGE_KEY] = {
        "version": IMAGES_STORAGE_VERSION,
        "minor_version": 1,
        "key": IMAGES_STORAGE_KEY,
        "data": {
            "apps": {
                "10": {"header": header, "capsule": capsule, "fetched": fetched},
                "20": {"failed": True, "fetched": fetched},
            }
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "steam_engine/images"})
    msg = await client.receive_json()
    assert msg["success"]
    # Only the resolved games, without the time of the answer
    assert msg["result"] == {"images": {"10": {"header": header, "capsule": capsule}}}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_images_error_without_entry(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "steam_engine/images"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_loaded"


async def test_subscribe_images(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, hass_storage: dict, store_offline: AsyncMock
) -> None:
    hass_storage[IMAGES_STORAGE_KEY] = {
        "version": IMAGES_STORAGE_VERSION,
        "minor_version": 1,
        "key": IMAGES_STORAGE_KEY,
        "data": {"apps": {"10": {**_store_images("10"), "fetched": dt_util.utcnow().isoformat()}}},
    }

    async def answer(_hass: HomeAssistant, appid: str) -> dict[str, str]:
        return _store_images(appid)

    store_offline.side_effect = answer
    _library(hass, ["10", "30"])
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "steam_engine/subscribe_images"})
    msg = await client.receive_json()
    assert msg["success"]
    sub_id = msg["id"]
    # The current answers right after the result
    msg = await client.receive_json()
    assert msg["id"] == sub_id
    assert msg["type"] == "event"
    assert msg["event"] == {"images": {"10": _store_images("10")}}

    # A new answer (the round after the start delay asks for 30): the whole map again
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=START_DELAY + 1))
    await hass.async_block_till_done(wait_background_tasks=True)
    msg = await client.receive_json()
    assert msg["id"] == sub_id
    assert msg["event"] == {"images": {"10": _store_images("10"), "30": _store_images("30")}}

    # A reload: the subscription stays and gets the answers of the new instance
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    msg = await client.receive_json()
    assert msg["id"] == sub_id
    assert msg["event"] == {"images": {"10": _store_images("10"), "30": _store_images("30")}}

    # Unsubscribed: a further answer is not sent (the next message is the answer to the ping)
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": sub_id})
    msg = await client.receive_json()
    assert msg["success"]
    _library(hass, ["10", "30", "40"])
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=START_DELAY + NEW_GAME_DELAY + 2))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.images.get("40") == _store_images("40")
    await client.send_json_auto_id({"type": "ping"})
    msg = await client.receive_json()
    assert msg["type"] == "pong"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_subscribe_images_unloaded(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "steam_engine/subscribe_images"})
    msg = await client.receive_json()
    assert msg["success"]
    sub_id = msg["id"]
    msg = await client.receive_json()
    assert msg["event"] == {"images": {}}

    # Unloaded: nothing more is sent, and a new subscription is refused
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await client.send_json_auto_id({"type": "steam_engine/subscribe_images"})
    msg = await client.receive_json()
    assert msg["id"] != sub_id
    assert not msg["success"]
    assert msg["error"]["code"] == "not_loaded"


async def test_subscribe_images_error_without_entry(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "steam_engine/subscribe_images"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_loaded"
