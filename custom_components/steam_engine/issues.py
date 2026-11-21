"""Hints under Settings > Repairs for problems the user has to fix. Each one goes away by itself
once its cause is gone.

- agent_missing: the agent's entities are not there (PC side not set up yet); the integration
  reloads by itself once the agent creates them
- api_key_invalid, profile_private: the owned games check (steam_owned.py) gets no games
- steam_not_logged_on: a game of a job failed because the Steam client did not sign in; gone after a
  game is installed or a job ends well
- agent_not_running: the PC is reachable, but the agent has not reported for AGENT_DOWN_AFTER
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import protocol
from .const import CONF_REACHABLE_ENTITY, DOMAIN, SECTION_PC
from .parts import Part
from .pc import UNNAMED, UNNAMED_PC, PcNames
from .push import several_pcs

if TYPE_CHECKING:
    from .agent_entities import AgentEntities

ISSUE_AGENT_MISSING = "agent_missing"
ISSUE_API_KEY = "api_key_invalid"
ISSUE_PRIVATE = "profile_private"
ISSUE_STEAM_LOGON = "steam_not_logged_on"
ISSUE_AGENT_DOWN = "agent_not_running"
ISSUES = (ISSUE_AGENT_MISSING, ISSUE_API_KEY, ISSUE_PRIVATE, ISSUE_STEAM_LOGON, ISSUE_AGENT_DOWN)

AGENT_DOWN_AFTER = timedelta(minutes=30)
JOB_OK = (protocol.DONE, protocol.INSTALLED)


@callback
def set_issue(
    hass: HomeAssistant,
    issue_id: str,
    active: bool,
    placeholders: dict[str, str] | None = None,
    names: PcNames = UNNAMED,
) -> None:
    """Show or remove a hint (issue_id: one of ISSUES, its translation key), one per PC. While
    there is more than one PC its title starts with the PC's name ({pc}), as push titles do."""
    if active:
        pc = f"{names.pc_name or UNNAMED_PC}: " if several_pcs(hass) else ""
        ir.async_create_issue(
            hass,
            DOMAIN,
            names.issue_id(issue_id),
            is_fixable=False,
            # The others are found again after a restart; a failed job is not
            is_persistent=issue_id == ISSUE_STEAM_LOGON,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue_id,
            translation_placeholders={**(placeholders or {}), "pc": pc},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, names.issue_id(issue_id))


@callback
def delete_all(hass: HomeAssistant, names: PcNames = UNNAMED) -> None:
    for issue_id in ISSUES:
        ir.async_delete_issue(hass, DOMAIN, names.issue_id(issue_id))


class SteamEngineIssues(Part):
    """Watches the job results and whether the agent runs while the PC is reachable."""

    def __init__(
        self, hass: HomeAssistant, options: dict[str, dict], agent: AgentEntities, names: PcNames = UNNAMED
    ) -> None:
        super().__init__(hass)
        self.names = names
        self.agent = agent
        self.reachable: str = options[SECTION_PC].get(CONF_REACHABLE_ENTITY) or ""
        self._agent_timer: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> None:
        self._unsubs.append(async_track_state_change_event(self.hass, [self.agent.install_status], self._on_job))
        if self.reachable:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [self.reachable, self.agent.agent_online], self._on_agent_change
                )
            )
            self._unsubs.append(self._cancel_agent_timer)
            self._check_agent(dt_util.utcnow())
        else:
            set_issue(self.hass, ISSUE_AGENT_DOWN, False, names=self.names)

    @callback
    def _cancel_agent_timer(self) -> None:
        if self._agent_timer is not None:
            self._agent_timer()
            self._agent_timer = None

    @callback
    def _on_agent_change(self, _event: Event[EventStateChangedData]) -> None:
        self._check_agent(dt_util.utcnow())

    @callback
    def _on_job(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is None:
            return
        if new.state in JOB_OK:
            set_issue(self.hass, ISSUE_STEAM_LOGON, False, names=self.names)
        elif new.state == protocol.ERROR and new.attributes.get("reason_code") == protocol.CODE_NOT_LOGGED_ON:
            set_issue(self.hass, ISSUE_STEAM_LOGON, True, names=self.names)

    @callback
    def _check_agent(self, now: datetime) -> None:
        """On every change of the two entities, and once more when AGENT_DOWN_AFTER would be over."""
        self._cancel_agent_timer()
        reachable = self.hass.states.get(self.reachable)
        agent = self.hass.states.get(self.agent.agent_online)
        # No agent entity at all: agent_missing says it already
        if agent is None or agent.state == STATE_ON:
            set_issue(self.hass, ISSUE_AGENT_DOWN, False, names=self.names)
            return
        if agent.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return  # HA's MQTT connection is down: nothing known about the agent
        if reachable is not None and reachable.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        if reachable is not None and reachable.state == STATE_ON:
            # Reachable, the agent off: down once both have been so for AGENT_DOWN_AFTER
            remaining = AGENT_DOWN_AFTER - (now - max(reachable.last_changed, agent.last_changed))
            if remaining <= timedelta(0):
                minutes = str(int(AGENT_DOWN_AFTER.total_seconds() // 60))
                set_issue(self.hass, ISSUE_AGENT_DOWN, True, {"minutes": minutes}, self.names)
            else:
                self._agent_timer = async_call_later(self.hass, remaining, self._check_agent)
        else:
            # PC off: nothing to say about the agent
            set_issue(self.hass, ISSUE_AGENT_DOWN, False, names=self.names)
