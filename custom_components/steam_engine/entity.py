"""What every entity of this integration shares: the device "Steam Engine", the unique ID
"<entry ID>_<key>", the entity ID it is created with (of its PC, pc.py), the translation key (= key), and writing the
state only when it changed."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .const import unique_id

_NOTHING_WRITTEN = object()


class SteamEngineEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key: str, entity_id: str) -> None:
        self._attr_unique_id = unique_id(entry.entry_id, key)
        self._attr_device_info = entry.runtime_data.device
        self._attr_translation_key = key
        self.entity_id = entry.runtime_data.names.own(entity_id)
        self._written: Any = _NOTHING_WRITTEN

    @callback
    def _write_if_changed(self, current: Any) -> None:
        """Write the state when current (what the state is made of) differs from the last write."""
        if current != self._written:
            self._written = current
            self.async_write_ha_state()
