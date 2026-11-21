"""The check of the Steam Web API key and Steam ID (steam_account.py) against recorded Steam answers."""

from __future__ import annotations

import aiohttp

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse

from custom_components.steam_engine.steam_account import (
    ERR_BOTH,
    ERR_CANNOT_CONNECT,
    ERR_INVALID_KEY,
    ERR_NOT_FOUND,
    ERR_PRIVATE,
    RESOLVE_URL,
    async_check_account,
)
from custom_components.steam_engine.steam_api import OWNED_URL

STEAM_ID = "76561198000000001"
OWNED_OK = {"response": {"game_count": 2, "games": [{"appid": 10}, {"appid": 20}]}}


async def test_both_empty_is_fine(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    assert await async_check_account(hass, "", "") == ("", None)
    assert aioclient_mock.call_count == 0


async def test_only_one_of_both(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    assert (await async_check_account(hass, "KEY", ""))[1] == ERR_BOTH
    assert (await async_check_account(hass, "", STEAM_ID))[1] == ERR_BOTH
    assert aioclient_mock.call_count == 0


async def test_steam_id64(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(OWNED_URL, json=OWNED_OK)
    assert await async_check_account(hass, " KEY ", f" {STEAM_ID} ") == (STEAM_ID, None)
    assert aioclient_mock.call_count == 1


async def test_profile_url(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(OWNED_URL, json=OWNED_OK)
    url = f"https://steamcommunity.com/profiles/{STEAM_ID}/"
    assert await async_check_account(hass, "KEY", url) == (STEAM_ID, None)


async def test_profile_url_with_too_many_digits(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    url = f"https://steamcommunity.com/profiles/{STEAM_ID}99/"
    assert (await async_check_account(hass, "KEY", url))[1] == ERR_NOT_FOUND


async def test_custom_name_and_url(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(RESOLVE_URL, json={"response": {"success": 1, "steamid": STEAM_ID}})
    aioclient_mock.get(OWNED_URL, json=OWNED_OK)
    assert await async_check_account(hass, "KEY", "somename") == (STEAM_ID, None)
    assert await async_check_account(hass, "KEY", "https://steamcommunity.com/id/somename/") == (STEAM_ID, None)
    resolved = [call[1] for call in aioclient_mock.mock_calls if str(call[1]).startswith(RESOLVE_URL)]
    assert [url.query["vanityurl"] for url in resolved] == ["somename", "somename"]


async def test_custom_name_not_found(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(RESOLVE_URL, json={"response": {"success": 42, "message": "No match"}})
    assert await async_check_account(hass, "KEY", "nobody") == ("nobody", ERR_NOT_FOUND)


async def test_custom_name_while_steam_fails(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(RESOLVE_URL, status=503)
    assert await async_check_account(hass, "KEY", "somename") == ("somename", ERR_CANNOT_CONNECT)


async def test_invalid_key(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(OWNED_URL, status=403)
    assert await async_check_account(hass, "BAD", STEAM_ID) == (STEAM_ID, ERR_INVALID_KEY)


async def test_private_profile(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(OWNED_URL, json={"response": {}})
    assert await async_check_account(hass, "KEY", STEAM_ID) == (STEAM_ID, ERR_PRIVATE)
    assert aioclient_mock.call_count == 2  # asked once more


async def test_one_empty_answer_is_asked_again(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    # Steam answers a public profile now and then with an empty response: that alone is no error
    answers = [{"response": {}}, OWNED_OK]

    async def _owned(method: str, url: object, data: object) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(method, url, json=answers.pop(0))

    aioclient_mock.get(OWNED_URL, side_effect=_owned)
    assert await async_check_account(hass, "KEY", STEAM_ID) == (STEAM_ID, None)
    assert aioclient_mock.call_count == 2


async def test_steam_unreachable_keeps_steam_id64(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(OWNED_URL, exc=aiohttp.ClientError())
    assert await async_check_account(hass, "KEY", STEAM_ID) == (STEAM_ID, None)


async def test_garbage(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    assert (await async_check_account(hass, "KEY", "not a steam id!"))[1] == ERR_NOT_FOUND
    assert aioclient_mock.call_count == 0
