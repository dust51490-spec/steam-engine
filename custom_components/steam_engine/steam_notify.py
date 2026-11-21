"""Steam pushes: start, progress and result of a job, aborted job, stalled download, uninstall
result, start, progress and result of a move, silent agent and new updates; the push "not enough
space" with its buttons is no_space.py.
Which games update by themselves is decided in steam_jobs.py; here only what to tell.

With the option "automatic updates quiet" a job of only games the automatic updates handed out
(steam_jobs.auto_quiet) pushes nothing while it runs and nothing when it goes well or is aborted (it
goes on by itself); its result is pushed when a game had too little space, or failed on its last
automatic try. The update push then leaves the automatic updates out. A job of only muted games the
automatic updates handed out is quiet the same way without the option (steam_jobs.muted); muted
games are neither in the update push nor in its "Install all".

Job pushes follow the news of steam_progress.py: the end of the job handed over that came while HA
was not listening (a restart of HA, an MQTT reconnect) is told once, its result replacing the
progress push; an older end read again is not.

A move (steam_move.py) pushes with its own tag: its start and progress (kind progress, a new push on
each phase and each 10 % step of the copy), its result (kind result; without it the progress push is
cleared). Only what came from the PC just now: an end read again after a restart of HA or a
reconnect is not told, unless it ends the move whose progress was pushed (its end came while HA was
not listening; that move is kept over a restart, steam_jobs.move_pushed). A move that counts as
ended without an end (silent) clears its progress push. Moves are always asked for by hand, so the
quiet option does not apply.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import protocol
from .agent_entities import AgentEntities, library_games
from .const import (
    BUSY_STALL_LIMIT,
    CONF_LINK,
    CONF_QUIET_AUTO_UPDATES,
    CONF_STALLED_MINUTES,
    CONF_STEAM_LINK,
    PUSH_PROBLEMS,
    PUSH_PROGRESS,
    PUSH_RESULT,
    PUSH_UNINSTALL,
    PUSH_UPDATES,
    SECTION_NOTIFY,
    SECTION_STEAM,
)
from .parts import Part, start_task
from .presence import PcPresence
from .pc import UNNAMED, PcNames
from .no_space import NoSpace
from .push import TAG_AGENT, TAG_MOVE, TAG_PROGRESS, TAG_STALLED, TAG_UNINSTALL, TAG_UPDATES, send_push, wanted
from .space import drive_label, drives_of
from .steam_events import EVENT_MOVED, EVENT_UNINSTALLED, fire, game
from .steam_jobs import Overflow, SteamJobs
from .steam_owned import SteamOwned
from .steam_progress import SteamProgress
from .steam_updates import EVENT_CHECK_DONE, SteamUpdates
from .texts import Texts
from .values import id_list, known_name, number, whole

_LOGGER = logging.getLogger(__name__)

NOT_FROM = (STATE_UNKNOWN, STATE_UNAVAILABLE)  # HA's MQTT connection is down, or nothing known yet
STALL_TICK = timedelta(minutes=10)
STALL_REPEAT = timedelta(minutes=30)
AGENT_SILENT_AFTER = timedelta(minutes=10)  # PC on, agent quiet this long: push
AGENT_TICK = timedelta(minutes=1)
ACTION_INSTALL_ALL = "STEAM_ENGINE_INSTALL_ALL"
STALL_PHASES = ("verify", "commit", "prepare")  # phases of install.ps1 with their own text
MOVE_PHASES = ("copy", "sync", "switch", "verify", "cleanup", "rollback")  # phases of move.ps1
MOVE_STEP = 10  # a progress push of a move on each step of this many percent


class SteamNotify(Part):
    """Sends the Steam job pushes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        options: dict[str, dict],
        presence: PcPresence,
        progress: SteamProgress,
        jobs: SteamJobs,
        updates: SteamUpdates,
        owned: SteamOwned,
        agent: AgentEntities,
        names: PcNames = UNNAMED,
    ) -> None:
        super().__init__(hass)
        self.names = names
        self._entry = entry
        self.tx = Texts(hass)
        self.agent = agent
        self.presence = presence
        self.progress = progress
        self.jobs = jobs
        self.updates = updates
        self.owned = owned
        self.notify: dict[str, Any] = options[SECTION_NOTIFY]
        self.quiet_auto = bool(self.notify[CONF_QUIET_AUTO_UPDATES])
        self.link: str = options[SECTION_STEAM][CONF_STEAM_LINK]
        self.stalled_seconds: int = options[SECTION_STEAM][CONF_STALLED_MINUTES] * 60
        self.agent_link: str = options[SECTION_NOTIFY][CONF_LINK]
        self._stopped = False
        self._stall_sent: datetime | None = None
        self._agent_warned = False
        self._reporting_updates = False
        # The running job, from its start: appid -> first install (for the progress text), and the
        # reason code of each game that failed (for the result)
        self._job_new: dict[str, bool] = {}
        self._reasons: dict[str, str] = {}
        # The move pushed last: (started, phase, percent step), so each step is pushed once. After a
        # restart of HA only its started is known (kept in steam_jobs.py's store)
        self._move_pushed: tuple[str, str, int] | None = (jobs.move_pushed, "", -1) if jobs.move_pushed else None
        self.no_space = NoSpace(self)

    # --- lifecycle -------------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        self._unsubs.append(self.progress.async_add_job_listener(self._on_job_state))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_stall_tick, STALL_TICK))
        self._unsubs.append(self.presence.async_add_listener(self._on_agent_change))
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.agent.agent_online, self.agent.steam_running], self._on_agent_change
            )
        )
        self._unsubs.append(async_track_time_interval(self.hass, self._on_agent_tick, AGENT_TICK))
        self._unsubs.append(self.hass.bus.async_listen("mobile_app_notification_action", self._on_push_action))
        self._unsubs.append(self.hass.bus.async_listen(EVENT_CHECK_DONE, self._on_check_done))
        start_task(self.hass, self._entry, self._subscribe_uninstall(), "uninstall subscribe")
        self._unsubs.append(self.jobs.move.async_add_move_listener(self._on_move))
        self._unsubs.append(self.jobs.move.async_add_listener(self._on_move_progress))

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        super().async_stop()

    async def _subscribe_uninstall(self) -> None:
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            _LOGGER.warning("MQTT not available, no uninstall result pushes")
            return
        unsub = await mqtt.async_subscribe(self.hass, self.names.uninstall_result_topic, self._on_uninstall_result)
        if self._stopped:
            unsub()
        else:
            self._unsubs.append(unsub)

    # --- helpers ---------------------------------------------------------------------------

    def _push(self, kind: str | None, data: dict[str, Any]) -> None:
        send_push(self.hass, self.notify, data, self.names, kind)

    def _clear(self, tag: str) -> None:
        self._push(None, {"message": "clear_notification", "data": {"tag": tag}})

    def _quiet(self, appids: Any) -> bool:
        """A job of these games stays quiet: all of them came from the automatic updates, and the
        option is on or all of them are muted."""
        ids = set(id_list(appids))
        if not ids or not ids <= self.jobs.auto_quiet:
            return False
        return self.quiet_auto or ids <= set(self.jobs.muted)

    def _library(self) -> list[dict[str, Any]]:
        return library_games(self.hass, self.agent) or []

    def _name_source(self) -> list[dict[str, Any]]:
        return self._library() + list(self.owned.missing)

    def _names(self, source: list[dict[str, Any]], appids: Any) -> list[str]:
        return [known_name(source, i) or self.tx("game_fallback", appid=i) for i in id_list(appids)]

    # --- status changes --------------------------------------------------------------------

    @callback
    def _on_job_state(self, old: State | None, new: State, live: bool) -> None:
        """A change of the job state; only news is told (steam_progress.py: not an older end read
        again after a restart of HA or an MQTT reconnect). An end after an end is a new end."""
        if not live:
            return
        state = new.state
        if old is None or old.state != state or state in protocol.ENDED:
            if state == protocol.STARTED:
                self._started(new.attributes)
            elif state in protocol.FINISHED:
                self._done(new.attributes)
            elif state == protocol.ABORTED:
                self._aborted()
        if state == protocol.ERROR and new.attributes.get("appid") is not None:
            self._reasons[str(new.attributes["appid"])] = str(new.attributes.get("reason_code") or "")
        if state == protocol.DOWNLOADING and (
            old is None or old.attributes.get("percent") != new.attributes.get("percent")
        ):
            self._progress(old, new)

    def _end_stall(self) -> None:
        """A new job or the end of one: the stall push is over."""
        if self._stall_sent is not None:
            self._stall_sent = None
            self._clear(TAG_STALLED)

    def _started(self, attrs: dict[str, Any]) -> None:
        """Start: indeterminate bar with the game names."""
        self._end_stall()
        source = self._name_source()
        known = {str(g.get("appid")) for g in self._library()}
        jobs = id_list(attrs.get("appids"))
        self._job_new = {i: i not in known for i in jobs}
        self._reasons = {}
        if self._quiet(jobs):
            return
        names = self._names(source, jobs)
        new_count = sum(1 for is_new in self._job_new.values() if is_new)
        count = len(names)
        if new_count == 0:
            title = self.tx.n("started_update", count)
        elif new_count == count:
            title = self.tx.n("started_install", count)
        else:
            title = self.tx("started_mixed", count=count)
        self._push(
            PUSH_PROGRESS,
            {
                "title": f"Steam: {title}",
                "message": ", ".join(names),
                "data": {
                    "tag": TAG_PROGRESS,
                    "channel": self.tx("channel_steam"),
                    "notification_icon": "mdi:steam",
                    "progress": 0,
                    "progress_max": 100,
                    "progress_indeterminate": True,
                    "alert_once": True,
                    "sticky": True,
                    "clickAction": self.link,
                },
            }
        )

    def _progress(self, old: State | None, new: State) -> None:
        """Progress bar, only on a new 5 % step (the first state of the next game counts as one)."""
        attrs = new.attributes
        now_pct = number(attrs.get("percent"), 0)
        was_downloading = old is not None and old.state == protocol.DOWNLOADING
        old_pct = number(old.attributes.get("percent"), -10) if was_downloading else -10
        if now_pct // 5 == old_pct // 5:
            return
        appid = str(attrs.get("appid"))
        if self._quiet(self.jobs.job or [appid]):
            return
        # Install or update as known at the start; after a restart of HA from the library
        is_new = self._job_new.get(appid)
        if is_new is None:
            is_new = not any(str(g.get("appid")) == appid for g in self._library())
        pct = whole(now_pct)
        count = int(number(attrs.get("count"), 1))
        message = self.tx("progress_install" if is_new else "progress_update", pct=pct)
        if count > 1:
            message += self.tx("progress_position", position=attrs.get("position"), count=count)
        self._push(
            PUSH_PROGRESS,
            {
                "title": known_name(self._name_source(), appid) or self.tx("game_fallback", appid=appid),
                "message": message,
                "data": {
                    "tag": TAG_PROGRESS,
                    "channel": self.tx("channel_steam"),
                    "notification_icon": "mdi:progress-download",
                    "progress": pct,
                    "progress_max": 100,
                    "alert_once": True,
                    "sticky": True,
                    "clickAction": self.link,
                },
            }
        )

    def _with_reason(self, name: str, code: str) -> str:
        return self.tx("name_reason", name=name, reason=self.tx.reason(code)) if code else name

    def _done(self, attrs: dict[str, Any]) -> None:
        """End: result with the game names; games without space get a push of their own
        (no_space.py), also when result pushes are not chosen or the job is quiet. New purchases
        that go to the overflow drive now (steam_jobs.overflow_now) are left out: the result of
        that try tells where they went and why (steam_jobs.overflowed), installed or not."""
        self._end_stall()
        source = self._name_source()
        library = self._library()
        retry = self.jobs.overflow_now
        overflowed = self.jobs.overflowed
        ok_ids = id_list(attrs.get("ok"))
        new_ids = set(id_list(attrs.get("new")))
        # A game's own reason, else the one of the whole job (it stopped, or waited too long)
        job_code = str(attrs.get("reason_code") or "")
        failed_ids = [i for i in id_list(attrs.get("failed")) if i not in retry]
        failed = [
            self._with_reason(name, self._reasons.get(appid) or job_code)
            for appid, name in zip(failed_ids, self._names(source, failed_ids))
        ]
        in_client = self._names(source, attrs.get("skipped"))
        game_running = self._names(source, attrs.get("game_running"))
        # Their try on the overflow drive did not install them: say why they went there
        tried = [
            i
            for i in dict.fromkeys(failed_ids + id_list(attrs.get("skipped")) + id_list(attrs.get("game_running")))
            if i in overflowed
        ]
        no_space_raw = [
            k for k in (attrs.get("no_space") or []) if isinstance(k, dict) and str(k.get("appid")) not in retry
        ]
        drives = drives_of(self.hass, self.names)
        no_space = [
            self.tx(
                "no_space_item",
                name=known_name(source, k.get("appid")) or self.tx("game_fallback", appid=k.get("appid")),
                needed=whole(number(k.get("needed_gb"), 0)),
                free=whole(number(k.get("free_gb"), 0)),
                drive=drive_label(k.get("drive"), drives),
            )
            for k in no_space_raw
        ]
        no_space_pushed = self.no_space.job_ended(
            no_space_raw, ok_ids, {str(g.get("appid")) for g in library}, dict(self._job_new)
        )
        # The job list still holds the job's games here (steam_jobs.py settles it after the pushes)
        if self._quiet(self.jobs.job or protocol.result_appids(attrs)):
            if not no_space_raw and not any(self.jobs.auto_tries_used(i) for i in failed_ids):
                # Went well, or a failed game gets its second try: nothing to tell
                return
        told = ok_ids or failed or in_client or game_running or no_space
        only_no_space = (
            no_space_pushed
            and wanted(self.notify, PUSH_PROBLEMS)
            and not (ok_ids or failed or in_client or game_running or tried)
        )
        if not wanted(self.notify, PUSH_RESULT) or (retry and not told) or only_no_space:
            # Nothing to tell when only games that go to the overflow drive are left: their job
            # follows at once. Only games without space: the push "not enough space" (no_space.py,
            # kind problems) says it all, a second push would only hide its buttons in a group.
            self._clear(TAG_PROGRESS)  # the progress bar must not stay
            return
        moved = [
            self._overflow_text("done_overflow", source, i, overflowed[i], drives) for i in ok_ids if i in overflowed
        ]
        notes = [self._overflow_text("overflow_note", source, i, overflowed[i], drives) for i in tried]
        new_ok = self._names(source, [i for i in ok_ids if i in new_ids and i not in overflowed])
        updated = self._names(source, [i for i in ok_ids if i not in new_ids and i not in overflowed])

        if failed:
            title = self.tx.n("done_failed", len(failed))
        elif not ok_ids and no_space:
            title = self.tx("done_no_space")
        elif not ok_ids and game_running:
            title = self.tx("done_game_running")
        elif not ok_ids and in_client:
            title = self.tx("done_in_client_title")
        elif (new_ok or moved) and not updated:
            title = self.tx.n("done_install", len(new_ok) + len(moved))
        elif not new_ok and not moved:
            title = self.tx.n("done_update", len(updated))
        else:
            title = self.tx("done_mixed", count=len(ok_ids))

        parts: list[str] = []
        if new_ok:
            parts.append(self.tx.n("done_installed", len(new_ok), names=", ".join(new_ok)))
        parts.extend(moved)
        if updated:
            parts.append(self.tx.n("done_updated", len(updated), names=", ".join(updated)))
        if failed:
            parts.append(self.tx("done_failed_list", names=", ".join(failed)))
        if in_client:
            parts.append(self.tx("done_in_client", names=", ".join(in_client)))
        if game_running:
            parts.append(self.tx("done_game_running_list", names=", ".join(game_running)))
        if no_space:
            parts.append(self.tx("done_no_space_list", items=", ".join(no_space)))
        parts.extend(notes)

        actions = [{"action": "URI", "title": self.tx("button_view"), "uri": self.link}]
        if failed:
            icon = "mdi:alert-circle"
        elif no_space:
            icon = "mdi:harddisk-remove"
        elif game_running and not ok_ids:
            icon = "mdi:gamepad-variant"
        elif in_client and not ok_ids:
            icon = "mdi:steam"
        else:
            icon = "mdi:check-circle"
        self._push(
            PUSH_RESULT,
            {
                "title": f"Steam: {title}",
                "message": " ".join(parts) if parts else self.tx("done_all"),
                "data": {
                    "tag": TAG_PROGRESS,
                    "channel": self.tx("channel_steam"),
                    "notification_icon": icon,
                    "sticky": False,
                    "clickAction": self.link,
                    "actions": actions,
                },
            }
        )

    def _overflow_text(
        self, base: str, source: list[dict[str, Any]], appid: str, overflow: Overflow, drives: list[dict[str, Any]]
    ) -> str:
        """done_overflow: "X is installed on E: (HDD), there was no space on F: (SSD) (overflow
        drive).", overflow_note: "X went to E: (HDD) (overflow drive) as ..." (not installed)."""
        return self.tx(
            overflow.text_key(base),
            name=known_name(source, appid) or self.tx("game_fallback", appid=appid),
            drive=drive_label(overflow.drive, drives),
            full=drive_label(overflow.full, drives),
        )

    def _aborted(self) -> None:
        """Remove the progress bar and say which games are still open (steam_jobs.py sends them
        again once no job runs)."""
        self._clear(TAG_PROGRESS)
        open_ids = list(dict.fromkeys(self.jobs.job + self.jobs.queue))
        if self._quiet(open_ids):
            return  # it goes on by itself
        message = self.tx("aborted_message")
        if open_ids:
            message += self.tx("aborted_open", ids=", ".join(self._names(self._name_source(), open_ids)))
        self._push(
            PUSH_PROBLEMS,
            {
                "title": self.tx("aborted_title"),
                "message": message,
                "data": {"tag": TAG_STALLED, "channel": self.tx("channel_steam"), "importance": "default"},
            }
        )

    # --- stalled download ------------------------------------------------------------------

    @callback
    def _on_stall_tick(self, _now: datetime) -> None:
        """A job without progress for the stall time (settings page). The agent and install.ps1 keep
        sending while they run; a job that ended unexpectedly is reported as aborted by the PC."""
        status = self.hass.states.get(self.agent.install_status)
        if not self.presence.on or status is None or not protocol.job_active(status.state, status.attributes):
            return
        stalled = self.progress.stalled_seconds()
        if stalled is None or stalled <= self.stalled_seconds:
            return
        now = dt_util.utcnow()
        if self._stall_sent is not None and now - self._stall_sent <= STALL_REPEAT:
            return
        self._stall_sent = now
        attrs = status.attributes
        phase = str(attrs.get("phase") or "")
        if phase not in ("", protocol.PHASE_DOWNLOAD):
            pct: Any = whole(number(attrs.get("phase_percent"), 0))
        else:
            pct = self.progress.percent
        package = self.tx("stall_package", position=attrs.get("position"), count=attrs.get("count"))
        rest = whole((BUSY_STALL_LIMIT.total_seconds() - stalled) / 60)
        phase_text = self.tx(f"stalled_phase_{phase}") if phase in STALL_PHASES else ""
        message = self.tx("stalled_message", minutes=whole(stalled / 60), phase=phase_text, pct=pct, package=package)
        message += self.tx("stalled_rest", minutes=rest) if rest > 0 else self.tx("stalled_free")
        _LOGGER.info("Steam download stalled for %s s", stalled)
        self._push(
            PUSH_PROBLEMS,
            {
                "title": self.tx("stalled_title"),
                "message": message,
                "data": {"tag": TAG_STALLED, "channel": self.tx("channel_steam"), "importance": "default"},
            }
        )

    # --- move ------------------------------------------------------------------------------

    def _move_name(self, data: dict[str, Any]) -> str:
        appid = data.get("appid")
        return str(data.get("name") or known_name(self._name_source(), appid) or self.tx("game_fallback", appid=appid))

    def _move_push(self, kind: str | None, title: str, message: str, icon: str, extra: dict[str, Any] | None = None) -> None:
        self._push(
            kind,
            {
                "title": title,
                "message": message,
                "data": {
                    "clickAction": self.link,
                    "tag": TAG_MOVE,
                    "channel": self.tx("channel_steam"),
                    "notification_icon": icon,
                    **(extra or {}),
                },
            },
        )

    def _set_move_pushed(self, pushed: tuple[str, str, int] | None) -> None:
        """The move whose progress push is out; its started is kept over a restart of HA."""
        self._move_pushed = pushed
        started = pushed[0] if pushed is not None else ""
        if started != self.jobs.move_pushed:
            start_task(self.hass, self._entry, self.jobs.async_set_move_pushed(started), "move push")

    @callback
    def _on_move(self, kind: str, data: dict[str, Any], live: bool) -> None:
        # The move whose progress is on the phone (not told yet when its end comes retained)
        pushed = self._move_pushed is not None and self._move_pushed[0] == str(data.get("started") or "")
        if kind == "silent":
            if pushed:
                self._set_move_pushed(None)
                self._clear(TAG_MOVE)  # nothing more will come: the progress push must not stay
            self.no_space.move_silent(str(data.get("appid") or ""))
            return
        if not live and not (kind == "ended" and pushed):
            return  # an end read again after a restart of HA or an MQTT reconnect
        if kind == "started":
            self._move_started(data)
        elif kind == "ended":
            self._move_ended(data)

    def _move_started(self, data: dict[str, Any]) -> None:
        drives = drives_of(self.hass, self.names)
        total = data.get("total_gb")
        size = self.tx("move_size", size=whole(number(total, 0))) if number(total, 0) > 0 else ""
        self._set_move_pushed((str(data.get("started") or ""), str(data.get("phase") or ""), 0))
        self._move_push(
            PUSH_PROGRESS,
            self.tx("move_started_title", name=self._move_name(data)),
            self.tx(
                "move_started_message",
                source=drive_label(data.get("from"), drives),
                target=drive_label(data.get("drive"), drives),
                size=size,
            ),
            "mdi:folder-move-outline",
            {"progress": 0, "progress_max": 100, "progress_indeterminate": True, "alert_once": True, "sticky": True},
        )

    @callback
    def _on_move_progress(self) -> None:
        """A running move: a progress push on a new phase or a new MOVE_STEP of the copy."""
        data = self.jobs.move.running
        if data is None or self._move_pushed is None:
            return  # no move, or one whose start was not seen (its start push says nothing then either)
        started, phase = str(data.get("started") or ""), str(data.get("phase") or "")
        pct = max(0, min(100, whole(number(data.get("percent"), 0))))
        step = pct // MOVE_STEP
        if started != self._move_pushed[0] or (phase, step) == self._move_pushed[1:]:
            return
        self._move_pushed = (started, phase, step)
        phase_text = self.tx(f"move_phase_{phase}") if phase in MOVE_PHASES else phase
        self._move_push(
            PUSH_PROGRESS,
            self.tx("move_started_title", name=self._move_name(data)),
            self.tx("move_progress", phase=phase_text, pct=pct),
            "mdi:folder-move-outline",
            {"progress": pct, "progress_max": 100, "alert_once": True, "sticky": True},
        )

    def _move_ended(self, data: dict[str, Any]) -> None:
        """The end of a move: the event, and the result push. A move started from the push "not
        enough space" (no_space.py) adds what follows for the game that waits for it; that push
        then comes whatever kinds are chosen (the answer to a button)."""
        self._set_move_pushed(None)
        ok = data.get("state") == "ok"
        name = self._move_name(data)
        fire(
            self.hass,
            EVENT_MOVED,
            {
                **game(data.get("appid"), name),
                "success": ok,
                "reason": None if ok else data.get("reason_code"),
                "warning": data.get("warning"),
                "from": data.get("from"),
                "drive": data.get("drive"),
                "new_library": bool(data.get("new_library")) if ok else False,
                "source_free_gb": data.get("source_free_gb"),
                "target_free_gb": data.get("target_free_gb"),
            },
            self.names,
        )
        then = self.no_space.moved(data)
        kind = None if then else PUSH_RESULT
        if not wanted(self.notify, kind):
            self._clear(TAG_MOVE)  # the progress push must not stay
            return
        drives = drives_of(self.hass, self.names)
        source = drive_label(data.get("from"), drives) if data.get("from") else "?"
        target = drive_label(data.get("drive"), drives) if data.get("drive") else "?"
        if ok:
            message = self.tx("moved_message", name=name, target=target)
            if data.get("new_library"):
                message += self.tx("moved_new_library")
            if data.get("target_free_gb") is not None and data.get("source_free_gb") is not None:
                message += self.tx(
                    "moved_free",
                    target=drive_label(data.get("drive"), ()),
                    target_free=data.get("target_free_gb"),
                    source=drive_label(data.get("from"), ()),
                    source_free=data.get("source_free_gb"),
                )
            if data.get("warning") == "source_not_deleted":
                message += self.tx("moved_source_left", source=source)
            self._move_push(kind, self.tx("moved_title"), message + then, "mdi:folder-move-outline")
            return
        code = str(data.get("reason_code") or "")
        if code == "rollback_failed":
            message = self.tx("move_rollback_failed_message", name=name, target=target, source=source)
        else:
            reason = self.tx.reason(code or "unexpected_error", "move_")
            if code == "no_space" and data.get("needed_gb") is not None and data.get("target_free_gb") is not None:
                reason += self.tx(
                    "move_no_space",
                    needed=whole(number(data.get("needed_gb"), 0)),
                    free=whole(number(data.get("target_free_gb"), 0)),
                    target=target,
                )
            message = self.tx("move_failed_message", name=name, reason=reason)
            if data.get("warning") == "source_not_deleted":
                # not_installed from the journal: the game was uninstalled meanwhile, its old folder stays
                message += self.tx("move_folder_left", source=source)
        self._move_push(kind, self.tx("move_failed_title"), message + then, "mdi:folder-alert-outline")

    # --- uninstall result and the "make space" buttons -------------------------------------

    @callback
    def _on_uninstall_result(self, msg: mqtt.ReceiveMessage) -> None:
        """Result of uninstall.ps1. The Steam agent updates the library by itself."""
        try:
            data = json.loads(msg.payload)
        except (TypeError, ValueError):
            _LOGGER.warning("Uninstall result is no JSON: %s", msg.payload)
            return
        if not isinstance(data, dict):
            return
        ok = data.get("result") == "ok"
        name = data.get("name") or data.get("appid")
        fire(
            self.hass,
            EVENT_UNINSTALLED,
            {
                **game(data.get("appid"), name),
                "success": ok,
                "reason": None if ok else data.get("reason"),
                "free_gb": data.get("free_gb"),
            },
            self.names,
        )
        then = self.no_space.uninstalled(str(data.get("appid")), ok, str(name), data.get("reason"), data.get("free_gb"))
        if ok:
            message = self.tx("uninstalled_message", name=name)
            if data.get("free_gb") is not None:
                message += self.tx("uninstalled_free", free=data.get("free_gb"))
            message += then
        else:
            message = f"{name}: {self.tx.reason(data.get('reason'), 'uninstall_')}"
        self._push(
            None if then else PUSH_UNINSTALL,  # the answer to "Yes, delete" of a push "not enough space"
            {
                "title": self.tx("uninstalled_title" if ok else "uninstall_failed_title"),
                "message": message,
                "data": {
                    "clickAction": self.link,
                    "tag": TAG_UNINSTALL,
                    "channel": self.tx("channel_steam"),
                    "notification_icon": "mdi:delete-outline" if ok else "mdi:delete-alert-outline",
                },
            }
        )

    @callback
    def _on_push_action(self, event: Event) -> None:
        """Buttons of the pushes: "Install all" of the update push; those of the "not enough
        space" pushes (no_space.py), by the prefix of their action."""
        action = self.names.action_of(str(event.data.get("action") or ""))
        if action is None:
            return  # another PC's button
        if action == ACTION_INSTALL_ALL:
            start_task(self.hass, self._entry, self._install_all(), "install all")
            return
        self.no_space.on_action(action)

    async def _install_all(self) -> None:
        """All open updates, except those a job has or that are queued already, and muted games
        (the push does not name them). Automatic updates waiting for the active hours are included:
        asked for by hand, they go at once."""
        open_ids = set(self.jobs.job) | (set(self.jobs.queue) - set(self.jobs.deferred)) | set(self.jobs.muted)
        ids = [str(g.get("appid")) for g in self.updates.outdated if str(g.get("appid")) not in open_ids]
        _LOGGER.info("Install all updates from push: %s", ids)
        self._clear(TAG_UPDATES)  # answered: the job's pushes follow under their own tag
        self._push(
            None,  # the answer to a button
            {
                "title": self.tx("install_all_title"),
                "message": self.tx.n("install_all_message", len(ids)) if ids else self.tx("install_all_nothing"),
                "data": {
                    "tag": TAG_PROGRESS,
                    "channel": self.tx("channel_steam"),
                    "clickAction": self.link,
                    "notification_icon": "mdi:steam",
                },
            }
        )
        if ids:
            await self.jobs.async_install(ids)

    # --- silent agent ----------------------------------------------------------------------

    def _agent_silent_since(self) -> datetime | None:
        """Since when the PC is on while the agent is silent: offline (its last will), or online
        with its state expired (it hangs). None otherwise, and while HA's MQTT connection is down
        (agent_online unavailable): then nothing is known about the agent, as in issues.py."""
        if not self.presence.on:
            return None
        online = self.hass.states.get(self.agent.agent_online)
        if online is None or online.state in NOT_FROM:
            return None
        if online.state == "on":
            running = self.hass.states.get(self.agent.steam_running)
            if running is None or running.state not in NOT_FROM:
                return None
            since = running.last_changed
        else:
            since = online.last_changed
        # After a wake only the time since the PC came on counts
        return max(self.presence.since, since)

    @callback
    def _on_agent_change(self, _event: Event | None = None) -> None:
        if self._agent_warned and self._agent_silent_since() is None:
            self._agent_warned = False
            self._clear(TAG_AGENT)

    @callback
    def _on_agent_tick(self, _now: datetime) -> None:
        since = self._agent_silent_since()
        if self._agent_warned or since is None or dt_util.utcnow() - since < AGENT_SILENT_AFTER:
            return
        self._agent_warned = True
        _LOGGER.info("Steam agent silent while the PC is on (since %s)", since)
        self._push(
            PUSH_PROBLEMS,
            {
                "title": self.tx("agent_silent_title"),
                "message": self.tx("agent_silent_message"),
                "data": {
                    "tag": TAG_AGENT,
                    "channel": self.tx("channel_steam"),
                    "notification_icon": "mdi:lan-disconnect",
                    "clickAction": self.agent_link,
                },
            }
        )

    # --- new updates -----------------------------------------------------------------------

    @callback
    def _on_check_done(self, event: Event) -> None:
        if event.data.get("pc", "") != self.names.pc_name:
            return  # another PC's check
        if self._reporting_updates:
            return  # one report at a time; a check that ends meanwhile is left to the next one
        start_task(self.hass, self._entry, self._report_updates(), "report updates")

    async def _report_updates(self) -> None:
        """Push only when a game is new since the last push, but then list all open updates. Games
        with automatic updates are only counted (steam_jobs.py starts them; outside the active hours
        with the time they start), unless their automatic tries for this public state are used up:
        then they are listed like the others. Games a job, the queue or a running game holds are
        left out (announced after their job if still open), muted games always. The push has a tag
        of its own (TAG_UPDATES), so a job's pushes never take it away while its games count as
        announced; it goes once no game is left to announce. Not while a job runs: the check after
        its end announces what is still open then."""
        self._reporting_updates = True
        try:
            if self.jobs.job_running():
                return
            held = set(self.jobs.job) | set(self.jobs.queue) | set(self.jobs.waiting)
            all_outdated = {str(g.get("appid")) for g in self.updates.outdated} - set(self.jobs.muted)
            outdated_ids = all_outdated - held
            outdated = [g for g in self._library() if str(g.get("appid")) in outdated_ids]
            auto = set(self.jobs.auto_update) - self.jobs.auto_given_up()
            auto_count = sum(1 for g in outdated if str(g.get("appid")) in auto)
            rest = sorted((g for g in outdated if str(g.get("appid")) not in auto), key=lambda g: str(g.get("name")))
            rest_ids = [str(g.get("appid")) for g in rest]
            reported = self.jobs.reported
            new = [i for i in rest_ids if i not in reported]
            if not new:
                if reported and not rest_ids:
                    self._clear(TAG_UPDATES)  # all announced updates are done (or muted)
                await self.jobs.async_set_reported(i for i in reported if i in rest_ids)
                return
            message = ", ".join(str(g.get("name")) for g in rest)
            hours = self.jobs.hours
            if self.quiet_auto:
                pass  # the automatic updates are not mentioned
            elif not hours.is_open():
                # Automatic updates waiting for the active hours are held in the queue: count them too
                auto_count += len(all_outdated & set(self.jobs.deferred))
                if auto_count:
                    message += self.tx.n("updates_auto_deferred", auto_count, time=hours.start_text())
            elif auto_count:
                message += self.tx.n("updates_auto", auto_count)
            self._push(
                PUSH_UPDATES,
                {
                    "title": self.tx.n("updates_title", len(rest)),
                    "message": message,
                    "data": {
                        "clickAction": self.link,
                        "tag": TAG_UPDATES,
                        "channel": self.tx("channel_steam"),
                        "actions": [
                            {"action": ACTION_INSTALL_ALL, "title": self.tx("updates_install_all")},
                            {"action": "URI", "title": self.tx("button_view"), "uri": self.link},
                        ],
                    },
                }
            )
            await self.jobs.async_set_reported(rest_ids)
        finally:
            self._reporting_updates = False
