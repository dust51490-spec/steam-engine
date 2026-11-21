"""Checks the Steam Web API key and the Steam account on the settings page.

The Steam ID field takes the SteamID64, a profile URL (/profiles/<id> or /id/<name>) or the custom
profile name; a name is resolved to the SteamID64 with the key. Then one request for the owned
games shows whether the key works and whether the game details of the profile are public.
"""

from __future__ import annotations

import logging
import re

import aiohttp

from homeassistant.core import HomeAssistant

from .steam_api import SteamUnreachable, async_get_json, async_get_owned

_LOGGER = logging.getLogger(__name__)

RESOLVE_URL = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
# Shorter than the background checks: the settings page waits for it
FLOW_TIMEOUT = aiohttp.ClientTimeout(total=10)
STEAM_ID64 = re.compile(r"^7656\d{13}$")
PROFILE_ID = re.compile(r"steamcommunity\.com/profiles/(7656\d{13})(?=[/?#]|$)", re.IGNORECASE)
PROFILE_NAME = re.compile(r"steamcommunity\.com/id/([^/?#]+)", re.IGNORECASE)
CUSTOM_NAME = re.compile(r"^[A-Za-z0-9_-]{2,32}$")

# Error keys (translations under config.error and options.error)
ERR_BOTH = "steam_both"
ERR_INVALID_KEY = "invalid_api_key"
ERR_NOT_FOUND = "steam_id_not_found"
ERR_PRIVATE = "profile_private"
ERR_CANNOT_CONNECT = "cannot_connect"


async def async_check_account(hass: HomeAssistant, api_key: str, steam_id: str) -> tuple[str, str | None]:
    """Return (SteamID64 or the input as given, error key or None). Both empty is fine (the owned
    games are optional). If Steam does not answer, a SteamID64 is taken unchecked, a profile name
    cannot be resolved (cannot_connect)."""
    api_key, steam_id = api_key.strip(), steam_id.strip()
    if not api_key and not steam_id:
        return "", None
    if not api_key or not steam_id:
        return steam_id, ERR_BOTH

    name = None
    if STEAM_ID64.match(steam_id):
        resolved = steam_id
    elif match := PROFILE_ID.search(steam_id):
        resolved = match.group(1)
    elif (match := PROFILE_NAME.search(steam_id)) or CUSTOM_NAME.match(steam_id):
        name = match.group(1) if match else steam_id
        resolved = ""
    else:
        return steam_id, ERR_NOT_FOUND

    try:
        if name is not None:
            status, data = await async_get_json(
                hass, RESOLVE_URL, {"key": api_key, "vanityurl": name, "format": "json"}, FLOW_TIMEOUT
            )
            if status in (401, 403):
                return steam_id, ERR_INVALID_KEY
            if status != 200:  # rate limit or Steam maintenance, not a missing profile
                return steam_id, ERR_CANNOT_CONNECT
            answer = data.get("response") if isinstance(data, dict) else None
            if not isinstance(answer, dict) or answer.get("success") != 1 or not answer.get("steamid"):
                return steam_id, ERR_NOT_FOUND
            resolved = str(answer["steamid"])

        status, answer = await async_get_owned(hass, api_key, resolved, FLOW_TIMEOUT)
        if status == 200 and answer is None:
            # Empty: a private profile, or one of Steam's occasional empty answers; ask once more
            status, answer = await async_get_owned(hass, api_key, resolved, FLOW_TIMEOUT)
    except SteamUnreachable:
        return (resolved, None) if resolved else (steam_id, ERR_CANNOT_CONNECT)
    if status in (401, 403):
        return resolved, ERR_INVALID_KEY
    if status != 200:
        _LOGGER.warning("Steam Web API answered %s for the owned games, settings saved unchecked", status)
        return resolved, None
    if answer is None:
        return resolved, ERR_PRIVATE
    return resolved, None
