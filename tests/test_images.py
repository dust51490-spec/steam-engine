"""The game images (steam_images.py): the answer of the Steam store, the requests for the games the
integration knows, the stored answers (no request for a known game, failures remembered, old answers
asked again), a new game, a store that refuses, and the picture of the current game.

The store answers through the autouse mock store_offline (conftest.py), here with a function per
test; the request itself is tested once with aioclient_mock. The pause between two requests is 0.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.steam_engine import steam_images
from custom_components.steam_engine.const import DOMAIN, ENTITY_CURRENT_GAME, merged_options
from custom_components.steam_engine.sensor import GAME_PICTURE_URL
from custom_components.steam_engine.steam_images import (
    APPDETAILS_URL,
    EVENT_IMAGES_UPDATED,
    FLUSH_EVERY,
    NEW_GAME_DELAY,
    START_DELAY,
    STORAGE_KEY,
    STORAGE_VERSION,
    StoreUnavailable,
    parse_appdetails,
)

# Taken before the autouse fixture replaces it
REAL_GET_IMAGES = steam_images.async_get_images

LIBRARY = "sensor.steam_pc_library"
GAME_RUNNING = "binary_sensor.steam_pc_game_running"
CDN = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps"


def _images(appid: str) -> dict[str, str]:
    return {
        "header": f"{CDN}/{appid}/abc/header.jpg?t=1",
        "capsule": f"{CDN}/{appid}/abc/capsule_231x87.jpg?t=1",
    }


def _answer(appid: str) -> dict[str, Any]:
    images = _images(appid)
    return {
        appid: {
            "success": True,
            "data": {"name": "Game", "header_image": images["header"], "capsule_image": images["capsule"]},
        }
    }


@pytest.fixture(autouse=True)
def no_pause() -> Generator[None]:
    with patch.object(steam_images, "REQUEST_PAUSE", 0):
        yield


def _store_answers(store_offline: AsyncMock, without_images: set[str] | None = None) -> None:
    """The store answers every game with images, except those in without_images (success false)."""

    async def answer(_hass: HomeAssistant, appid: str) -> dict[str, str] | None:
        return None if appid in (without_images or set()) else _images(appid)

    store_offline.side_effect = answer


def _asked(store_offline: AsyncMock) -> list[str]:
    return [call.args[1] for call in store_offline.call_args_list]


def _stored(hass_storage: dict[str, Any], apps: dict[str, dict[str, Any]]) -> None:
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"apps": apps},
    }


async def _setup(hass: HomeAssistant, appids: list[str]) -> MockConfigEntry:
    hass.states.async_set(LIBRARY, str(len(appids)), {"games": [{"appid": a, "name": f"Game {a}"} for a in appids]})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_parse_appdetails() -> None:
    assert parse_appdetails("10", _answer("10")) == _images("10")
    # Without a capsule the header stands in for it
    answer = _answer("10")
    del answer["10"]["data"]["capsule_image"]
    assert parse_appdetails("10", answer) == {"header": _images("10")["header"], "capsule": _images("10")["header"]}
    # Removed or region locked, no data, an answer for another game, no JSON object
    assert parse_appdetails("10", {"10": {"success": False}}) is None
    assert parse_appdetails("10", {"10": {"success": True, "data": []}}) is None
    assert parse_appdetails("10", _answer("20")) is None
    assert parse_appdetails("10", None) is None
    # Only https URLs of Steam's CDN are taken over
    answer = _answer("10")
    answer["10"]["data"]["header_image"] = "https://example.com/header.jpg"
    assert parse_appdetails("10", answer) is None
    answer["10"]["data"]["header_image"] = "http://shared.akamai.steamstatic.com/header.jpg"
    assert parse_appdetails("10", answer) is None


async def test_request(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(APPDETAILS_URL, params={"appids": "10", "filters": "basic"}, json=_answer("10"))
    aioclient_mock.get(APPDETAILS_URL, params={"appids": "20", "filters": "basic"}, json={"20": {"success": False}})
    aioclient_mock.get(APPDETAILS_URL, params={"appids": "30", "filters": "basic"}, status=429)
    aioclient_mock.get(APPDETAILS_URL, params={"appids": "40", "filters": "basic"}, text="<html>busy</html>")
    # A rejected request (null) or an answer without the game is no answer, not "no images"
    aioclient_mock.get(APPDETAILS_URL, params={"appids": "50", "filters": "basic"}, text="null")
    aioclient_mock.get(APPDETAILS_URL, params={"appids": "60", "filters": "basic"}, json={})
    aioclient_mock.get(APPDETAILS_URL, params={"appids": "70", "filters": "basic"}, json={"70": {}})
    assert await REAL_GET_IMAGES(hass, "10") == _images("10")
    assert await REAL_GET_IMAGES(hass, "20") is None
    for appid in ("30", "40", "50", "60", "70"):
        with pytest.raises(StoreUnavailable):
            await REAL_GET_IMAGES(hass, appid)


async def test_resolves_the_known_games(
    hass: HomeAssistant, hass_storage: dict[str, Any], store_offline: AsyncMock
) -> None:
    _store_answers(store_offline, without_images={"20"})
    entry = await _setup(hass, ["10", "20"])
    events = async_capture_events(hass, EVENT_IMAGES_UPDATED)
    images = entry.runtime_data.images
    # Not at once: Home Assistant is still starting
    assert store_offline.call_count == 0

    await _later(hass, START_DELAY + 1)
    assert sorted(_asked(store_offline)) == ["10", "20"]
    assert images.get("10") == _images("10")
    assert images.get("20") is None
    assert images.resolved() == {"10": _images("10")}
    assert len(events) == 1
    apps = hass_storage[STORAGE_KEY]["data"]["apps"]
    assert apps["10"]["header"] == _images("10")["header"]
    assert apps["20"]["failed"] is True

    # The running game: the image from the store, or the old CDN path while there is none
    hass.states.async_set(GAME_RUNNING, "on", {"appid": "10"})
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_CURRENT_GAME).attributes["entity_picture"] == _images("10")["header"]
    hass.states.async_set(GAME_RUNNING, "on", {"appid": "20"})
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_CURRENT_GAME).attributes["entity_picture"] == GAME_PICTURE_URL.format(appid="20")

    # Nothing new: no further request
    await _later(hass, START_DELAY + NEW_GAME_DELAY + 1)
    assert store_offline.call_count == 2
    await _unload(hass, entry)


async def test_new_game_is_asked_for_soon(hass: HomeAssistant, store_offline: AsyncMock) -> None:
    _store_answers(store_offline)
    entry = await _setup(hass, ["10"])
    await _later(hass, START_DELAY + 1)
    assert _asked(store_offline) == ["10"]

    hass.states.async_set(LIBRARY, "2", {"games": [{"appid": "10"}, {"appid": "30"}]})
    await hass.async_block_till_done()
    assert store_offline.call_count == 1
    await _later(hass, START_DELAY + NEW_GAME_DELAY + 2)
    assert _asked(store_offline) == ["10", "30"]
    assert entry.runtime_data.images.get("30") == _images("30")
    await _unload(hass, entry)


async def test_stored_answers(hass: HomeAssistant, hass_storage: dict[str, Any], store_offline: AsyncMock) -> None:
    now = dt_util.utcnow()
    _stored(
        hass_storage,
        {
            "10": {**_images("10"), "fetched": (now - timedelta(days=2)).isoformat()},  # fresh
            "20": {"failed": True, "fetched": (now - timedelta(days=2)).isoformat()},  # failed lately
            "30": {**_images("30"), "fetched": (now - timedelta(days=40)).isoformat()},  # old
            "40": {"failed": True, "fetched": (now - timedelta(days=8)).isoformat()},  # failed long ago
        },
    )
    _store_answers(store_offline, without_images={"40"})
    hass.states.async_set(GAME_RUNNING, "on", {"appid": "10"})
    entry = await _setup(hass, ["10", "20", "30", "40"])
    images = entry.runtime_data.images
    # Known at once, before any request
    assert images.get("10") == _images("10")
    assert hass.states.get(ENTITY_CURRENT_GAME).attributes["entity_picture"] == _images("10")["header"]

    await _later(hass, START_DELAY + 1)
    # The old answer and the old failure are asked again, the others not
    assert sorted(_asked(store_offline)) == ["30", "40"]
    apps = hass_storage[STORAGE_KEY]["data"]["apps"]
    assert dt_util.parse_datetime(apps["30"]["fetched"]) > now - timedelta(minutes=1)
    assert apps["40"]["failed"] is True
    assert set(images.resolved()) == {"10", "30"}
    await _unload(hass, entry)


async def test_store_refuses(hass: HomeAssistant, store_offline: AsyncMock) -> None:
    entry = await _setup(hass, ["10", "20"])
    await _later(hass, START_DELAY + 1)
    # The round stops at the first refusal and nothing is remembered as failed
    assert store_offline.call_count == 1
    assert entry.runtime_data.images.resolved() == {}

    # A new game does not shorten the wait
    hass.states.async_set(LIBRARY, "3", {"games": [{"appid": "10"}, {"appid": "20"}, {"appid": "30"}]})
    await hass.async_block_till_done()
    await _later(hass, START_DELAY + NEW_GAME_DELAY + 2)
    assert store_offline.call_count == 1

    _store_answers(store_offline)
    await _later(hass, START_DELAY + 6 * 60)
    assert sorted(_asked(store_offline)[1:]) == ["10", "20", "30"]
    assert set(entry.runtime_data.images.resolved()) == {"10", "20", "30"}
    await _unload(hass, entry)


async def test_round_stopped_by_unload_keeps_its_answers(
    hass: HomeAssistant, hass_storage: dict[str, Any], store_offline: AsyncMock
) -> None:
    """Every FLUSH_EVERY answers are saved, failures too; an unload during the round saves the rest."""
    without_images = {str(appid) for appid in range(1, FLUSH_EVERY + 1)}
    appids = [*sorted(without_images, key=int), "100", "200"]

    asking_last = asyncio.Event()

    async def answer(_hass: HomeAssistant, appid: str) -> dict[str, str] | None:
        if appid == "200":
            asking_last.set()
            await asyncio.Event().wait()  # still asking when the unload comes
        return None if appid in without_images else _images(appid)

    store_offline.side_effect = answer
    entry = await _setup(hass, appids)
    events = async_capture_events(hass, EVENT_IMAGES_UPDATED)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=START_DELAY + 1))
    async with asyncio.timeout(5):
        await asking_last.wait()
    await hass.async_block_till_done()
    assert _asked(store_offline) == appids
    apps = hass_storage[STORAGE_KEY]["data"]["apps"]
    assert set(apps) == without_images
    assert all(item["failed"] is True for item in apps.values())
    assert events == []  # nothing to show yet

    await _unload(hass, entry)
    apps = hass_storage[STORAGE_KEY]["data"]["apps"]
    assert set(apps) == {*without_images, "100"}
    assert apps["100"]["header"] == _images("100")["header"]
    assert events == []
