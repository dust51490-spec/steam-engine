"""Requests to the Steam Web API, shared by the settings check (steam_account.py) and the owned games
(steam_owned.py)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

OWNED_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"


class SteamUnreachable(Exception):
    """The Steam Web API did not answer."""


async def async_get_json(
    hass: HomeAssistant, url: str, params: dict[str, str], timeout: aiohttp.ClientTimeout
) -> tuple[int, object]:
    """(HTTP status, parsed JSON or None if the status is not 200). Raises SteamUnreachable."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(url, params=params, timeout=timeout) as response:
            if response.status != 200:
                return response.status, None
            return 200, await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        # Only the error type: the text of some errors holds the request URL with the API key
        _LOGGER.warning("Steam Web API not reachable (%s)", type(err).__name__)
        raise SteamUnreachable from err


async def async_get_owned(
    hass: HomeAssistant, api_key: str, steam_id: str, timeout: aiohttp.ClientTimeout, *, appinfo: bool = False
) -> tuple[int, dict[str, Any] | None]:
    """(HTTP status, the "response" object of GetOwnedGames). The object is None for any other status
    than 200 and for an empty answer: that is what a profile whose game details are not public gets,
    and Steam sends it now and then for a public one too. Raises SteamUnreachable."""
    params = {
        "key": api_key,
        "steamid": steam_id,
        "include_played_free_games": "1",  # otherwise played free games are missing
        "format": "json",
    }
    if appinfo:
        params["include_appinfo"] = "1"
    status, data = await async_get_json(hass, OWNED_URL, params, timeout)
    if status != 200:
        return status, None
    answer = data.get("response") if isinstance(data, dict) else None
    return 200, (answer if isinstance(answer, dict) and "game_count" in answer else None)
