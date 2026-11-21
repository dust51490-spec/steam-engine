"""The integration's device "Steam Engine", shown below the agent's device "Steam PC"."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.loader import async_get_loaded_integration

from .const import DOMAIN
from .pc import UNNAMED, PcNames


def steam_engine_device(hass: HomeAssistant, entry: ConfigEntry, names: PcNames = UNNAMED) -> DeviceInfo:
    """Device info for all entities of the integration. The link to "Steam PC" is set only when the
    agent has created that device (a via_device_id of an unknown device is an error in HA), so a
    device the agent creates later is linked on the next start of the integration."""
    info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=names.device_name,
        manufacturer="Steam Engine",
        entry_type=DeviceEntryType.SERVICE,
        sw_version=str(async_get_loaded_integration(hass, DOMAIN).version),
    )
    if agent := dr.async_get(hass).async_get_devices(identifiers={names.agent_device}):
        info["via_device_id"] = agent[0].id
    return info
