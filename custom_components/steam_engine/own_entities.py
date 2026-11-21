"""Entities of this integration, looked up by unique ID.

The logic reads or switches some of its own entities (the auto off switches, the wake flag, new
purchases, the current game for the game events). Their entity IDs can differ from setup to setup (renamed by the user, or generated for
a new one), so they are looked up in the entity registry on every use; a rename reloads the
integration, so the state subscriptions follow too (agent_entities.async_reload_on_rename). Before
the entity exists (first start) the entity ID it is created with is used. The busy sensors and the
PC switch are not read through their states: the parts behind them are passed directly.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    ENTITY_AUTO_INSTALL_PURCHASES,
    ENTITY_AUTO_OFF_ENABLED,
    ENTITY_CURRENT_GAME,
    ENTITY_WOKEN_BY_AUTOMATION,
    KEY_AUTO_INSTALL_PURCHASES,
    KEY_AUTO_OFF_ENABLED,
    KEY_CURRENT_GAME,
    KEY_WOKEN_BY_AUTOMATION,
    unique_id,
)
from .pc import UNNAMED, PcNames


class OwnEntities:
    """Current entity IDs of this integration's entities that the logic reads or switches."""

    def __init__(self, hass: HomeAssistant, entry_id: str, names: PcNames = UNNAMED) -> None:
        self._registry = er.async_get(hass)
        self._entry_id = entry_id
        self._names = names

    def _find(self, domain: str, key: str, fallback: str) -> str:
        found = self._registry.async_get_entity_id(domain, DOMAIN, unique_id(self._entry_id, key))
        return found or self._names.own(fallback)

    @property
    def auto_off_enabled(self) -> str:
        return self._find("switch", KEY_AUTO_OFF_ENABLED, ENTITY_AUTO_OFF_ENABLED)

    @property
    def woken_by_automation(self) -> str:
        return self._find("switch", KEY_WOKEN_BY_AUTOMATION, ENTITY_WOKEN_BY_AUTOMATION)

    @property
    def auto_install_purchases(self) -> str:
        return self._find("switch", KEY_AUTO_INSTALL_PURCHASES, ENTITY_AUTO_INSTALL_PURCHASES)

    @property
    def current_game(self) -> str:
        return self._find("sensor", KEY_CURRENT_GAME, ENTITY_CURRENT_GAME)
