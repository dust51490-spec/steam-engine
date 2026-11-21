"""Binary sensors: PC safe to shut down, Steam really downloads, PC busy by a plugin."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoredExtraData, RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    ENTITY_PLUGIN_BUSY,
    ENTITY_STEAM_BUSY,
    KEY_PLUGIN_BUSY,
    KEY_SAFE_TO_SHUT_DOWN,
    KEY_STEAM_BUSY,
    SAFE_SENSOR_ENTITY_ID,
)
from .entity import SteamEngineEntity
from .pc_power import PcPower
from .plugin_busy import PluginBusy
from .steam_progress import SteamProgress


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    async_add_entities(
        [
            SafeToShutDownSensor(entry, data.power),
            SteamBusySensor(entry, data.progress),
            PluginBusySensor(entry, data.plugin_busy),
        ]
    )


class SafeToShutDownSensor(SteamEngineEntity, BinarySensorEntity, RestoreEntity):
    """On when the PC is idle long enough and nothing keeps it busy.

    Attributes safe_since, and off_at: when an automatically woken PC will shut down (for a countdown).
    Keeps over a restart which idle time the idle prompt was sent for and when the last game ended
    (not shown).
    """

    _attr_icon = "mdi:power-sleep"

    def __init__(self, entry: ConfigEntry, power: PcPower) -> None:
        super().__init__(entry, KEY_SAFE_TO_SHUT_DOWN, SAFE_SENSOR_ENTITY_ID)
        self._power = power

    async def async_added_to_hass(self) -> None:
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            self._power.restore_prompted_input(extra.as_dict().get("idle_prompt_for"))
            self._power.restore_game_ended_at(extra.as_dict().get("game_ended"))
        self.async_on_remove(self._power.async_add_listener(self._on_update))
        self._on_update()

    @property
    def is_on(self) -> bool:
        return self._power.safe

    @property
    def extra_restore_state_data(self) -> RestoredExtraData:
        prompted = self._power.prompted_input
        ended = self._power.game_ended_at
        return RestoredExtraData(
            {
                "idle_prompt_for": prompted.isoformat() if prompted else None,
                "game_ended": ended.isoformat() if ended else None,
            }
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "safe_since": self._power.safe_since.isoformat() if self._power.safe_since else None,
            "off_at": self._power.auto_off_at.isoformat() if self._power.auto_off_at else None,
        }

    @callback
    def _on_update(self) -> None:
        self._write_if_changed((self._power.safe, self._power.safe_since, self._power.auto_off_at))


class SteamBusySensor(SteamEngineEntity, BinarySensorEntity, RestoreEntity):
    """On while the PC is on and our job runs with progress in the last 6 h (also between two
    of its games, protocol.job_active), while a job is handed to the agent, or while the Steam
    client itself downloads (agent). Keeps the PC from shutting down.

    Attribute last_progress: last real progress of our job in any phase.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, entry: ConfigEntry, progress: SteamProgress) -> None:
        super().__init__(entry, KEY_STEAM_BUSY, ENTITY_STEAM_BUSY)
        self._progress = progress

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        restored = last.attributes.get("last_progress") if last else None
        if restored:
            self._progress.restore(dt_util.parse_datetime(str(restored)))
        self.async_on_remove(self._progress.async_add_listener(self._on_update))
        self._on_update()

    @property
    def is_on(self) -> bool:
        return self._progress.busy

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._progress.attributes

    @callback
    def _on_update(self) -> None:
        self._write_if_changed((self._progress.busy, self._progress.last_progress))


class PluginBusySensor(SteamEngineEntity, BinarySensorEntity):
    """On while an entity with the label "PC busy" is busy (plugin_busy.py). Blocks shutting down.

    Attribute busy_by: names of the busy entities.
    """

    _attr_icon = "mdi:puzzle-check-outline"

    def __init__(self, entry: ConfigEntry, plugin_busy: PluginBusy) -> None:
        super().__init__(entry, KEY_PLUGIN_BUSY, ENTITY_PLUGIN_BUSY)
        self._plugin_busy = plugin_busy

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._plugin_busy.async_add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._plugin_busy.busy

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"busy_by": self._plugin_busy.names()}
