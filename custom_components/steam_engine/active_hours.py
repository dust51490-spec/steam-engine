"""Active hours: the time of day in which the integration may start automatic work by itself
(automatic updates, new purchases, waking the PC for them).

The window is a start and an end time of the local day. It may cross midnight (22:00-06:00);
start == end means all day. Switched off it is always open. The update and purchase checks run at
any time; only steam_jobs.py asks whether the window is open before it starts automatic work, and
starts what waited when it opens (listeners of this part are called on every opening and closing).

The open state is kept by timers at the next start and end (async_track_point_in_utc_time); when one
fires, the state is read from the clock again and the next timer computed anew from the local time
zone, so a change to or from daylight saving time
moves them with the wall clock. A start or end in the hour skipped in spring moves on by that hour
(02:30 becomes 03:30); one in the hour repeated in autumn counts at its first occurrence.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo
import logging

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import CONF_ACTIVE_ENABLED, CONF_ACTIVE_END, CONF_ACTIVE_START, DEFAULT_OPTIONS, SECTION_ACTIVE_HOURS
from .parts import Part

_LOGGER = logging.getLogger(__name__)


def parse_time(value: object, default: str) -> time:
    """HH:MM or HH:MM:SS (the time selector stores the latter); the default when it is not a time."""
    parsed = dt_util.parse_time(str(value or "")) if value else None
    return parsed if parsed is not None else dt_util.parse_time(default)  # type: ignore[return-value]


def is_open(start: time, end: time, local: datetime) -> bool:
    """Whether the wall clock time of local lies in the window [start, end)."""
    if start == end:
        return True
    now = local.time().replace(tzinfo=None)
    if start < end:
        return start <= now < end
    return now >= start or now < end  # crosses midnight


def next_at(wall: time, now: datetime, tz: tzinfo) -> datetime:
    """The next moment after now (strictly) at which the clock of tz shows wall, in UTC."""
    now_utc = dt_util.as_utc(now)
    today: date = now_utc.astimezone(tz).date()
    for days in range(3):
        candidate = dt_util.as_utc(datetime.combine(today + timedelta(days=days), wall, tzinfo=tz))
        if candidate > now_utc:
            return candidate
    raise AssertionError("unreachable: a wall time comes back within two days")  # pragma: no cover


class ActiveHours(Part):
    """The window from the settings page, with its open state; listeners hear each change."""

    def __init__(self, hass: HomeAssistant, options: dict[str, dict]) -> None:
        super().__init__(hass)
        section = options[SECTION_ACTIVE_HOURS]
        defaults = DEFAULT_OPTIONS[SECTION_ACTIVE_HOURS]
        self.enabled: bool = bool(section.get(CONF_ACTIVE_ENABLED))
        self.start: time = parse_time(section.get(CONF_ACTIVE_START), defaults[CONF_ACTIVE_START])
        self.end: time = parse_time(section.get(CONF_ACTIVE_END), defaults[CONF_ACTIVE_END])
        # Switched on with a real window (start == end is all day, as switched off)
        self.limited: bool = self.enabled and self.start != self.end
        self._open = not self.limited or is_open(self.start, self.end, dt_util.now())
        self._timer: CALLBACK_TYPE | None = None
        self._next_start: datetime | None = None

    # --- lifecycle -------------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        if not self.limited:
            return
        self._open = is_open(self.start, self.end, dt_util.now())
        self._unsubs.append(self._cancel_timer)
        self._arm(dt_util.utcnow())

    @callback
    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None

    @callback
    def _arm(self, now: datetime) -> None:
        """Set the timer for the next change: the end while open, the start while closed."""
        tz = dt_util.get_default_time_zone()
        self._next_start = next_at(self.start, now, tz)
        point = next_at(self.end, now, tz) if self._open else self._next_start
        self._timer = async_track_point_in_utc_time(self.hass, self._on_change, point)

    @callback
    def _on_change(self, now: datetime) -> None:
        """A start or end is due. The state follows the clock, not a toggle: the timer fires at or
        after its point (now), and the clock may be later still (a late timer, a clock corrected
        after the start, e.g. by NTP on a device without a real time clock)."""
        self._timer = None
        now = max(now, dt_util.utcnow())
        was_open, self._open = self._open, is_open(self.start, self.end, dt_util.as_local(now))
        self._arm(now)
        if self._open != was_open:
            _LOGGER.info("Active hours %s", "begin" if self._open else "end")
            self._notify_listeners()

    # --- state -----------------------------------------------------------------------------

    def is_open(self) -> bool:
        """Automatic work may start now (always, when the window is off or all day)."""
        return self._open

    @property
    def next_start(self) -> datetime | None:
        """The next start of the window (UTC) while it is closed, else None."""
        return None if self._open else self._next_start

    def start_text(self) -> str:
        """The next start as local HH:MM (for pushes); empty while open."""
        start = self.next_start
        return dt_util.as_local(start).strftime("%H:%M") if start is not None else ""

    def attribute(self) -> dict[str, str] | None:
        """{"start": "HH:MM", "end": "HH:MM"} while the window is switched on, else None."""
        if not self.enabled:
            return None
        return {"start": self.start.strftime("%H:%M"), "end": self.end.strftime("%H:%M")}
