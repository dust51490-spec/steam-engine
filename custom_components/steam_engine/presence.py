"""Whether the PC is on.

On while the optional "PC reachable" sensor of the settings page is on, or while the agent reports:
it is connected to the broker (its last will says when it is gone), or its network sensor has a
value (it turns unavailable 90 s after the agent's last message). So without a reachable sensor the
agent alone tells. While Home Assistant's own MQTT connection is down, all agent entities are
unavailable at once; that says nothing about the PC, so the last value stays. The PC switch shows
this, and the wake flag, the busy sensor and the job queue use it.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .agent_entities import AgentEntities
from .parts import Part

NOT_KNOWN = (STATE_UNKNOWN, STATE_UNAVAILABLE)


class PcPresence(Part):
    """on: the PC is on; since: when that last changed."""

    def __init__(self, hass: HomeAssistant, agent: AgentEntities, reachable_entity: str) -> None:
        super().__init__(hass)
        self.agent = agent
        self.reachable_entity = reachable_entity
        self.since: datetime = dt_util.utcnow()
        self._was_on: bool | None = None

    def _current(self) -> bool | None:
        """On or off by the watched entities; None when they tell nothing (HA's MQTT connection is
        down and no reachable sensor says on or off)."""
        reachable = self.hass.states.get(self.reachable_entity) if self.reachable_entity else None
        if reachable is not None and reachable.state == STATE_ON:
            return True
        online = self.hass.states.get(self.agent.agent_online)
        if online is not None and online.state == STATE_ON:
            return True
        network = self.hass.states.get(self.agent.network)
        if network is not None and network.state not in NOT_KNOWN:
            return True
        reachable_off = reachable is not None and reachable.state == STATE_OFF
        if online is not None and online.state in NOT_KNOWN and not reachable_off:
            return None
        return False

    @property
    def on(self) -> bool:
        current = self._current()
        return bool(self._was_on) if current is None else current

    @property
    def known(self) -> bool:
        """False while the watched entities tell nothing (see _current): on is the last value then."""
        return self._current() is not None

    @callback
    def async_start(self) -> None:
        watched = [self.agent.agent_online, self.agent.network]
        if self.reachable_entity:
            watched.append(self.reachable_entity)
        self._unsubs.append(async_track_state_change_event(self.hass, watched, self._on_change))
        self._was_on = self.on
        # Before the first change: since the reachable sensor or the agent turned on (the PC was on
        # at least since then; the network sensor changes with every report and tells nothing about
        # that), or the last change of all of them
        states = [s for s in (self.hass.states.get(e) for e in watched) if s is not None]
        if self._was_on:
            deciding = [s for s in states if s.entity_id != self.agent.network and s.state == STATE_ON]
            self.since = min(s.last_changed for s in deciding) if deciding else dt_util.utcnow()
        elif states:
            self.since = max(s.last_changed for s in states)

    @callback
    def _on_change(self, _event: Event) -> None:
        on = self.on
        if on == self._was_on:
            return
        self._was_on = on
        self.since = dt_util.utcnow()
        self._notify_listeners()
