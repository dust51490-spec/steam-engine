"""Entities of the Steam agent and install.ps1 on the PC.

They come by MQTT discovery with fixed unique IDs, but their entity IDs can differ from setup to
setup (renamed by the user, or created before the PC side suggested one). So the integration looks
them up in the entity registry by unique ID. One that is not there yet (agent never ran) falls back
to the default entity ID that discovery gives a new entity (default_entity_id); while one is
missing there is a hint under Repairs, and the integration reloads by itself once the agent has
created them all (first start of the PC side).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later

from .issues import ISSUE_AGENT_MISSING, set_issue
from .pc import AGENT_ENTITIES, UNNAMED, PcNames

_LOGGER = logging.getLogger(__name__)

# The agent creates its entities within seconds of each other; reload once they are all there
RELOAD_DELAY = timedelta(seconds=10)

PLATFORM = "mqtt"

# field: (unique_id, default entity ID) of the unnamed PC (pc.py). The domain is the one of the default entity ID. The default entity ID is the
# default_entity_id of the discovery config; it only applies to a newly created entity, an existing
# one keeps its entity ID. The agent's other discovery entities (free space, restart and lock buttons,
# plugin buttons and busy sensors) follow the same rules; the integration does not read them.
UNIQUE_IDS: dict[str, tuple[str, str]] = {field: UNNAMED.agent_entity(field) for field in AGENT_ENTITIES}


def _unique_ids(names: PcNames) -> dict[str, tuple[str, str]]:
    return {field: names.agent_entity(field) for field in AGENT_ENTITIES}


@dataclass(frozen=True)
class AgentEntities:
    """Entity IDs of the PC side, resolved at setup (a rename reloads the integration, see
    async_reload_on_rename)."""

    library: str
    install_status: str
    history: str
    agent_online: str
    steam_downloading: str
    steam_running: str
    game_running: str
    last_input: str
    network: str
    shutdown_button: str
    restart_button: str


def _lookup(registry: er.EntityRegistry, names: PcNames) -> tuple[dict[str, str], list[str]]:
    """Entity ID per field (the default one when missing) and the default IDs of the missing ones."""
    found: dict[str, str] = {}
    missing: list[str] = []
    for field in fields(AgentEntities):
        unique_id, default_entity_id = names.agent_entity(field.name)
        domain = default_entity_id.split(".", 1)[0]
        entity_id = registry.async_get_entity_id(domain, PLATFORM, unique_id)
        if entity_id is None:
            entity_id = default_entity_id
            missing.append(entity_id)
        found[field.name] = entity_id
    return found, missing


def resolve_agent_entities(hass: HomeAssistant, names: PcNames = UNNAMED) -> tuple[AgentEntities, bool]:
    """The agent's entity IDs, and whether all of them are there."""
    found, missing = _lookup(er.async_get(hass), names)
    if missing:
        _LOGGER.warning(
            "Steam agent entities not found yet (has the agent run on the PC?), using %s; "
            "Steam Engine reloads by itself after the agent's first start",
            ", ".join(missing),
        )
    set_issue(hass, ISSUE_AGENT_MISSING, bool(missing), names=names)
    return AgentEntities(**found), not missing


@callback
def async_reload_when_agent_appears(hass: HomeAssistant, entry_id: str, names: PcNames = UNNAMED) -> CALLBACK_TYPE:
    """While agent entities are missing: reload the integration once the agent has created them all."""
    registry = er.async_get(hass)
    unique_ids = {unique_id for unique_id, _ in _unique_ids(names).values()}
    cancel_timer: CALLBACK_TYPE | None = None

    @callback
    def reload(_now: datetime) -> None:
        nonlocal cancel_timer
        cancel_timer = None
        if not _lookup(registry, names)[1]:
            hass.config_entries.async_schedule_reload(entry_id)

    @callback
    def on_registry(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        nonlocal cancel_timer
        if event.data["action"] != "create":
            return
        created = registry.async_get(event.data["entity_id"])
        if created is None or created.platform != PLATFORM or created.unique_id not in unique_ids:
            return
        if cancel_timer is not None:
            cancel_timer()
        cancel_timer = async_call_later(hass, RELOAD_DELAY, reload)

    unsub = hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, on_registry)

    @callback
    def stop() -> None:
        unsub()
        if cancel_timer is not None:
            cancel_timer()

    return stop


@callback
def async_reload_on_rename(hass: HomeAssistant, entry_id: str, names: PcNames = UNNAMED) -> CALLBACK_TYPE:
    """Reload the integration when an entity it watches gets a new entity ID: one of the agent's
    (of its PC) or one of its own. The state subscriptions are made with the entity IDs at start."""
    registry = er.async_get(hass)
    unique_ids = {unique_id for unique_id, _ in _unique_ids(names).values()}

    @callback
    def on_registry(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] != "update" or "entity_id" not in data["changes"]:
            return
        changed = registry.async_get(data["entity_id"])
        if changed is None:
            return
        if changed.config_entry_id == entry_id or (changed.platform == PLATFORM and changed.unique_id in unique_ids):
            _LOGGER.info("%s was renamed, Steam Engine reloads", data["entity_id"])
            hass.config_entries.async_schedule_reload(entry_id)

    return hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, on_registry)


def library_games(hass: HomeAssistant, agent: AgentEntities) -> list[dict[str, Any]] | None:
    """The installed games from the agent's library sensor: only entries with a numeric app ID.
    None while the library is not known (no state, or no list), [] for a PC without games."""
    state = hass.states.get(agent.library)
    games = state.attributes.get("games") if state is not None else None
    if not isinstance(games, list):
        return None
    return [g for g in games if isinstance(g, dict) and str(g.get("appid") or "").isdigit()]


async def async_send_agent_command(hass: HomeAssistant, payload: str, names: PcNames = UNNAMED) -> None:
    """Send a command to the agent of the PC (PROTOCOL.md, steam/agent/command). Raises when MQTT fails."""
    await hass.services.async_call(
        "mqtt", "publish", {"topic": names.agent_command_topic, "payload": payload}, blocking=True
    )
