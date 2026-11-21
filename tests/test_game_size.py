"""The install size of a game that is not installed (game_size.py): the rule of the install job's
space check on recorded answers of api.steamcmd.net (tests/fixtures, trimmed to the keys the rule
reads), and the WebSocket command steam_engine/game_size with its cache, its store and "unknown"
for errors."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.steam_engine.const import DOMAIN, merged_options
from custom_components.steam_engine.game_size import (
    SAVE_DELAY,
    STORAGE_KEY,
    STORAGE_VERSION,
    GameSizes,
    install_size,
    size_answer,
)
from custom_components.steam_engine.steam_updates import BUILD_URL

FIXTURES = Path(__file__).parent / "fixtures"


def _answer(appid: str) -> dict[str, Any]:
    """The recorded answer of api.steamcmd.net for the game."""
    return json.loads((FIXTURES / f"steamcmd_{appid}.json").read_text(encoding="utf-8"))


def _app(appid: str) -> dict[str, Any]:
    return _answer(appid)["data"][appid]


@pytest.fixture(autouse=True)
def no_pause() -> Any:
    """No pause between two requests in the tests (REQUEST_PAUSE is for api.steamcmd.net)."""
    with patch("custom_components.steam_engine.game_size.REQUEST_PAUSE", 0):
        yield


# --- the rule -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("appid", "size", "download"),
    [
        # Treasure Planet: Windows depot + English depot (the install job logged "game 0,7 GB" and
        # Steam "0,73 GB to write"); its old manifests give downloads larger than the game: unknown
        ("331970", 552401426 + 232763216, None),
        # Tabletop Simulator: the Windows depot only (not macOS, not Linux); install.log: "game 2,7 GB",
        # Steam "1,15 GB to download, 2,68 GB to write"
        ("286160", 2875813774, 1234074496),
        # Team Fortress 2: depots without config count, low violence and other languages do not
        ("440", 612239541 + 825745 + 32679290540, 418219920 + 43168 + 16864821072),
        # Battlefield 2042: the main depot, English, and the depots from other apps with a manifest
        ("1517290", 109017762954 + 1122250331 + 254018335 + 360861921, 103086860688 + 1012331408 + 253708064 + 334463536),
    ],
)
def test_install_size_of_recorded_answers(appid: str, size: int, download: int | None) -> None:
    assert install_size(_app(appid)) == (size, download)


def test_install_size_in_mb() -> None:
    assert size_answer("286160", install_size(_app("286160"))) == {"appid": "286160", "size_mb": 2743, "download_mb": 1177}
    assert size_answer("331970", install_size(_app("331970"))) == {"appid": "331970", "size_mb": 749, "download_mb": None}
    assert size_answer("1", None) == {"appid": "1", "size_mb": None, "download_mb": None}


def test_install_size_rules() -> None:
    def depot(size: str | None, download: str | None = None, **extra: Any) -> dict[str, Any]:
        public = {k: v for k, v in (("size", size), ("download", download)) if v is not None}
        return {"manifests": {"public": public}, **extra}

    app = {
        "depots": {
            "1": depot("100", "50"),
            # DLC: not counted (a first install without DLC)
            "2": depot("1000", "500", dlcappid="9"),
            # Windows among several systems, written in another case: counted
            "3": depot("10", "5", config={"oslist": "macos,Windows"}),
            "4": depot("1000", config={"oslist": "linux"}),
            "5": depot("20", "10", config={"language": "English"}),
            "6": depot("1000", config={"language": "german"}),
            "7": depot("1000", config={"lowviolence": "1"}),
            # No public manifest (a beta only, a shared redistributable): nothing
            "8": {"manifests": {"beta": {"size": "1000"}}},
            "9": {"config": {"oslist": "windows"}, "depotfromapp": "228980", "sharedinstall": "1"},
            # Not a depot
            "branches": {"public": {"buildid": "1", "size": "1000"}},
            "baselanguages": "english,german",
        }
    }
    assert install_size(app) == (130, 65)
    # A counted depot without a download: the download is unknown, the size is not
    app["depots"]["10"] = depot("5")
    assert install_size(app) == (135, None)
    # Numbers as JSON numbers are taken too; not digits are not
    assert install_size({"depots": {"1": {"manifests": {"public": {"size": 7, "download": 3}}}}}) == (7, 3)
    assert install_size({"depots": {"1": {"manifests": {"public": {"size": "-7"}}}}}) is None
    assert install_size({"depots": {"1": {"manifests": {"public": {"size": True}}}}}) is None
    # No depot with a size, no depots, no app
    assert install_size({"depots": {"branches": {"public": {"buildid": "1"}}}}) is None
    assert install_size({"depots": "none"}) is None
    assert install_size({}) is None
    assert install_size(None) is None


# --- the WebSocket command ----------------------------------------------------------------------


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _ask(client: Any, appid: Any, **extra: Any) -> dict[str, Any]:
    await client.send_json_auto_id({"type": "steam_engine/game_size", "appid": appid, **extra})
    return await client.receive_json()


async def test_game_size_asked_once_and_kept(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, aioclient_mock: AiohttpClientMocker, hass_storage: dict
) -> None:
    aioclient_mock.get(BUILD_URL.format(appid="286160"), json=_answer("286160"))
    entry = await _setup(hass)
    client = await hass_ws_client(hass)

    msg = await _ask(client, "286160")
    assert msg["success"]
    assert msg["result"] == {"appid": "286160", "size_mb": 2743, "download_mb": 1177}
    assert aioclient_mock.call_count == 1

    # Opened again (also with the app ID as a number): from the cache, no second request
    msg = await _ask(client, 286160)
    assert msg["result"] == {"appid": "286160", "size_mb": 2743, "download_mb": 1177}
    assert aioclient_mock.call_count == 1

    # Saved for the next start, in bytes with the time of the answer
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SAVE_DELAY + 1))
    await hass.async_block_till_done()
    saved = hass_storage[STORAGE_KEY]["data"]["apps"]["286160"]
    assert [saved["size"], saved["download"]] == [2875813774, 1234074496]
    assert dt_util.parse_datetime(saved["fetched"]) is not None

    # A reload keeps the cache
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    msg = await _ask(client, "286160")
    assert msg["result"]["size_mb"] == 2743
    assert aioclient_mock.call_count == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_game_size_from_the_store(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, aioclient_mock: AiohttpClientMocker, hass_storage: dict
) -> None:
    now = dt_util.utcnow()
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "apps": {
                "331970": {"size": 785164642, "download": None, "fetched": (now - timedelta(days=6)).isoformat()},
                # Older than a week: asked again
                "286160": {"size": 1, "download": None, "fetched": (now - timedelta(days=8)).isoformat()},
                # The API knew no size: asked again after a day
                "440": {"size": None, "download": None, "fetched": (now - timedelta(days=2)).isoformat()},
            }
        },
    }
    aioclient_mock.get(BUILD_URL.format(appid="286160"), json=_answer("286160"))
    aioclient_mock.get(BUILD_URL.format(appid="440"), json=_answer("440"))
    entry = await _setup(hass)
    client = await hass_ws_client(hass)

    msg = await _ask(client, "331970")
    assert msg["result"] == {"appid": "331970", "size_mb": 749, "download_mb": None}
    assert aioclient_mock.call_count == 0

    msg = await _ask(client, "286160")
    assert msg["result"]["size_mb"] == 2743
    msg = await _ask(client, "440")
    assert msg["result"]["size_mb"] == 31750
    assert aioclient_mock.call_count == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "answer",
    [
        {"exc": TimeoutError()},
        {"status": 500},
        {"text": "not json"},
        {"json": {"data": {}, "status": "failed"}},
    ],
)
async def test_game_size_unknown_without_answer(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, aioclient_mock: AiohttpClientMocker, answer: dict
) -> None:
    aioclient_mock.get(BUILD_URL.format(appid="10"), **answer)
    entry = await _setup(hass)
    client = await hass_ws_client(hass)

    msg = await _ask(client, "10")
    assert msg["success"]
    assert msg["result"] == {"appid": "10", "size_mb": None, "download_mb": None}
    # Not asked again at once (a card opened again a moment later)
    msg = await _ask(client, "10")
    assert msg["result"]["size_mb"] is None
    assert aioclient_mock.call_count == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_game_size_without_depot_sizes(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, aioclient_mock: AiohttpClientMocker
) -> None:
    """An answer without depot sizes: unknown, kept like an answer."""
    aioclient_mock.get(
        BUILD_URL.format(appid="20"), json={"data": {"20": {"depots": {"branches": {"public": {"buildid": "5"}}}}}}
    )
    entry = await _setup(hass)
    client = await hass_ws_client(hass)
    msg = await _ask(client, "20")
    assert msg["result"] == {"appid": "20", "size_mb": None, "download_mb": None}
    assert aioclient_mock.call_count == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_game_size_refuses_a_bad_app_id(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    entry = await _setup(hass)
    client = await hass_ws_client(hass)
    msg = await _ask(client, "../1")
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"
    msg = await _ask(client, "1", pc="Büro")
    assert not msg["success"]
    assert msg["error"]["code"] == "unknown_pc"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_game_size_error_without_entry(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_ws_client(hass)
    msg = await _ask(client, "440")
    assert not msg["success"]
    assert msg["error"]["code"] == "not_loaded"


async def test_game_sizes_removed_with_the_last_entry(hass: HomeAssistant, hass_storage: dict) -> None:
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"apps": {"440": {"size": 1, "download": None, "fetched": dt_util.utcnow().isoformat()}}},
    }
    first = await _setup(hass)
    kids = MockConfigEntry(domain=DOMAIN, data={"pc_name": "Kinder-PC"}, options=merged_options({}), unique_id="kinderpc")
    kids.add_to_hass(hass)
    assert await hass.config_entries.async_setup(kids.entry_id)
    await hass.async_block_till_done()

    # Another PC is left: the sizes stay
    assert await hass.config_entries.async_remove(kids.entry_id)
    await hass.async_block_till_done()
    assert STORAGE_KEY in hass_storage
    # The last one: gone
    assert await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()
    assert STORAGE_KEY not in hass_storage


async def test_game_sizes_not_saved_again_after_removal(hass: HomeAssistant, hass_storage: dict) -> None:
    """A fetch still running when the store is removed does not write it again. (A delayed save that
    is still pending is cancelled by Store.async_remove itself, which the test storage replaces.)"""
    release = asyncio.Event()

    async def slow(_session: Any, appid: str, _timeout: Any) -> dict[str, Any]:
        await release.wait()
        return _app(appid)

    sizes = GameSizes(hass)
    with patch("custom_components.steam_engine.game_size.async_get_app_info", AsyncMock(side_effect=slow)):
        asking = hass.async_create_task(sizes.async_get("440"))
        await asyncio.sleep(0)
        await sizes.async_remove()
        release.set()
        assert (await asking)["size_mb"] == 31750
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SAVE_DELAY + 1))
    await hass.async_block_till_done()
    assert STORAGE_KEY not in hass_storage


async def test_one_fetch_for_requests_at_the_same_time(hass: HomeAssistant) -> None:
    """Two cards opening the same game at once share one request; an exception becomes unknown."""
    release = asyncio.Event()

    async def slow(_session: Any, appid: str, _timeout: Any) -> dict[str, Any]:
        await release.wait()
        return _app(appid)

    sizes = GameSizes(hass)
    with patch("custom_components.steam_engine.game_size.async_get_app_info", AsyncMock(side_effect=slow)) as get:
        first = hass.async_create_task(sizes.async_get("440"))
        second = hass.async_create_task(sizes.async_get("440"))
        await asyncio.sleep(0)
        release.set()
        assert await first == await second == {"appid": "440", "size_mb": 31750, "download_mb": 16482}
        assert get.call_count == 1

    with patch(
        "custom_components.steam_engine.game_size.async_get_app_info", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        assert await sizes.async_get("286160") == {"appid": "286160", "size_mb": None, "download_mb": None}
    await hass.async_block_till_done()
