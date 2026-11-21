"""Switches: the PC, auto off, wake flag, new purchases and auto update of the picked game."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    ENTITY_AUTO_INSTALL_PURCHASES,
    ENTITY_AUTO_OFF_ENABLED,
    ENTITY_AUTO_UPDATE_PICKED,
    ENTITY_PC,
    ENTITY_WOKEN_BY_AUTOMATION,
    KEY_AUTO_INSTALL_PURCHASES,
    KEY_AUTO_OFF_ENABLED,
    KEY_AUTO_UPDATE_PICKED,
    KEY_PC,
    KEY_WOKEN_BY_AUTOMATION,
)
from .entity import SteamEngineEntity
from .game_choice import PICK_INSTALLED, GameChoice
from .pc_power import PcPower
from .steam_jobs import SteamJobs


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(
        [
            PcSwitch(entry, entry.runtime_data.power),
            StoredSwitch(entry, KEY_AUTO_OFF_ENABLED, ENTITY_AUTO_OFF_ENABLED, "mdi:power-sleep"),
            StoredSwitch(entry, KEY_WOKEN_BY_AUTOMATION, ENTITY_WOKEN_BY_AUTOMATION, "mdi:alarm"),
            StoredSwitch(entry, KEY_AUTO_INSTALL_PURCHASES, ENTITY_AUTO_INSTALL_PURCHASES, "mdi:cart-arrow-down"),
            AutoUpdatePickedSwitch(entry, entry.runtime_data.choice, entry.runtime_data.jobs),
        ]
    )


class PcSwitch(SteamEngineEntity, SwitchEntity):
    """The PC: on while reachable or the agent reports (presence.py). On wakes it (without the wake
    flag; nothing while it is on), off shuts it down unless Steam works or a plugin keeps it busy
    (then a push, and an error that names the reason)."""

    _attr_icon = "mdi:desktop-tower-monitor"

    def __init__(self, entry: ConfigEntry, power: PcPower) -> None:
        super().__init__(entry, KEY_PC, ENTITY_PC)
        self._power = power

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._power.async_add_listener(self._on_update))
        self._on_update()

    @callback
    def _on_update(self) -> None:
        self._attr_is_on = self._power.pc_on()
        self._write_if_changed(self._attr_is_on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if not await self._power.async_pc_turn_on():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_wake_entity")

    async def async_turn_off(self, **kwargs: Any) -> None:
        reason = await self._power.async_pc_turn_off()
        if reason is not None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="pc_stays_on", translation_placeholders={"reason": reason}
            )


class StoredSwitch(SteamEngineEntity, SwitchEntity, RestoreEntity):
    """A plain on/off setting that survives restarts; it starts off on the very first start.

    - auto_install_purchases: games bought on Steam are installed at once (read by steam_owned.py)
    - auto_off_enabled: main switch for auto off, idle prompt and reminder (pc_power.py)
    - woken_by_automation: set by every wake path, allows auto off; pc_power.py resets it
    """

    def __init__(self, entry: ConfigEntry, key: str, entity_id: str, icon: str) -> None:
        super().__init__(entry, key, entity_id)
        self._attr_icon = icon
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state == STATE_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class AutoUpdatePickedSwitch(SteamEngineEntity, SwitchEntity):
    """Automatic updates of the game picked in installed_game (select.py): on while that game is
    in the auto update list of steam_jobs.py, off when none is picked. Switching it changes the
    list, as the action steam_engine.set_auto_update does, and starts the pick's time again."""

    _attr_icon = "mdi:update"

    def __init__(self, entry: ConfigEntry, choice: GameChoice, jobs: SteamJobs) -> None:
        super().__init__(entry, KEY_AUTO_UPDATE_PICKED, ENTITY_AUTO_UPDATE_PICKED)
        self._choice = choice
        self._jobs = jobs
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._choice.async_add_listener(self._on_update))
        self.async_on_remove(self._jobs.async_add_listener(self._on_update))
        self._on_update()

    @callback
    def _on_update(self) -> None:
        picked = self._choice.get(PICK_INSTALLED)
        self._attr_is_on = picked is not None and picked in self._jobs.auto_update
        self._write_if_changed(self._attr_is_on)

    async def _set(self, enabled: bool) -> None:
        picked = self._choice.get(PICK_INSTALLED)
        if picked is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_game_picked")
        self._choice.set(PICK_INSTALLED, picked)
        await self._jobs.async_set_auto_update(picked, enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
