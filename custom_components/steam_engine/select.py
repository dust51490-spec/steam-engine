"""Selects that pick a game: one not installed (to install) and one installed (to uninstall or
start, and for its automatic updates).

The buttons in button.py start the job, switch.py switches the automatic updates. The options are the game names sorted by name; a name that
two games share gets its app ID added.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .agent_entities import AgentEntities, library_games
from .const import ENTITY_GAME_TO_INSTALL, ENTITY_INSTALLED_GAME
from .entity import SteamEngineEntity
from .game_choice import PICK_INSTALL, PICK_INSTALLED, GameChoice
from .steam_owned import SteamOwned


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    async_add_entities(
        [
            GameToInstallSelect(entry, data.choice, data.owned),
            InstalledGameSelect(entry, data.choice, data.agent),
        ]
    )


class _GameSelect(SteamEngineEntity, SelectEntity):
    def __init__(self, entry: ConfigEntry, choice: GameChoice, key: str, entity_id: str, icon: str) -> None:
        super().__init__(entry, key, entity_id)
        self._choice = choice
        self._key = key
        self._attr_icon = icon
        self._attr_options = []
        self._attr_current_option = None
        self._appids: dict[str, str] = {}  # option -> app ID

    def _games(self) -> list[Any]:
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._choice.async_add_listener(self._refresh))
        self._refresh()

    @callback
    def _refresh(self) -> None:
        games = [g for g in self._games() if isinstance(g, dict) and str(g.get("appid") or "").isdigit()]
        games.sort(key=lambda g: str(g.get("name") or "").casefold())
        names = [str(g.get("name") or g["appid"]) for g in games]
        counts = Counter(names)
        self._appids = {}
        for g, name in zip(games, names):
            label = name if counts[name] == 1 else f"{name} ({g['appid']})"
            if label in self._appids:  # only if a name looks like "<other name> (<its app ID>)"
                label = f"{label} ({g['appid']})"
            self._appids[label] = str(g["appid"])
        picked = self._choice.get(self._key)
        if picked is not None and picked not in self._appids.values():
            # The game left the list (installed or uninstalled meanwhile): nothing picked
            self._choice.set(self._key, None)
            return  # the listener calls _refresh again
        self._attr_options = list(self._appids)
        self._attr_current_option = next((o for o, a in self._appids.items() if a == picked), None)
        self._write_if_changed((self._attr_options, self._attr_current_option))

    async def async_select_option(self, option: str) -> None:
        self._choice.set(self._key, self._appids[option])


class GameToInstallSelect(_GameSelect):
    """Owned games that are not installed (sensor not_installed)."""

    def __init__(self, entry: ConfigEntry, choice: GameChoice, owned: SteamOwned) -> None:
        super().__init__(entry, choice, PICK_INSTALL, ENTITY_GAME_TO_INSTALL, "mdi:tray-arrow-down")
        self._owned = owned

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._owned.async_add_listener(self._refresh))
        await super().async_added_to_hass()

    def _games(self) -> list[Any]:
        return list(self._owned.missing)


class InstalledGameSelect(_GameSelect):
    """Installed games (the agent's library sensor)."""

    def __init__(self, entry: ConfigEntry, choice: GameChoice, agent: AgentEntities) -> None:
        super().__init__(entry, choice, PICK_INSTALLED, ENTITY_INSTALLED_GAME, "mdi:gamepad-variant-outline")
        self._agent = agent

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_track_state_change_event(self.hass, [self._agent.library], self._on_library))
        await super().async_added_to_hass()

    @callback
    def _on_library(self, event: Event[EventStateChangedData]) -> None:
        self._refresh()

    def _games(self) -> list[Any]:
        # Games on a drive that is not there (unavailable) cannot be uninstalled now
        return [g for g in library_games(self.hass, self._agent) or [] if not g.get("unavailable")]
