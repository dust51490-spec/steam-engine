"""Steam download state: whether Steam really works (our job or the Steam client itself) and the
download percentage. The time of the last real progress is kept and shown as attribute
last_progress; a job without progress for 6 hours no longer keeps the PC on.

It also hands each change of the job state to steam_jobs.py, steam_notify.py and steam_events.py,
saying whether it is news from the PC (live). Only end states are retained: after a restart of HA the
sensor goes from unknown to the end of the last job, and on an MQTT reconnect HA first writes the
state it had (coming from unavailable) and then gets that retained end. Such an end read again is
news only when it is the end of the job handed over last or seen running since (its job ID is
job_id, steam_jobs.py) and was not told yet: it came while HA was not listening. Every end with a job ID is news once (end_id, the job whose
end was told last; both IDs are kept in steam_jobs.py's store); an end without one (a job started by
hand on the PC) only when it came just now. An end found at the start of the integration is told
the same way once every part listens (async_found_end). A job seen running across an end that is no
news, or while HA's MQTT connection is down, counts as running for REPLAY_HOLD more unless a new
state comes first: between two games the PC can be silent for minutes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import protocol
from .agent_entities import AgentEntities
from .const import BUSY_STALL_LIMIT
from .parts import Part
from .presence import PcPresence

NOT_FROM = (STATE_UNKNOWN, STATE_UNAVAILABLE)
REPLAY_HOLD = timedelta(minutes=10)  # longer than the PC's silence between two games of a job

# (old state, new state, live)
JobListener = Callable[[State | None, State, bool], None]


def _percent(state: State | None) -> int:
    try:
        return int(float(state.attributes.get("percent")) + 0.5) if state is not None else 0
    except (TypeError, ValueError):
        return 0


def _pc_progress_time(state: State) -> datetime | None:
    """The PC's time of the last progress (attribute updated, UTC with offset); None without one."""
    updated = state.attributes.get("updated")
    parsed = dt_util.parse_datetime(str(updated)) if updated else None
    if parsed is None or parsed.tzinfo is None:
        return None
    return min(dt_util.as_utc(parsed), dt_util.utcnow())


def _progress_time(old: State | None, new: State | None) -> datetime | None:
    """When the job made progress, judged by this live change of its state; None if it made none.
    A new state or new progress attributes count. Only end states are retained, so after a restart
    of HA or an MQTT reconnect the first state of a running job follows an old end state: that is no
    progress of its own, the PC's time of the last progress is taken instead."""
    if new is None or new.state in NOT_FROM:
        return None
    now = dt_util.utcnow()
    if new.state == protocol.STARTED:
        return now
    was_active = old is not None and protocol.job_active(old.state, old.attributes)
    if not protocol.job_active(new.state, new.attributes):
        return now if was_active else None
    if not was_active:
        return _pc_progress_time(new) or now
    if old.state != new.state or any(old.attributes.get(k) != new.attributes.get(k) for k in protocol.PROGRESS_KEYS):
        return now
    return None


class SteamProgress(Part):
    """Knows whether Steam really works (and not only hangs) and how far it is.

    busy: the PC is on and our job runs with progress in the last 6 h (job_running), a job is being
    handed to the agent or the queue is about to move on (handover), the Steam client downloads
    by itself, or a game is being moved to another drive (moving, steam_move.py).
    """

    def __init__(self, hass: HomeAssistant, agent: AgentEntities, presence: PcPresence) -> None:
        super().__init__(hass)
        self.agent = agent
        self.presence = presence
        # Public state for the sensors and the other parts
        self.last_progress: datetime | None = None
        self.busy = False
        self.busy_since: datetime = dt_util.utcnow()  # when busy last changed
        self.job_running = False
        self.percent = 0
        self._handovers = 0
        self._moving = False
        self._job_listeners: list[JobListener] = []
        # The job handed to the PC last or seen running since, and the job whose end was told last
        # (steam_jobs.py sets job_id and keeps both)
        self.job_id = ""
        self.end_id = ""
        self._rewritten = False  # the job state shown came from unavailable (HA's MQTT cache)
        self._hold_until: datetime | None = None
        self._timer_due: datetime | None = None
        self._timer: CALLBACK_TYPE | None = None
        self._stopped = False

    # --- lifecycle -------------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.agent.install_status, self.agent.steam_downloading], self._on_change
            )
        )
        self._unsubs.append(self.presence.async_add_listener(self._evaluate))
        self._evaluate()

    @callback
    def async_stop(self) -> None:
        # A hand-over cancelled by the unload ends after this: nothing may start a timer then
        self._stopped = True
        self._set_timer(None)
        super().async_stop()

    @callback
    def restore(self, last_progress: datetime | None) -> None:
        """Take over the last progress time after a restart (from the sensor's restore state)."""
        if last_progress is not None and self.last_progress is None:
            self.last_progress = last_progress
        self._evaluate()

    @callback
    def set_handover(self, active: bool) -> None:
        """steam_jobs.py hands a job to the agent or moves the queue on: busy until the job reports
        (no shutdown meanwhile)."""
        self._handovers = max(0, self._handovers + (1 if active else -1))
        self._evaluate()

    @callback
    def set_moving(self, active: bool) -> None:
        """steam_move.py: a move job runs on the PC (or was just sent): busy, no shutdown."""
        self._moving = active
        self._evaluate()

    @callback
    def async_add_job_listener(self, listener: JobListener) -> CALLBACK_TYPE:
        """Called on each change of the job state with (old, new, live), before busy is evaluated
        again: a part that takes over (set_handover) keeps Steam busy without a gap. live: news from
        the PC (module docstring); old is None for the end found at the start."""
        self._job_listeners.append(listener)

        @callback
        def remove() -> None:
            self._job_listeners.remove(listener)

        return remove

    # --- logic -----------------------------------------------------------------------------

    @callback
    def _on_change(self, event: Event) -> None:
        if event.data["entity_id"] == self.agent.install_status:
            old, new = event.data["old_state"], event.data["new_state"]
            live = self._live(old, new)
            if live:
                progress_at = _progress_time(old, new)
                if progress_at is not None:
                    self.last_progress = progress_at
            if new is not None:
                for listener in list(self._job_listeners):
                    listener(old, new, live)
        self._evaluate()

    def _live(self, old: State | None, new: State | None) -> bool:
        """Whether this change of the job state is news from the PC; sets the hold of a job seen
        running (module docstring)."""
        rewritten, self._rewritten = self._rewritten, old is not None and old.state == STATE_UNAVAILABLE
        was_active = old is not None and protocol.job_active(old.state, old.attributes)
        if new is None or new.state in NOT_FROM:
            if was_active and new is not None and new.state == STATE_UNAVAILABLE:
                self._hold_until = dt_util.utcnow() + REPLAY_HOLD  # HA's MQTT connection is down
            return False
        if protocol.job_active(new.state, new.attributes):
            # Not retained: news from the PC, unless HA wrote it from its cache on a reconnect
            if old is not None and old.state == STATE_UNAVAILABLE:
                return False
            self._hold_until = None
            return True
        read_again = old is None or old.state in NOT_FROM or (rewritten and was_active)
        news = self._end_news(new, read_again) if new.state in protocol.ENDED else not read_again
        if not news:
            if was_active:
                self._hold_until = dt_util.utcnow() + REPLAY_HOLD  # an older end read again
            return False
        self._hold_until = None
        if new.state in protocol.ENDED and protocol.job_id(new.attributes):
            self.end_id = protocol.job_id(new.attributes)
        return True

    def _end_news(self, end: State, read_again: bool) -> bool:
        """Whether a job end is news: not told yet (by its job ID), and it came just now or, read
        again, it is the end of job_id (it came while HA was not listening)."""
        job = protocol.job_id(end.attributes)
        if job and job == self.end_id:
            return False  # told already (read again, or sent twice)
        return not read_again or (bool(job) and job == self.job_id)

    @callback
    def async_found_end(self) -> None:
        """Once every part listens: the job end found at the start of the integration, when it is
        news (the end of job_id, which came while HA was down)."""
        status = self.hass.states.get(self.agent.install_status)
        if status is None or status.state not in protocol.ENDED or not self._end_news(status, True):
            return
        self.end_id = protocol.job_id(status.attributes)
        for listener in list(self._job_listeners):
            listener(None, status, True)
        self._evaluate()

    def _since(self, status: State | None) -> datetime | None:
        if self.last_progress is not None:
            return self.last_progress
        return status.last_changed if status is not None else None

    def stalled_seconds(self) -> int | None:
        """Seconds since the last real progress, None if unknown."""
        since = self._since(self.hass.states.get(self.agent.install_status))
        return int((dt_util.utcnow() - since).total_seconds()) if since is not None else None

    @callback
    def _set_timer(self, due: datetime | None) -> None:
        if due == self._timer_due:
            return
        if self._timer is not None:
            self._timer()
            self._timer = None
        self._timer_due = due
        if due is not None:
            self._timer = async_track_point_in_utc_time(self.hass, self._on_timer, due)

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._timer = None
        self._timer_due = None
        self._evaluate()

    @callback
    def _evaluate(self) -> None:
        if self._stopped:
            return
        now = dt_util.utcnow()
        pc_on = self.presence.on
        status = self.hass.states.get(self.agent.install_status)
        downloading = self.hass.states.get(self.agent.steam_downloading)
        active = status is not None and protocol.job_active(status.state, status.attributes)
        if self._hold_until is not None and now >= self._hold_until:
            self._hold_until = None
        until: datetime | None = None
        if active or self._hold_until is not None:
            since = self._since(status)
            until = since + BUSY_STALL_LIMIT if since is not None else None
            if not active and self._hold_until is not None:
                until = self._hold_until if until is None else min(until, self._hold_until)
        # The 6 h limit and the hold run out by time alone: evaluate again then
        self._set_timer(until if until is not None and until > now else None)
        job_running = pc_on and until is not None and now < until
        busy = pc_on and (
            job_running or self._handovers > 0 or self._moving or (downloading is not None and downloading.state == "on")
        )
        if busy != self.busy:
            self.busy_since = now
        valid = status is not None and status.state not in NOT_FROM
        self.busy, self.job_running, self.percent = busy, job_running, _percent(status) if valid else 0
        self._notify_listeners()

    @property
    def attributes(self) -> dict[str, Any]:
        return {"last_progress": self.last_progress.isoformat() if self.last_progress else None}
