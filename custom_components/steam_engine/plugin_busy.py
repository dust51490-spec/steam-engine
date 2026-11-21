"""PC busy by a plugin.

Docking point for plugins (e.g. a backup script): every entity with the label "PC busy" (label ID
pc_busy) blocks shutting down while it is on/1/true. Automations and scripts only count while they
run (attribute current > 0), because an enabled automation is always on (a plugin can be a
blueprint automation). The label is given in the HA UI, the core knows no foreign
entity IDs. The labelled entities are found again whenever the entity registry changes labels,
so newly labelled entities count at once, without a reload.
"""

from __future__ import annotations

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .pc import UNNAMED, PcNames
from .parts import Part


class PluginBusy(Part):
    """Which labelled entities keep the PC busy right now."""

    def __init__(self, hass: HomeAssistant, names: PcNames = UNNAMED) -> None:
        super().__init__(hass)
        self.labels = names.busy_labels
        self.busy_by: list[str] = []  # entity IDs
        self._labelled: frozenset[str] = frozenset()
        self._unsub_states: CALLBACK_TYPE | None = None

    @property
    def busy(self) -> bool:
        return bool(self.busy_by)

    def names(self) -> list[str]:
        return [state.name if (state := self.hass.states.get(e)) else e for e in self.busy_by]

    @callback
    def async_start(self) -> None:
        self._unsubs.append(self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry))
        self._refresh_labelled()
        self._evaluate()

    @callback
    def async_stop(self) -> None:
        if self._unsub_states is not None:
            self._unsub_states()
            self._unsub_states = None
        super().async_stop()

    @callback
    def _on_registry(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] == "update" and not {"labels", "entity_id"} & set(data["changes"]):
            return
        self._refresh_labelled()
        self._evaluate()

    def _refresh_labelled(self) -> None:
        registry = er.async_get(self.hass)
        labelled = frozenset(
            entry.entity_id for label in self.labels for entry in er.async_entries_for_label(registry, label)
        )
        if labelled == self._labelled:
            return
        self._labelled = labelled
        if self._unsub_states is not None:
            self._unsub_states()
            self._unsub_states = None
        if labelled:
            self._unsub_states = async_track_state_change_event(self.hass, list(labelled), self._on_state_change)

    def _is_busy(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        if entity_id.split(".", 1)[0] in ("automation", "script"):
            try:
                return int(state.attributes.get("current") or 0) > 0
            except (TypeError, ValueError):
                return False
        return state.state.lower() in ("on", "1", "true")

    @callback
    def _on_state_change(self, _event: Event) -> None:
        self._evaluate()

    @callback
    def _evaluate(self) -> None:
        busy_by = sorted(e for e in self._labelled if self._is_busy(e))
        if busy_by == self.busy_by:
            return
        self.busy_by = busy_by
        self._notify_listeners()
