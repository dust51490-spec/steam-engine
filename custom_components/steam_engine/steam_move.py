"""Moving an installed game to another drive: what the PC's move job (move.ps1, PROTOCOL.md "Move")
reports on steam/move, for Steam busy (no shutdown during a move), the install queue (it waits for a
move), the card (attributes of the download progress sensor) and the result push (steam_notify.py).

running messages come about every 5 s and are not retained; the end states (ok, error) are. A message
the broker delivers live has the retain flag off, the retained copy a new subscription gets first
(after a restart of HA or an MQTT reconnect) has it on: only a live end is news (push, event, the
queue moves on); a retained one only ends a move seen running (or the one just sent, when the PC
answered while the connection was down) and is kept as the last result. An end is handled once (its
"started", state, reason_code and updated): the same end read again is skipped, while a later end of
the same move (move.ps1 -Recover finishing a move that could not be taken back) is news. An end the PC
sends late (late: it could not send it when its move ended) never answers a move just sent, also one
of the same game.

A move counts as running from the command until its first message (at most REQUEST_WAIT: the PC
may need that long to wake, the job waits up to 3 minutes for another Steam job), while running
messages come, and while the agent online reports steam_moving (attribute "moving" of its Steam
downloading sensor): after a restart of HA that is all it knows until the next running message, which
can be minutes away while the Steam client exits. One silent for MOVE_SILENT counts as ended (PC off,
job killed); the listeners hear that as "silent".
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .parts import Part, start_task
from .pc import UNNAMED, PcNames
from .space import drive_letter
from .steam_progress import SteamProgress

_LOGGER = logging.getLogger(__name__)

RUNNING = "running"
OK = "ok"
ERROR = "error"
MOVE_SILENT = timedelta(minutes=5)  # a move without a running message this long counts as ended
REQUEST_WAIT = timedelta(minutes=5)  # a move sent counts as running this long without a message
ENDS_KEPT = 20  # the ends handled, so a repeated one is not handled again

# (kind, message, live): kind "started" (the first running message of a move), "ended" or "silent"
# (no news for MOVE_SILENT or REQUEST_WAIT: the last running message or the command, live False)
MoveListener = Callable[[str, dict[str, Any], bool], None]


def _end_key(data: dict[str, Any]) -> str:
    """One end state: a retained copy of it has the same key, a later end of the same move not."""
    return "|".join(str(data.get(key) or "") for key in ("started", "state", "reason_code", "updated"))


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """The fields of a message as the card and the pushes use them: drives as "F:"."""
    result = dict(data)
    result["appid"] = str(data.get("appid") or "")
    for key in ("from", "drive"):
        result[key] = drive_letter(data.get(key)) or None
    return result


class SteamMove(Part):
    """The move job on the PC: running or not, how far, and its end."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, progress: SteamProgress, names: PcNames = UNNAMED) -> None:
        super().__init__(hass)
        self.names = names
        self._entry = entry
        self.progress = progress
        self.running: dict[str, Any] | None = None  # the last running message of the move that runs
        self.last_end: dict[str, Any] | None = None  # the last end state (live or retained)
        self._requested: dict[str, Any] | None = None  # {appid, drive, at, subscribed}: sent, no message yet
        self._handled: list[str] = []  # _end_key of the ends handled
        self._ended: list[str] = []  # "started" of the moves that ended (their late running messages)
        self._pc_moving = False  # the agent reports steam_moving ON
        self._subscribed = False
        self._move_listeners: list[MoveListener] = []
        self._timer: CALLBACK_TYPE | None = None
        self._stopped = False

    # --- lifecycle -------------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        agent = self.progress.agent
        self._unsubs.append(
            async_track_state_change_event(self.hass, [agent.agent_online, agent.steam_downloading], self._on_agent)
        )
        self._pc_moving = self._read_pc_moving()
        if self._pc_moving:
            self._changed(False)
        start_task(self.hass, self._entry, self._subscribe(), "move subscribe")

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        self._set_timer(None)
        super().async_stop()

    async def _subscribe(self) -> None:
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            _LOGGER.warning("MQTT not available, moves are not followed")
            return
        unsub = await mqtt.async_subscribe(self.hass, self.names.move_topic, self._on_message)
        if self._stopped:
            unsub()
        else:
            self._unsubs.append(unsub)
            self._subscribed = True

    @callback
    def async_add_move_listener(self, listener: MoveListener) -> CALLBACK_TYPE:
        """Called with ("started", message, True) at the first running message of a move, with
        ("ended", message, live) at its end and with ("silent", message, False) when it counts as
        ended without news."""
        self._move_listeners.append(listener)

        @callback
        def remove() -> None:
            self._move_listeners.remove(listener)

        return remove

    # --- state -----------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """A move runs on the PC (by its messages or the agent's steam_moving), or one was sent and
        its first message did not come yet."""
        return self.running is not None or self._requested is not None or self._pc_moving

    @property
    def appid(self) -> str:
        """The game being moved (or sent to be moved), "" when none."""
        if self.running is not None:
            return str(self.running.get("appid") or "")
        return str(self._requested.get("appid") or "") if self._requested is not None else ""

    @property
    def attributes(self) -> dict[str, Any]:
        """For the card: move (the running move, or the one sent: appid, name, from, drive, phase,
        percent, copied_gb, total_gb; phase "requested" before its first message) and move_result
        (the last end: appid, name, from, drive, result ok/error, reason_code, warning, updated)."""
        move: dict[str, Any] | None = None
        if self.running is not None:
            keys = ("appid", "name", "from", "drive", "phase", "percent", "copied_gb", "total_gb")
            move = {key: self.running.get(key) for key in keys}
        elif self._requested is not None:
            move = {
                "appid": self._requested["appid"],
                "name": None,
                "from": None,
                "drive": self._requested["drive"],
                "phase": "requested",
                "percent": 0,
                "copied_gb": None,
                "total_gb": None,
            }
        result: dict[str, Any] | None = None
        if self.last_end is not None:
            end = self.last_end
            result = {
                "appid": end.get("appid"),
                "name": end.get("name"),
                "from": end.get("from"),
                "drive": end.get("drive"),
                "result": end.get("state"),
                "reason_code": end.get("reason_code"),
                "warning": end.get("warning"),
                "updated": end.get("updated"),
            }
        return {"move": move, "move_result": result}

    @callback
    def expect(self, appid: str, drive: str) -> None:
        """A move command is about to go out (steam_jobs.async_move): busy from now on."""
        was = self.active
        self._requested = {
            "appid": str(appid),
            "drive": drive_letter(drive),
            "at": dt_util.utcnow(),
            # A retained end may answer it only when the old retained ends came before (see _on_end)
            "subscribed": self._subscribed,
        }
        self._changed(was)

    @callback
    def not_sent(self) -> None:
        """The command did not go out after all."""
        if self._requested is not None:
            was = self.active
            self._requested = None
            self._changed(was)

    # --- the agent's steam_moving ----------------------------------------------------------

    def _read_pc_moving(self) -> bool:
        agent = self.progress.agent
        online = self.hass.states.get(agent.agent_online)
        downloading = self.hass.states.get(agent.steam_downloading)
        return (
            online is not None
            and online.state == "on"
            and downloading is not None
            and downloading.attributes.get("moving") == "ON"
        )

    @callback
    def _on_agent(self, _event: Event[EventStateChangedData]) -> None:
        moving = self._read_pc_moving()
        if moving != self._pc_moving:
            was = self.active
            self._pc_moving = moving
            self._changed(was)

    # --- messages --------------------------------------------------------------------------

    @callback
    def _on_message(self, msg: mqtt.ReceiveMessage) -> None:
        if not msg.payload:
            return  # retained topic cleared
        try:
            data = json.loads(msg.payload)
        except (TypeError, ValueError):
            _LOGGER.warning("Move message is no JSON: %s", msg.payload)
            return
        if not isinstance(data, dict) or not str(data.get("appid") or "").isdigit():
            return
        data = _clean(data)
        state = data.get("state")
        if state == RUNNING:
            self._on_running(data)
        elif state in (OK, ERROR):
            self._on_end(data, not msg.retain)

    @callback
    def _on_running(self, data: dict[str, Any]) -> None:
        was = self.active
        started = str(data.get("started") or "")
        if started and started in self._ended:
            return  # a late message of a move that ended
        new = self.running is None or str(self.running.get("started") or "") != started
        self.running = data
        if self._requested is not None and self._requested["appid"] == data["appid"]:
            self._requested = None
        self._changed(was)
        if new:
            _LOGGER.info("Move of %s to %s runs (%s)", data["appid"], data.get("drive"), data.get("phase"))
            self._tell("started", data, True)

    @callback
    def _on_end(self, data: dict[str, Any], live: bool) -> None:
        started, key = str(data.get("started") or ""), _end_key(data)
        if key in self._handled:
            return  # the same end again (sent twice, or retained after a reconnect)
        self._handled = [*self._handled, key][-ENDS_KEPT:]
        if started and started not in self._ended:
            self._ended = [*self._ended, started][-ENDS_KEPT:]
        was = self.active
        # Not the move that runs when another one was refused meanwhile (a move started by hand)
        if self.running is not None and str(self.running.get("started") or "") == started:
            self.running = None
        # A retained end not handled before answers the move sent when the connection dropped in
        # between; one sent before the subscription could be an old end read for the first time. An
        # end sent late (late: the PC could not send it at the end of its move) never answers it.
        requested = self._requested
        answered = (
            requested is not None
            and requested["appid"] == data["appid"]
            and not data.get("late")
            and (live or requested["subscribed"])
        )
        if answered:
            self._requested = None
        if self.last_end is None or live or str(data.get("updated") or "") >= str(self.last_end.get("updated") or ""):
            self.last_end = data
        if live:
            _LOGGER.info(
                "Move of %s to %s ended: %s %s", data["appid"], data.get("drive"), data.get("state"), data.get("reason_code") or ""
            )
        self._changed(was)
        # The answer to the command just sent is news even when it came retained (a reconnect)
        self._tell("ended", data, live or answered)

    @callback
    def _tell(self, kind: str, data: dict[str, Any], live: bool) -> None:
        for listener in list(self._move_listeners):
            listener(kind, data, live)

    # --- busy and the silence timer --------------------------------------------------------

    @callback
    def _changed(self, was_active: bool) -> None:
        if self._stopped:
            return
        if was_active != self.active:
            self.progress.set_moving(self.active)
        self._set_timer(self._due())
        self._notify_listeners()

    def _due(self) -> datetime | None:
        """When the move counts as ended without news: MOVE_SILENT after its last running message,
        REQUEST_WAIT after a command without an answer."""
        if self.running is not None:
            return dt_util.utcnow() + MOVE_SILENT
        if self._requested is not None:
            return self._requested["at"] + REQUEST_WAIT
        return None

    @callback
    def _set_timer(self, due: datetime | None) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None
        if due is not None:
            self._timer = async_track_point_in_utc_time(self.hass, self._on_timer, due)

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._timer = None
        was = self.active
        silent: dict[str, Any] | None = None
        if self.running is not None:
            _LOGGER.warning("Move of %s: no news from the PC for %s, counted as ended", self.running.get("appid"), MOVE_SILENT)
            silent, self.running = self.running, None
        elif self._requested is not None:
            _LOGGER.warning("Move of %s: the PC did not answer within %s", self._requested.get("appid"), REQUEST_WAIT)
            silent = {"appid": self._requested["appid"], "drive": self._requested["drive"]}
            self._requested = None
        self._changed(was)
        if silent is not None:
            self._tell("silent", silent, False)
