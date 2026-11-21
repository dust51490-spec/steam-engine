"""PC power logic: safe to shut down, auto off, idle prompt, reminder, start type and the PC switch.

A PC that the integration woke shuts down again once it has been idle and quiet long enough. A PC
started by hand only gets a push asking whether to shut it down. Steam downloads, a running game and
busy plugins always keep the PC on. The wait times come from the settings page.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfDataRate
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import DataRateConverter

from .agent_entities import AgentEntities
from .const import (
    ACTION_AUTO_OFF_ON,
    ACTION_PC_SHUTDOWN,
    CONF_AFTER_DOWNLOAD_MINUTES,
    CONF_AFTER_GAME_MINUTES,
    CONF_IDLE_MINUTES,
    CONF_LINK,
    CONF_MIN_AFTER_WAKE_MINUTES,
    CONF_NETWORK_LIMIT_KBS,
    CONF_PROMPT_AFTER_MINUTES,
    CONF_QUIET_MINUTES,
    CONF_REMINDER_AFTER_HOURS,
    CONF_REMINDER_EVERY_HOURS,
    CONF_WAKE_ENTITY,
    SECTION_AUTO_OFF,
    SECTION_IDLE_PROMPT,
    PUSH_IDLE,
    PUSH_PROBLEMS,
    PUSH_REMINDER,
    SECTION_NOTIFY,
    SECTION_PC,
    SECTION_REMINDER,
    WAKE_ACTIONS,
)
from .own_entities import OwnEntities
from .parts import Part, wait_until
from .plugin_busy import PluginBusy
from .presence import PcPresence
from .pc import UNNAMED, PcNames
from .push import TAG_AUTO_OFF_REMINDER, TAG_PC_IDLE, TAG_PC_LOCK, send_push
from .steam_progress import SteamProgress
from .texts import Texts

_LOGGER = logging.getLogger(__name__)

TICK = timedelta(seconds=30)
BAD_STATES = (STATE_UNKNOWN, STATE_UNAVAILABLE, "none", "")

# Start type. Safety margins, not settings.
OFF_CONFIRM = timedelta(minutes=2)  # off this long = PC is off
LONG_OFF = timedelta(minutes=3)  # off at least this long before it came on = a real start
WAKE_WINDOW = timedelta(minutes=20)  # a wake press this recent explains the start
MANUAL_MATCH = timedelta(minutes=1)  # a wake entity press this close to the PC switch's was that one
FRESH_INPUT = timedelta(minutes=3)  # only input this fresh (by HA's clock) stops a shutdown when idle
# A last input this much after the logon's (_input_base) is someone at the PC: less can be the PC
# setting its clock right after the start
INPUT_MARGIN = timedelta(seconds=30)
BOOT_PAUSE = 20  # seconds to let a woken PC settle before its first job
GOING_DOWN = timedelta(minutes=3)  # a PC shutting down at a wake call counts as off within this
# A PC still off this long after our press of the wake entity is still starting: no second press (a
# device that presses the power button would switch it off again)
BOOT_WINDOW = timedelta(minutes=3)
# A press of the agent's restart button this recent is a restart until the agent is back after it
RESTART_WINDOW = timedelta(minutes=5)
# States of the PC state sensor (PcPower.phase)
PHASE_OFF = "off"
PHASE_STARTING = "starting"
PHASE_ON = "on"
PHASE_STOPPING = "shutting_down"
PHASE_RESTARTING = "restarting"
PC_PHASES = [PHASE_OFF, PHASE_STARTING, PHASE_ON, PHASE_STOPPING, PHASE_RESTARTING]
REMINDER_MARGIN = timedelta(minutes=5)  # a reminder this much short of the interval counts as due


def _minutes_text(tx: Texts, minutes: int) -> str:
    if minutes == 60:
        return tx("minutes_one_hour")
    if minutes % 60 == 0:
        return tx("minutes_hours", hours=minutes // 60)
    return tx("minutes_minutes", minutes=minutes)


class PcPower(Part):
    """Watches the PC and decides about shutting down or asking."""

    def __init__(
        self,
        hass: HomeAssistant,
        options: dict[str, dict],
        agent: AgentEntities,
        own: OwnEntities,
        presence: PcPresence,
        progress: SteamProgress,
        plugin_busy: PluginBusy,
        names: PcNames = UNNAMED,
    ) -> None:
        super().__init__(hass)
        self.names = names
        self.tx = Texts(hass)
        self.agent = agent
        self.own = own
        self.presence = presence
        self.progress = progress
        self.plugin_busy = plugin_busy
        self.notify = options[SECTION_NOTIFY]
        self.auto_off = options[SECTION_AUTO_OFF]
        self.prompt = options[SECTION_IDLE_PROMPT]
        self.reminder = options[SECTION_REMINDER]
        # Chosen on the settings page ("" when not set yet)
        self.wake_entity: str = options[SECTION_PC][CONF_WAKE_ENTITY] or ""
        # Public state for the binary sensor
        self.safe: bool = False
        self.safe_since: datetime | None = None
        self.auto_off_at: datetime | None = None
        # Internal bookkeeping
        self._quiet = False  # nothing but the idle time keeps the PC from shutting down
        self._auto_off_done_for: datetime | None = None
        self._prompted_input: datetime | None = None
        self._off_timer: CALLBACK_TYPE | None = None
        self._wake_timer: CALLBACK_TYPE | None = None  # a wake that has not brought the PC up yet
        self._wake_timer_for: datetime | None = None  # the wake (_woken_at) the timer is for
        self._was_on: bool | None = None  # None: not known yet (HA's MQTT connection not up)
        self._off_since: datetime | None = None
        self._woken_at: datetime | None = None  # last wake by async_wake
        self._manual_at: datetime | None = None  # last press of the wake entity by the PC switch
        self._pressed_at: datetime | None = None  # our last press of the wake entity, None once the PC was on
        # Last presses of the agent's shutdown and restart buttons (by anyone), from their states: kept
        # here because the buttons turn unavailable with the agent
        self._shutdown_at: datetime | None = None
        self._restart_at: datetime | None = None
        self._game_ended_at: datetime | None = None  # last game end seen live (kept over a restart)
        # The last input when the agent came online: the logon sets it just before (or, the agent
        # only restarted, the last use). A later value is input by someone at the PC. Both are the
        # PC's time, so the PC's clock may differ from Home Assistant's. None: taken from the next
        # value, or from the current one while the agent is online.
        self._input_base: datetime | None = None
        self._started_at = dt_util.utcnow()
        self._last_reminder: datetime | None = None

    # --- lifecycle -------------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        watched = [
            self.agent.last_input,
            self.agent.network,
            self.agent.game_running,
            self.agent.agent_online,
            self.own.auto_off_enabled,
            self.own.woken_by_automation,
            self.agent.shutdown_button,
            self.agent.restart_button,
        ]
        self._note_button_press(self._state(self.agent.shutdown_button))
        self._note_button_press(self._state(self.agent.restart_button))
        self._started_at = dt_util.utcnow()
        self._input_base = self._timestamp(self.agent.last_input)
        self._unsubs.append(self.hass.bus.async_listen("mobile_app_notification_action", self._on_push_action))
        self._unsubs.append(async_track_state_change_event(self.hass, watched, self._on_state_change))
        self._unsubs.append(self.presence.async_add_listener(self._on_presence))
        self._unsubs.append(self.progress.async_add_listener(self._evaluate))
        self._unsubs.append(self.plugin_busy.async_add_listener(self._evaluate))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_tick, TICK))
        self._unsubs.append(async_track_time_change(self.hass, self._on_full_hour, minute=0, second=0))
        self._was_on = self.presence.on if self.presence.known else None
        if not self._was_on:
            # Off already, or not known yet: a wake flag left on is cleared once the PC is known to
            # be off for the confirm time
            self._off_since = self.presence.since
            self._off_timer = async_call_later(self.hass, OFF_CONFIRM, self._on_off_confirmed)
        self._evaluate()

    @callback
    def async_stop(self) -> None:
        self._cancel_off_timer()
        if self._wake_timer is not None:
            self._wake_timer()
            self._wake_timer = None
        super().async_stop()

    @property
    def prompted_input(self) -> datetime | None:
        """The last input the idle prompt was sent for (kept by the safe sensor over a restart)."""
        return self._prompted_input

    @callback
    def restore_prompted_input(self, value: Any) -> None:
        """After a reload or restart the same idle time must not prompt again; only a prompt that
        was sent counts, not one a download or a game held back."""
        if self._prompted_input is None and value:
            self._prompted_input = dt_util.parse_datetime(str(value))

    @property
    def game_ended_at(self) -> datetime | None:
        """The last game end seen (kept by the safe sensor over a restart)."""
        return self._game_ended_at

    @callback
    def restore_game_ended_at(self, value: Any) -> None:
        """A game that ended just before a reload or restart still counts like input."""
        if self._game_ended_at is None and value:
            self._game_ended_at = dt_util.parse_datetime(str(value))

    # --- helpers ---------------------------------------------------------------------------

    def _state(self, entity_id: str) -> State | None:
        return self.hass.states.get(entity_id)

    def _is(self, entity_id: str, value: str) -> bool:
        state = self._state(entity_id)
        return state is not None and state.state == value

    def _timestamp(self, entity_id: str) -> datetime | None:
        state = self._state(entity_id)
        if state is None or state.state in BAD_STATES:
            return None
        return dt_util.parse_datetime(state.state)

    def _network_bytes_s(self) -> float | None:
        """The agent's network rate in B/s, whatever display unit the user gave the sensor."""
        state = self._state(self.agent.network)
        if state is None or state.state in BAD_STATES:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        unit = state.attributes.get("unit_of_measurement") or UnitOfDataRate.BYTES_PER_SECOND
        try:
            return DataRateConverter.convert(value, unit, UnitOfDataRate.BYTES_PER_SECOND)
        except HomeAssistantError:
            return value

    def _off_for(self, entity_id: str, minutes: int, now: datetime) -> bool:
        """State is off and has been for at least the given minutes (like a condition with for:)."""
        state = self._state(entity_id)
        return state is not None and state.state == "off" and now - state.last_changed >= timedelta(minutes=minutes)

    def _last_wake(self) -> datetime | None:
        """When the wake entity was last triggered: a button keeps it as state, a script as
        attribute last_triggered, a switch by turning on."""
        state = self._state(self.wake_entity) if self.wake_entity else None
        if state is None:
            return None
        domain = self.wake_entity.split(".", 1)[0]
        if domain in ("button", "input_button"):
            return self._timestamp(self.wake_entity)
        if domain == "script":
            last = state.attributes.get("last_triggered")
            return last if isinstance(last, datetime) else dt_util.parse_datetime(str(last)) if last else None
        return state.last_changed if state.state == "on" else None

    def _automation_wake(self) -> datetime | None:
        """The latest sign of a wake by an automation: async_wake, the wake flag switched on since
        the start (every wake path sets it, also one of the user's own), or a use of the wake entity
        from before the last press by the PC switch (a switch that follows the PC's reachability
        turns on well after that press)."""
        times = [self._woken_at]
        flag = self._state(self.own.woken_by_automation)
        if flag is not None and flag.state == "on" and flag.last_changed > self._started_at:
            times.append(flag.last_changed)
        pressed = self._last_wake()
        if pressed is not None and (self._manual_at is None or pressed < self._manual_at - MANUAL_MATCH):
            times.append(pressed)
        known = [t for t in times if t is not None]
        return max(known) if known else None

    def _idle_since(self, last_input: datetime | None) -> datetime | None:
        """Since when nobody uses the PC: the last input, or the end of the last game when that is
        later (a game played with a controller leaves no input)."""
        if last_input is None:
            return None
        if self._game_ended_at is not None and self._game_ended_at > last_input:
            return self._game_ended_at
        return last_input

    def _input_after_logon(self, last_input: datetime | None) -> bool:
        """Input by someone at the PC: well after the logon's last input (_input_base). Both are
        the PC's time."""
        return self._input_base is not None and last_input is not None and last_input > self._input_base + INPUT_MARGIN

    def _wake_action(self) -> tuple[str, str] | None:
        domain = self.wake_entity.split(".", 1)[0] if self.wake_entity else ""
        action = WAKE_ACTIONS.get(domain)
        return (domain, action) if action else None

    async def _press_wake(self) -> None:
        wake = self._wake_action()
        if wake is not None:
            await self.hass.services.async_call(wake[0], wake[1], target={"entity_id": self.wake_entity}, blocking=True)

    def _booting(self) -> bool:
        """Our last press of the wake entity is within BOOT_WINDOW and the PC has not been on since:
        it is still starting from that press."""
        return self._pressed_at is not None and dt_util.utcnow() - self._pressed_at < BOOT_WINDOW

    async def _press_once(self) -> bool:
        """Press the wake entity unless the PC is still starting from our last press (_booting);
        True when pressed. Checked and noted without an await in between, so calls at the same time
        share one press."""
        if self._booting():
            _LOGGER.info("Wake entity not pressed again: the PC is still starting from the last press")
            return False
        self._pressed_at = dt_util.utcnow()
        self._notify_listeners()  # the PC state shows "starting" right away
        try:
            await self._press_wake()
        except Exception:
            self._pressed_at = None  # not pressed: the next call tries again
            self._notify_listeners()
            raise
        return True

    def _notify(self, kind: str | None, data: dict[str, Any]) -> None:
        send_push(self.hass, self.notify, data, self.names, kind)

    # --- triggers --------------------------------------------------------------------------

    @callback
    def _on_state_change(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        if entity_id == self.agent.last_input:
            self._on_input(event.data["new_state"])
            self._maybe_clear_prompt()
        elif entity_id == self.agent.agent_online:
            new = event.data["new_state"]
            if new is None or new.state != "on":
                # The agent is gone (shut down, or a reconnect): its next last input is the base
                self._input_base = None
        elif entity_id == self.agent.game_running:
            old, new = event.data["old_state"], event.data["new_state"]
            if new is not None and new.state == "on":
                # Someone plays (maybe without keyboard or mouse): the idle push no longer applies
                self._clear_prompt()
            elif old is not None and old.state == "on" and new is not None and new.state == "off":
                self._game_ended_at = dt_util.utcnow()
        elif entity_id in (self.agent.shutdown_button, self.agent.restart_button):
            self._note_button_press(event.data["new_state"])
        self._evaluate()

    @callback
    def _on_tick(self, _now: datetime) -> None:
        self._evaluate()

    @callback
    def _on_full_hour(self, now: datetime) -> None:
        self._reminder(now)

    # --- logic -----------------------------------------------------------------------------

    @callback
    def _evaluate(self) -> None:
        if self._was_on is None and self.presence.known:
            # Not known at the start, known now (off; on comes through _on_presence): from now on
            # a change of it is a start or a shutdown again
            self._was_on = self.presence.on
        if self._input_base is None and self._is(self.agent.agent_online, "on"):
            # The agent is back while its last input did not expire (a reconnect, a restart of the
            # agent): the same session, its current value is the base
            self._input_base = self._timestamp(self.agent.last_input)
        now = dt_util.utcnow()
        last_input = self._timestamp(self.agent.last_input)
        self._update_safe(now, last_input)
        self._update_auto_off(now, last_input)
        self._idle_prompt(now, last_input)
        self._notify_listeners()

    def _update_safe(self, now: datetime, last_input: datetime | None) -> None:
        """Idle long enough (since the last input or game end), little network traffic, nothing
        busy, no game running."""
        network = self._network_bytes_s()
        self._quiet = (
            network is not None
            and network < self.auto_off[CONF_NETWORK_LIMIT_KBS] * 1000
            and not self.progress.busy
            and not self.plugin_busy.busy
            and not self._is(self.agent.game_running, "on")
        )
        idle_since = self._idle_since(last_input)
        safe = (
            self._quiet
            and idle_since is not None
            and now - idle_since > timedelta(minutes=self.auto_off[CONF_IDLE_MINUTES])
        )
        if safe and not self.safe:
            self.safe_since = now
        elif not safe:
            self.safe_since = None
        self.safe = safe

    def _update_auto_off(self, now: datetime, last_input: datetime | None) -> None:
        """Shut down a PC that an automation woke, once it has been safe for the quiet time."""
        quiet = timedelta(minutes=self.auto_off[CONF_QUIET_MINUTES])
        after_wake = timedelta(minutes=self.auto_off[CONF_MIN_AFTER_WAKE_MINUTES])
        active = (
            self._is(self.own.auto_off_enabled, "on")
            and self._is(self.own.woken_by_automation, "on")
            and self.presence.on
        )
        # Only the idle time may be missing for a time to show: a download, a game, network traffic
        # or a plugin keeps the PC on for an unknown time
        if not active or last_input is None or not self._quiet:
            self.auto_off_at = None
            return

        if self.safe and self.safe_since is not None:
            due = self.safe_since + quiet
        else:
            due = self._idle_since(last_input) + timedelta(minutes=self.auto_off[CONF_IDLE_MINUTES]) + quiet
        # The minimum time after the wake: also for a wake entity that is off again by now (a switch
        # that only gives a pulse) or a wake path of the user's own
        woken = self._automation_wake()
        if woken is not None and woken + after_wake > due:
            due = woken + after_wake
        self.auto_off_at = due

        if not self.safe or now < due or self._auto_off_done_for == self.safe_since:
            return
        self._auto_off_done_for = self.safe_since
        idle = self.auto_off[CONF_IDLE_MINUTES]
        _LOGGER.info("Auto off: PC safe since %s, shutting down", self.safe_since)
        self._notify(
            PUSH_IDLE,
            {
                "title": self.tx("auto_off_title"),
                "message": self.tx("auto_off_message", idle=idle, quiet=self.auto_off[CONF_QUIET_MINUTES]),
            }
        )
        self.hass.async_create_task(
            self.hass.services.async_call(
                "button", "press", target={"entity_id": self.agent.shutdown_button}, blocking=False
            )
        )

    def _idle_prompt(self, now: datetime, last_input: datetime | None) -> None:
        """Manually started PC idle for a while: ask by push whether to shut it down."""
        if last_input is None or self._prompted_input == last_input:
            return
        game = self._state(self.agent.game_running)
        game_free = game is None or game.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) or self._off_for(
            self.agent.game_running, self.prompt[CONF_AFTER_GAME_MINUTES], now
        )
        prompt_minutes = self.prompt[CONF_PROMPT_AFTER_MINUTES]
        steam_free = not self.progress.busy and now - self.progress.busy_since >= timedelta(
            minutes=self.prompt[CONF_AFTER_DOWNLOAD_MINUTES]
        )
        eligible = (
            self._is(self.own.auto_off_enabled, "on")
            and self._is(self.own.woken_by_automation, "off")
            and self.presence.on
            and steam_free
            and game_free
            and now - last_input >= timedelta(minutes=prompt_minutes)
        )
        if not eligible:
            return
        self._prompted_input = last_input
        _LOGGER.info("Idle prompt: no input since %s", last_input)
        self._notify(
            PUSH_IDLE,
            {
                "title": self.tx("idle_title"),
                "message": self.tx("idle_message", duration=_minutes_text(self.tx, prompt_minutes)),
                "data": {
                    "tag": TAG_PC_IDLE,
                    "channel": self.tx("channel_pc"),
                    "notification_icon": "mdi:desktop-tower-monitor",
                    "clickAction": self.notify[CONF_LINK],
                    "actions": [{"action": ACTION_PC_SHUTDOWN, "title": self.tx("idle_button")}],
                },
            }
        )

    @callback
    def _maybe_clear_prompt(self) -> None:
        """Remove the idle push once someone uses the PC again or it is off."""
        if self._prompted_input is None:
            return
        last_input = self._timestamp(self.agent.last_input)
        if self.presence.on and (last_input is None or last_input <= self._prompted_input):
            return
        self._clear_prompt()

    def _clear_prompt(self) -> None:
        if self._prompted_input is None:
            return
        self._prompted_input = None
        self._notify(None, {"message": "clear_notification", "data": {"tag": TAG_PC_IDLE}})

    def _prompt_outdated(self) -> str | None:
        """Why the shut down button of an idle push no longer applies: a game runs, or the PC was
        used since the push (its prompt is cleared then). A push removed from one phone may still
        be on another one."""
        if self._is(self.agent.game_running, "on"):
            return self.tx("lock_game")
        last_input = self._timestamp(self.agent.last_input)
        if self._prompted_input is None or (last_input is not None and last_input > self._prompted_input):
            return self.tx("lock_in_use")
        return None

    def _reminder(self, now: datetime) -> None:
        """Remind while the auto off switch has been off for a long time, then every N hours
        (counted from the last reminder, not by the clock)."""
        state = self._state(self.own.auto_off_enabled)
        if state is None or state.state != "off":
            self._last_reminder = None
            return
        after = self.reminder[CONF_REMINDER_AFTER_HOURS]
        every = timedelta(hours=self.reminder[CONF_REMINDER_EVERY_HOURS])
        if now - state.last_changed < timedelta(hours=after):
            return
        if self._last_reminder is not None and now - self._last_reminder < every - REMINDER_MARGIN:
            return
        self._last_reminder = now
        self._notify(
            PUSH_REMINDER,
            {
                "title": self.tx("reminder_title"),
                "message": self.tx("reminder_message", hours=after),
                "data": {
                    "tag": TAG_AUTO_OFF_REMINDER,
                    "channel": self.tx("channel_pc"),
                    "importance": "default",
                    "actions": [
                        {"action": ACTION_AUTO_OFF_ON, "title": self.tx("reminder_button")},
                        {"action": "URI", "title": self.tx("button_view"), "uri": self.notify[CONF_LINK]},
                    ],
                },
            }
        )

    @callback
    def _on_push_action(self, event: Event) -> None:
        """Buttons of the idle prompt (shut down) and of the reminder (switch auto off on again)."""
        action = self.names.action_of(str(event.data.get("action") or ""))
        if action == ACTION_PC_SHUTDOWN:
            self._notify(None, {"message": "clear_notification", "data": {"tag": TAG_PC_IDLE}})
            if not self.presence.on:
                _LOGGER.info("Shutdown from the idle push: the PC is off already")
                return
            reason = self._prompt_outdated()
            if reason is not None:
                _LOGGER.info("Shutdown from the idle push refused: %s", reason)
                self._push_stays_on(reason, None)
                return
            _LOGGER.info("Shutdown from the idle push")
            # With the shutdown lock of the PC switch; its answer to the button is always sent
            self.hass.async_create_task(self.async_pc_turn_off(None))
        elif action == ACTION_AUTO_OFF_ON:
            _LOGGER.info("Auto off switched on from the reminder push")
            self._notify(None, {"message": "clear_notification", "data": {"tag": TAG_AUTO_OFF_REMINDER}})
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "switch", "turn_on", target={"entity_id": self.own.auto_off_enabled}, blocking=False
                )
            )

    # --- start type: keep the wake flag honest ---------------------------------------------
    # The wake paths set the wake flag switch themselves. Here it is reset when the PC is off,
    # when a wake evidently failed and the PC was later started by hand, or when someone sits
    # down at an automatically woken PC (then it counts as manual). "On" is the PC switch
    # (presence.py), so this works without the optional "PC reachable" sensor too.

    def _cancel_off_timer(self) -> None:
        if self._off_timer is not None:
            self._off_timer()
            self._off_timer = None

    def _clear_woken(self, reason: str) -> None:
        _LOGGER.info("Wake flag cleared: %s", reason)
        self.hass.async_create_task(
            self.hass.services.async_call(
                "switch", "turn_off", target={"entity_id": self.own.woken_by_automation}, blocking=False
            )
        )

    @callback
    def _on_presence(self) -> None:
        on = self.presence.on
        if on != self._was_on:
            known_before = self._was_on is not None
            self._was_on = on
            if on:
                self._pressed_at = None  # the press brought the PC up: the next wake may press again
            self._cancel_off_timer()
            if not on:
                self._off_since = self.presence.since
                self._off_timer = async_call_later(self.hass, OFF_CONFIRM, self._on_off_confirmed)
            elif known_before:
                # Not when the state only became known (HA's MQTT connection came up): no start
                self._on_came_on()
            self._maybe_clear_prompt()
        self._evaluate()

    def _on_came_on(self) -> None:
        """Started by hand: switched on by the PC switch after the last wake by an automation (a
        wake that failed leaves the flag on), or off for long and on without a recent wake."""
        if self._off_since is None or not self._is(self.own.woken_by_automation, "on"):
            return
        woken = self._automation_wake()
        if self._manual_at is not None and (woken is None or self._manual_at - woken > MANUAL_MATCH):
            self._clear_woken("PC switched on by hand after the last wake")
        elif self.presence.since - self._off_since > LONG_OFF and (
            woken is None or dt_util.utcnow() - woken > WAKE_WINDOW
        ):
            self._clear_woken("PC came on without a recent wake, flag was stale")

    @callback
    def _on_off_confirmed(self, _now: datetime) -> None:
        self._off_timer = None
        flag = self._state(self.own.woken_by_automation)
        if self.presence.on or flag is None or flag.state != "on" or self._off_since is None:
            return
        if not self.presence.known:
            # HA's MQTT connection is not up (yet): that says nothing about the PC, ask again later
            self._off_timer = async_call_later(self.hass, OFF_CONFIRM, self._on_off_confirmed)
            return
        # Only if the flag is from before the PC went off: a new wake within the confirm time
        # (by async_wake, or a flag switched on meanwhile by another wake path) keeps it
        wakes = [t for t in (self._woken_at,) if t is not None]
        if flag.last_changed > self._started_at:
            wakes.append(flag.last_changed)
        if not wakes or max(wakes) < self._off_since:
            self._clear_woken("PC is off")

    def _on_input(self, new: State | None) -> None:
        last_input = None if new is None or new.state in BAD_STATES else dt_util.parse_datetime(new.state)
        if last_input is None or self._input_base is None:
            # Gone (expired: the PC is off), or the first value since the agent came online: the logon
            self._input_base = last_input
            return
        if self._is(self.own.woken_by_automation, "on") and self.presence.on and self._input_after_logon(last_input):
            self._clear_woken("input at an automatically woken PC, counts as manual now")

    # --- the PC switch ---------------------------------------------------------------------

    def pc_on(self) -> bool:
        """On while the PC is reachable or the agent reports (presence.py)."""
        return self.presence.on

    def _note_button_press(self, state: State | None) -> None:
        """A button's state is the time of its last press: note it as the shutdown's or the restart's.
        The newest one counts, so a button back from unavailable with its old time changes nothing."""
        if state is None or state.state in BAD_STATES:
            return
        pressed = dt_util.parse_datetime(state.state)
        if pressed is None:
            return
        if state.entity_id == self.agent.shutdown_button:
            if self._shutdown_at is None or pressed > self._shutdown_at:
                self._shutdown_at = pressed
        elif self._restart_at is None or pressed > self._restart_at:
            self._restart_at = pressed

    @property
    def phase(self) -> str:
        """The PC state sensor (one of PC_PHASES): starting from our press of the wake entity until
        the PC is on (or BOOT_WINDOW is over), shutting down from a press of the agent's shutdown
        button until it is off (or GOING_DOWN is over), restarting from a press of its restart button
        until the agent is back (or RESTART_WINDOW is over); otherwise on or off."""
        now = dt_util.utcnow()
        restart = self._restart_at
        if restart is not None and now - restart < RESTART_WINDOW and (
            self._shutdown_at is None or restart > self._shutdown_at
        ):
            online = self._state(self.agent.agent_online)
            if online is None or online.state != "on" or online.last_changed <= restart:
                return PHASE_RESTARTING
        if self.presence.on:
            down = self._shutdown_at
            if down is not None and now - down < GOING_DOWN and down > self.presence.since:
                return PHASE_STOPPING
            return PHASE_ON
        return PHASE_STARTING if self._booting() else PHASE_OFF

    async def async_pc_turn_on(self) -> bool:
        """Switch on by hand (dashboard, voice): trigger the wake entity, without the wake flag.
        Nothing while the PC is on: a device that presses the power button would switch it off.
        False when no wake entity is chosen."""
        if self.presence.on:
            _LOGGER.debug("PC switch on: the PC is on already")
            return True
        if self._wake_action() is None:
            _LOGGER.warning("No wake entity chosen (settings page, section PC), cannot switch the PC on")
            return False
        if self._booting():
            _LOGGER.debug("PC switch on: the PC is still starting from the last press")
            return True
        # Switched on by hand: an old wake flag (from a wake that failed) does not apply to this start
        self._manual_at = dt_util.utcnow()
        await self._press_once()
        return True

    def _push_stays_on(self, reason: str, kind: str | None) -> None:
        """Why the PC was not shut down; kind None for the answer to a push button (always sent)."""
        self._notify(
            kind,
            {
                "title": self.tx("lock_title"),
                "message": self.tx("lock_message", reason=reason),
                "data": {
                    "tag": TAG_PC_LOCK,
                    "channel": self.tx("channel_pc"),
                    "notification_icon": "mdi:shield-lock-outline",
                    "clickAction": self.notify[CONF_LINK],
                },
            }
        )

    async def async_pc_turn_off(self, kind: str | None = PUSH_PROBLEMS) -> str | None:
        """Shut down, unless Steam works or a plugin keeps the PC busy: then a push (of this kind),
        and the reason is returned (None when the PC shuts down)."""
        if self.progress.busy or self.plugin_busy.busy:
            if self.progress.busy:
                reason = self.tx("lock_steam")
            else:
                reason = self.tx("lock_plugin", names=", ".join(self.plugin_busy.names()))
            _LOGGER.info("PC switch off refused: %s", reason)
            self._push_stays_on(reason, kind)
            return reason
        await self.hass.services.async_call(
            "button", "press", target={"entity_id": self.agent.shutdown_button}, blocking=True
        )
        return None

    # --- services for plugins and the Steam parts ------------------------------------------

    async def _mark_and_wake(self) -> None:
        """Set the wake flag and press the wake entity; a PC still starting from our last press
        (_booting) is not pressed again, the caller waits for it as for its own press."""
        self._woken_at = dt_util.utcnow()
        await self.hass.services.async_call(
            "switch", "turn_on", target={"entity_id": self.own.woken_by_automation}, blocking=True
        )
        await self._press_once()
        # A PC that is still off when the wake window ends was not woken: a later start is not this
        # wake's (not tied to how long the caller waits: a slow boot still counts as this wake)
        if self._wake_timer is not None:
            self._wake_timer()
        self._wake_timer_for = self._woken_at
        self._wake_timer = async_call_later(self.hass, WAKE_WINDOW, self._on_wake_window_over)

    @callback
    def _on_wake_window_over(self, _now: datetime) -> None:
        self._wake_timer = None
        if self.presence.on or not self._is(self.own.woken_by_automation, "on"):
            return
        if not self.presence.known:
            # HA's MQTT connection is down: that says nothing about the PC, ask again later
            self._wake_timer = async_call_later(self.hass, OFF_CONFIRM, self._on_wake_window_over)
            return
        woken = self._automation_wake()
        if woken is not None and self._wake_timer_for is not None and woken - self._wake_timer_for > MANUAL_MATCH:
            return  # a newer wake by another wake path: that one is not over yet
        self._clear_woken("the wake did not bring the PC up")

    async def async_wake(
        self,
        wait_for_entity: str | None,
        wait_for_state: str | None,
        timeout: timedelta,
        before_wake: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, bool]:
        """Wake the PC if it is off (and mark it as woken, which allows auto off), then wait
        until the Steam agent and optionally another entity are ready. before_wake runs right
        before the PC is woken, only when it really has to be woken (a game start leaves a note
        for the lock script there, steam_jobs.py)."""
        woken = False
        if not self.presence.on:
            if self._wake_action() is None:
                _LOGGER.warning("No wake entity chosen (settings page, section PC), cannot wake the PC")
                return {"woken": False, "ready": False}
            if before_wake is not None:
                await before_wake()
            await self._mark_and_wake()
            woken = True
        # On, but the agent gone or its buttons unavailable: maybe shutting down right now
        shutdown = self._state(self.agent.shutdown_button)
        going_down = not woken and (
            not self._is(self.agent.agent_online, "on") or shutdown is None or shutdown.state == STATE_UNAVAILABLE
        )
        going_down_until = dt_util.utcnow() + GOING_DOWN

        def ready() -> bool:
            shutdown = self._state(self.agent.shutdown_button)
            if not self.presence.on or shutdown is None or shutdown.state == STATE_UNAVAILABLE:
                return False
            if not wait_for_entity:
                return True
            if wait_for_state:
                return self._is(wait_for_entity, wait_for_state)
            extra = self._state(wait_for_entity)
            return extra is not None and extra.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)

        async def wake_if_went_off() -> None:
            # Right after a shutdown the PC still counts as on for up to 90 s: wake it once it is off.
            # Only then: a PC that someone shuts down later while this waits stays off.
            nonlocal woken
            if (
                going_down
                and not woken
                and not self.presence.on
                and dt_util.utcnow() < going_down_until
                and self._wake_action() is not None
            ):
                if before_wake is not None:
                    await before_wake()
                await self._mark_and_wake()
                woken = True

        is_ready = await wait_until(ready, timeout, wake_if_went_off)
        if woken and is_ready:
            await asyncio.sleep(BOOT_PAUSE)  # let the PC settle after booting
        _LOGGER.info("Wake: woken=%s ready=%s", woken, is_ready)
        return {"woken": woken, "ready": is_ready}

    async def async_shutdown_when_idle(self, max_wait: timedelta) -> dict[str, Any]:
        """Shut down a PC that an automation woke, once Steam, game and plugins are done."""
        if not self._is(self.own.woken_by_automation, "on"):
            return {"shutdown": False, "reason": "not_woken_by_automation"}

        def free() -> bool:
            return (
                not self.progress.busy
                and not self.plugin_busy.busy
                and not self._is(self.agent.game_running, "on")
            )

        if not await wait_until(free, max_wait):
            return {"shutdown": False, "reason": "still_busy"}
        # Someone at the PC: the flag is cleared by their input, and fresh input (a change of the
        # last input within FRESH_INPUT by HA's clock) is checked here once more before shutting down
        last_input = self._state(self.agent.last_input)
        if not self._is(self.own.woken_by_automation, "on") or (
            last_input is not None
            and dt_util.utcnow() - last_input.last_changed < FRESH_INPUT
            and self._input_after_logon(self._timestamp(self.agent.last_input))
        ):
            return {"shutdown": False, "reason": "user_took_over"}
        _LOGGER.info("Shutdown when idle: PC is free, switching off")
        if await self.async_pc_turn_off() is not None:
            return {"shutdown": False, "reason": "still_busy"}
        return {"shutdown": True, "reason": "idle"}
