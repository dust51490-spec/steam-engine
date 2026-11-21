"""WebSocket commands for the frontend (the dashboard card).

steam_engine/entities: the current entity IDs of this integration's entities and of the agent's.
Entity IDs can be renamed by the user, so a card cannot rely on the default ones; this answers
from the entity registry by unique ID (the same lookup the logic uses). Keys of "entities" are the
keys of the unique IDs (<entry_id>_<key>, frozen), keys of "agent" the fields of AgentEntities plus
the keys of CARD_AGENT_ENTITIES (library_free, lock_button, close_games_button).
An agent entity that is not in the registry yet is answered with its default entity ID. The states
themselves the card reads from hass.states as usual.

steam_engine/images: the image URLs the Steam store gave for the games the integration knows
(steam_images.py), {"images": {"<appid>": {"header": "<url>", "capsule": "<url>"}}}. Only resolved
games are in it; for the others the card keeps the old CDN paths.

steam_engine/pcs: the PCs Steam Engine is set up for, in the order they were set up,
{"pcs": [{"pc": "<name>"}]} ("" for the PC without a name); the card's editor offers them.

steam_engine/game_size: the install size of one game, {"appid": "<appid>", "size_mb": <MB or
null>, "download_mb": <MB or null>}, for a game that is not installed (game_size.py: size on disk
of a first install without DLC, by the rule of the install job's space check). Asked when the card
opens such a game; answered from a cache when it is known, null when the size is unknown or
api.steamcmd.net did not answer (soon, never an error for that).

Every command but pcs takes "pc" as the actions do (pc.py, pick_entry): one card per PC.

steam_engine/subscribe_images: the same map as a subscription, for the card. It sends the map as an
event message right after the result and again whenever answers were added or changed, or a reload
of the integration started a new instance (SIGNAL_IMAGES_UPDATED). A subscription command needs no
admin, unlike subscribing to the event steam_engine_images_updated, which Home Assistant allows only
administrators; the event is still fired, for automations. The subscription ends when the client
unsubscribes or the connection closes; while the integration is not loaded it sends nothing.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .agent_entities import PLATFORM as AGENT_PLATFORM
from .const import DOMAIN
from .game_size import async_game_sizes
from .pc import CARD_AGENT_ENTITIES, entry_pc_names, pick_entry
from .steam_images import images_signal

# Agent entities the card reads besides those of AgentEntities (the logic does not need them): the
# keys of CARD_AGENT_ENTITIES (pc.py)
PC_FIELD = {vol.Optional("pc"): str}
# The error messages of _entry, by code (pick_entry)
ENTRY_ERRORS = {
    "not_loaded": "Steam Engine is not loaded for this PC",
    "pc_needed": "Say which PC (pc)",
    "unknown_pc": "Unknown PC",
}


def _entry(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> Any:
    """The loaded entry of the PC in the message ("pc", as in the actions), or None after an error
    message; while that PC's entry is not loaded never another PC's."""
    entry, problem = pick_entry(hass.config_entries.async_entries(DOMAIN, include_ignore=False), msg.get("pc"))
    if entry is None:
        connection.send_error(msg["id"], problem, ENTRY_ERRORS[problem])
    return entry


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_entities)
    websocket_api.async_register_command(hass, ws_images)
    websocket_api.async_register_command(hass, ws_subscribe_images)
    websocket_api.async_register_command(hass, ws_pcs)
    websocket_api.async_register_command(hass, ws_game_size)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/pcs"})
@callback
def ws_pcs(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entries = hass.config_entries.async_entries(DOMAIN, include_ignore=False)  # an ignored found PC is none
    connection.send_result(msg["id"], {"pcs": [{"pc": entry_pc_names(e.data).pc_name} for e in entries]})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/entities", **PC_FIELD})
@callback
def ws_entities(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entry = _entry(hass, connection, msg)
    if entry is None:
        return
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    own = {
        item.unique_id.removeprefix(prefix): item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.unique_id.startswith(prefix)
    }
    agent = asdict(entry.runtime_data.agent)
    for key in CARD_AGENT_ENTITIES:
        unique_id, default_entity_id = entry.runtime_data.names.agent_entity(key)
        domain = default_entity_id.split(".", 1)[0]
        agent[key] = registry.async_get_entity_id(domain, AGENT_PLATFORM, unique_id) or default_entity_id
    connection.send_result(msg["id"], {"entities": own, "agent": agent})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/images", **PC_FIELD})
@callback
def ws_images(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entry = _entry(hass, connection, msg)
    if entry is None:
        return
    connection.send_result(msg["id"], {"images": entry.runtime_data.images.resolved()})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe_images", **PC_FIELD})
@callback
def ws_subscribe_images(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    entry = _entry(hass, connection, msg)
    if entry is None:
        return

    @callback
    def forward(images: dict[str, dict[str, str]]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"images": images}))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, images_signal(entry.runtime_data.names), forward)
    connection.send_result(msg["id"])
    forward(entry.runtime_data.images.resolved())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/game_size",
        vol.Required("appid"): vol.All(vol.Coerce(str), vol.Match(r"^[0-9]{1,10}$")),
        **PC_FIELD,
    }
)
@websocket_api.async_response
async def ws_game_size(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if _entry(hass, connection, msg) is None:
        return
    sizes = await async_game_sizes(hass)
    connection.send_result(msg["id"], await sizes.async_get(msg["appid"]))
