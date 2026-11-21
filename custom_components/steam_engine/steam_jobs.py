"""Steam jobs: install/update, uninstall, move and launch through the Steam agent, with a queue, and
the automatic updates.

The lists live in the integration's store (.storage/steam_engine.lists): job (games handed to the
PC whose end was not seen yet), queue (games waiting for the running job), after_game (games not
started because a game was running, sent once it is closed), reported (updates already announced
by push, steam_notify.py), auto_update (games whose updates start without asking), muted (games
whose updates are not announced and whose automatic jobs stay quiet, steam_notify.py), auto_tries
(automatic jobs per game and public state), auto_added (games of the queue or of after_game that
automatic work put there, see below), auto_added_new (those of them that are new purchases) and
auto_quiet (games the automatic updates handed out and nobody asked for by hand since: with the
option "automatic updates quiet" their jobs push only on a failure, steam_notify.py), targets
(the drive a game asked for by hand is installed to, see below), purchased (new purchases that
may go to the overflow drive, see below), job_id (the job ID of the job handed to the PC last, or
of the job seen running since), end_id (the job whose end was told last; both for
steam_progress.py) and move_pushed (the move whose progress push is out, steam_notify.py).

A job ends with done or done_with_errors, whose result lists name its games, or with aborted. The
job list keeps every game handed to the PC until an end names it; what is open goes out again (with
the queue) once no job runs and the agent is online. So an interrupted job (aborted, or a PC that
went off in the middle) goes on the next time the PC is on, as Steam itself goes on with a download
it did not finish, and a hand-over a reload stopped goes out again. Each hand-over gives the job a
new job ID, which the PC sends back in every state; a job seen running (a state that came just now)
is the one whose end counts, its job ID is taken over. Only an end that is news counts
(steam_progress.py): one that came just now, or the end of that job that came while HA was not
listening (a restart of HA, an MQTT reconnect), each once. An older end read again leaves the lists
as they are, also when it names games of the job list (the next job of the same game). The PC side
leaves out games of a job that runs or just ended, so a command sent again does no harm.

Active hours (active_hours.py): automatic work, that is the automatic updates (with their second
try) and new purchases installed by themselves, starts only while the window is open. Outside it
such games go into the queue, marked in auto_added, and wait there without waking the PC; a job
started by hand meanwhile leaves them in the queue. When the window opens (or at a start of Home
Assistant inside it) what waited goes out as one job, waking the PC; games that need nothing any
more by then (installed and up to date, automatic updates switched off, or an update of a game
uninstalled meanwhile) are dropped. The PC is woken before they leave the queue: if it does not
come, they stay there and wait for the agent inside the window, or for its next start. Games added
by hand never wait, and a job already handed to the PC (the job list) goes on at any time, as a
download Steam started does. While the window is switched off (or all day) nothing is marked.

Target drives: install with a drive letter (the card's choice, the action's field "drive") stores
it for the game; the command then carries "<appid>@<letter>" (protocol.install_command), and the PC
installs a first install into the Steam library on that drive (an installed game stays where it
is). A game keeps its target while it is in the job list, the queue or after_game, so it survives a
resend, a restart of HA and a wait for a running game; once it leaves all three, the target goes
(_set_list). Without a drive (automatic work, the buttons, the pushes) a stored target is never
changed and a game without one goes to Steam's default library. A new drive replaces the stored one
until the PC accepted the game (in the job list while a job runs); after that it stays.

Overflow drive (option overflow_drive): a new purchase installed by itself (automatic, purchase) or
by the button "Install on <PC>" of the purchase push is marked in purchased while it is in the job
list, the queue or after_game (asked for by hand otherwise, the mark goes). When its job ends with
the game without space on the default drive (no_space) or with that drive not there
(drive_unavailable), and the game has no target, it is sent once more with the overflow drive as
its target, at once: if that is another drive than the one that was full, the PC reports it in
drives, and it has room for the game by the rule of the install job (needed_gb of the no_space
entry; for drive_unavailable the size game_size.py knows, unknown: the PC decides; several games
of one end share its free space). The target set for that try is the loop guard: a game with a
target never goes to the overflow drive. The plan is made when the end comes (overflow_now, read
by the result push, which leaves these games out), the games sent there are remembered in memory
until their next end that is not 'a game was running' (overflowed: the result push says where they
went and why, whatever the result). If the overflow drive has no room either, the push "not enough
space" follows as for any game.

Moves (async_move): one game to another drive, by hand only. The PC runs one Steam job at a time, so
installs wait in the queue while a move runs (steam_move.py tells), and a move is not sent while an
install job runs or is being handed over, or a game runs (Steam cannot be closed then). After a
successful move the agent sends the library by itself; if it still shows the game on the old drive
after MOVE_LIBRARY_CHECK, it is asked for once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import secrets
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import protocol
from .active_hours import ActiveHours
from .agent_entities import AgentEntities, async_send_agent_command, library_games
from .const import (
    CONF_LAUNCH_UNLOCKED,
    CONF_OVERFLOW_DRIVE,
    CONF_STEAM_LINK,
    LAUNCH_PENDING_VALID,
    PUSH_PROBLEMS,
    SECTION_NOTIFY,
    SECTION_PC,
    SECTION_STEAM,
    STORAGE_VERSION,
    UNINSTALL_GUARD,
)
from .game_size import known_game_bytes
from .parts import Part, start_task, wait_until
from .pc_power import PcPower
from .pc import UNNAMED, PcNames
from .push import TAG_LAUNCH, TAG_MOVE, TAG_PROGRESS, TAG_UNINSTALL, send_push
from .space import drive_letter, drives_of, find_drive, needed_gb
from .steam_move import SteamMove
from .steam_progress import SteamProgress
from .steam_updates import EVENT_CHECK_DONE, SteamUpdates
from .texts import Texts
from .values import number

_LOGGER = logging.getLogger(__name__)

WAKE_TIMEOUT = timedelta(minutes=6)
SEND_TRIES = 3
SEND_WAIT = timedelta(seconds=60)  # per try: the agent must report a state of the job
# A clear of the launch note the broker did not take is sent again after this time
LAUNCH_NOTE_RETRY = timedelta(minutes=1)
QUEUE_DELAY = 20  # seconds after the end of a job before the queue moves on (unless a job starts)
# Seconds after an end naming a running game the agent does not show yet: it reads the running game
# once a round (20 s), so it shows it by then; one closed before that it never shows
GAME_SEEN_WAIT = 30
AUTO_TRIES = 2  # automatic jobs per game and public state (a second try after a failed one)
# After a successful move: the library is asked for if it does not show the game on its new drive by then
MOVE_LIBRARY_CHECK = timedelta(seconds=90)
# Lists in the store
JOB = "job"
QUEUE = "queue"
REPORTED = "reported"
AUTO_UPDATE = "auto_update"
MUTED = "muted"  # games whose updates are not announced (steam_notify.py)
WAITING = "after_game"
AUTO_TRIES_KEY = "auto_tries"  # {appid: [public state, jobs sent]}
AUTO_ADDED = "auto_added"  # games of QUEUE or WAITING added by automatic work (active hours)
AUTO_NEW = "auto_added_new"  # of AUTO_ADDED: new purchases (the others are updates)
AUTO_QUIET = "auto_quiet"  # games of the automatic updates not asked for by hand since
TARGETS_KEY = "targets"  # {appid: drive letter} of games in JOB, QUEUE or WAITING
PURCHASED = "purchased"  # new purchases of JOB, QUEUE or WAITING that may go to the overflow drive
JOB_ID_KEY = "job_id"  # the job handed to the PC last or seen running since (steam_progress.job_id)
END_ID_KEY = "end_id"  # the job whose end was told last (steam_progress.end_id)
MOVE_PUSHED = "move_pushed"  # "started" of the move whose progress push is out (steam_notify.py)
OPEN_LISTS = (JOB, QUEUE, WAITING)  # a game in one of them keeps its target
SORTED_LISTS = (REPORTED, AUTO_UPDATE, MUTED)  # the others keep the order of the requests


@dataclass(frozen=True)
class Overflow:
    """A new purchase sent (again) to the overflow drive."""

    drive: str  # the overflow drive, a lower-case letter
    full: str  # the drive it found full or not there ("F:"), "" when not known
    reason: str  # no_space or drive_unavailable

    def text_key(self, base: str) -> str:
        """The text for this case: <base>[_default]_no_space or ..._unavailable (_default when the
        drive that was full or not there is not known)."""
        case = "unavailable" if self.reason == "drive_unavailable" else "no_space"
        return f"{base}_{case}" if self.full else f"{base}_default_{case}"


def parse_appids(value: Any) -> list[str]:
    """App IDs from a comma separated text or a list; anything that is not an app ID (ASCII digits,
    protocol.valid_appid) is dropped."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    return [item for item in (str(i).strip() for i in items) if protocol.valid_appid(item)]


class SteamJobs(Part):
    """Hands install, uninstall and launch jobs to the Steam agent, waking the PC when needed."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        options: dict[str, dict],
        power: PcPower,
        progress: SteamProgress,
        updates: SteamUpdates,
        agent: AgentEntities,
        hours: ActiveHours,
        move: SteamMove,
        names: PcNames = UNNAMED,
    ) -> None:
        super().__init__(hass)
        self.names = names
        self._entry = entry
        self.move = move
        self.tx = Texts(hass)
        self.agent = agent
        self.hours = hours
        self.power = power
        self.progress = progress
        self.updates = updates
        self.notify: dict[str, Any] = options[SECTION_NOTIFY]
        self.link: str = options[SECTION_STEAM][CONF_STEAM_LINK]
        # Start a game on a switched-off PC without the lock at logon (see async_launch)
        self.launch_unlocked = bool(options[SECTION_PC][CONF_LAUNCH_UNLOCKED])
        overflow = str(options[SECTION_STEAM].get(CONF_OVERFLOW_DRIVE) or "")
        self.overflow_drive = overflow if protocol.valid_drive(overflow) else ""
        self._note_timer: CALLBACK_TYPE | None = None
        self._note_until: datetime | None = None  # end of a note that may still be on the broker
        self._lock = asyncio.Lock()  # one job hand-over at a time; later calls wait their turn
        self._queue_timer: CALLBACK_TYPE | None = None
        self._game_timer: CALLBACK_TYPE | None = None  # see _wait_or_queue
        self._was_running = False
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, names.store_key)
        self._lists: dict[str, list[str]] = {
            JOB: [],
            QUEUE: [],
            REPORTED: [],
            AUTO_UPDATE: [],
            MUTED: [],
            WAITING: [],
            AUTO_ADDED: [],
            AUTO_NEW: [],
            AUTO_QUIET: [],
            PURCHASED: [],
        }
        self._auto_tries: dict[str, tuple[str, int]] = {}
        self._targets: dict[str, str] = {}
        self._move_pushed = ""
        self._draining = False  # _install_waiting runs
        self._joining: frozenset[str] = frozenset()  # games async_install is about to add to a list
        self._uninstall_sent: dict[str, datetime] = {}
        self._move_sent: dict[str, datetime] = {}  # against a double tap; dropped once the PC answers
        self._move_was_active = False
        self._library_timer: CALLBACK_TYPE | None = None
        # The reason code of each game of the running job that failed (live error states)
        self._fail_codes: dict[str, str] = {}
        # The games of the end just seen that go to the overflow drive (set before the pushes of
        # that end read it), and those sent there until their next end (in memory only)
        self.overflow_now: dict[str, Overflow] = {}
        self.overflowed: dict[str, Overflow] = {}

    # --- lifecycle -------------------------------------------------------------------------

    async def async_load(self) -> None:
        """Read the lists from the store; a list it does not know yet starts empty. An entry that is
        no app ID (protocol.valid_appid) is dropped: in the job list it would block every later
        install command."""
        data = await self._store.async_load() or {}
        for key in self._lists:
            items = data.get(key)
            self._lists[key] = parse_appids(items) if isinstance(items, list) else []
        tries = data.get(AUTO_TRIES_KEY)
        if isinstance(tries, dict):
            for appid, value in tries.items():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                    self._auto_tries[str(appid)] = (str(value[0]), value[1])
        targets = data.get(TARGETS_KEY)
        if isinstance(targets, dict):
            present = self._open_ids()
            self._targets = {
                str(appid): str(drive)
                for appid, drive in targets.items()
                if str(appid) in present and protocol.valid_drive(drive)
            }
        self._lists[PURCHASED] = [i for i in self._lists[PURCHASED] if i in self._open_ids()]
        self.progress.job_id = str(data.get(JOB_ID_KEY) or "")
        self.progress.end_id = str(data.get(END_ID_KEY) or "")
        self._move_pushed = str(data.get(MOVE_PUSHED) or "")

    @callback
    def async_start(self) -> None:
        self._unsubs.append(self.progress.async_add_job_listener(self._on_job_state))
        self._unsubs.append(self.progress.async_add_listener(self._on_progress))
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.agent.game_running, self.agent.agent_online], self._on_agent_or_game
            )
        )
        self._unsubs.append(self.hass.bus.async_listen(EVENT_CHECK_DONE, self._on_check_done))
        self._unsubs.append(self.hours.async_add_listener(self._on_hours))
        self._unsubs.append(self._cancel_queue_timer)
        self._unsubs.append(self._cancel_game_timer)
        self._unsubs.append(self._cancel_note_timer)
        self._unsubs.append(self.move.async_add_listener(self._on_move_change))
        self._unsubs.append(self.move.async_add_move_listener(self._on_move))
        self._unsubs.append(self._cancel_library_timer)
        self._move_was_active = self.move.active
        # A note for the lock script left by an earlier run (restart or reload during a game start,
        # or the option switched off since): nothing of this run set it
        start_task(self.hass, self._entry, self._clear_launch_note(at_start=True), "launch note")
        self._was_running = self.progress.job_running
        # The end found after a restart or reload is told by steam_progress.async_found_end once every
        # part listens, when it is news. What is open goes out once no job runs (also a hand-over a
        # reload stopped)
        if self._has_open():
            self._schedule_queue()
        # Automatic work that waited for the active hours, which are open now
        if self.hours.is_open() and self._held():
            start_task(self.hass, self._entry, self._start_held(), "active hours")
        # Games that waited for a game before a restart
        self._drain_waiting()
        # The updates sensor was set up before the active hours started (their next start was not
        # known yet): show what waits and until when
        if self.hours.limited:
            self._notify_listeners()

    # --- helpers ---------------------------------------------------------------------------

    @property
    def job(self) -> list[str]:
        return list(self._lists[JOB])

    @property
    def queue(self) -> list[str]:
        return list(self._lists[QUEUE])

    @property
    def waiting(self) -> list[str]:
        return list(self._lists[WAITING])

    @property
    def reported(self) -> list[str]:
        return list(self._lists[REPORTED])

    @property
    def auto_update(self) -> list[str]:
        return list(self._lists[AUTO_UPDATE])

    @property
    def muted(self) -> list[str]:
        """Games whose updates are not announced: no update push, not counted by the updates sensor,
        their automatic jobs stay quiet (steam_notify.py)."""
        return list(self._lists[MUTED])

    @property
    def targets(self) -> dict[str, str]:
        """The drive letter per game asked for with one (only games of the job, queue or after_game)."""
        return dict(self._targets)

    @property
    def purchased(self) -> list[str]:
        """New purchases of the job, the queue or after_game that may go to the overflow drive."""
        return list(self._lists[PURCHASED])

    @property
    def auto_quiet(self) -> set[str]:
        """Games the automatic updates handed out that nobody asked for by hand since."""
        return set(self._lists[AUTO_QUIET])

    @property
    def move_pushed(self) -> str:
        """The "started" of the move whose progress push is out (steam_notify.py), "" when none."""
        return self._move_pushed

    async def async_set_move_pushed(self, started: str) -> None:
        """Keep the move whose progress push is out ("" when none), also over a restart of HA."""
        if started != self._move_pushed:
            self._move_pushed = started
            await self._save()

    def auto_tries_used(self, appid: str) -> bool:
        """The automatic updates tried this game as often as they may for its public state."""
        state, tries = self._auto_tries.get(str(appid), ("", 0))
        return tries >= AUTO_TRIES and state == self.updates.fingerprints.get(str(appid), "")

    @property
    def deferred(self) -> list[str]:
        """Games of automatic work waiting for the active hours (none while they are open)."""
        return [] if self.hours.is_open() else self._held()

    def _held(self) -> list[str]:
        """Games of the queue and of after_game that automatic work added."""
        auto = set(self._lists[AUTO_ADDED])
        return [i for i in dict.fromkeys(self._lists[QUEUE] + self._lists[WAITING]) if i in auto]

    def _sendable_queue(self) -> list[str]:
        """The games of the queue that may go out now: outside the active hours not those of
        automatic work."""
        if self.hours.is_open():
            return self._list(QUEUE)
        auto = set(self._lists[AUTO_ADDED])
        return [i for i in self._lists[QUEUE] if i not in auto]

    def _has_open(self) -> bool:
        """Something may go out now: the rest of a job, or games of the queue."""
        return bool(self._lists[JOB] or self._sendable_queue())

    def job_running(self) -> bool:
        """A job runs on the PC as far as HA can tell: protocol.job_active with progress in the last
        6 h (steam_progress.py), the PC on and the agent online. New games do not wait for a job
        interrupted by a power cut or a crash: they go out together with what it left open."""
        return self.progress.job_running and self._agent_online()

    def _agent_online(self) -> bool:
        state = self.hass.states.get(self.agent.agent_online)
        return state is not None and state.state == "on"

    def game_running(self) -> bool:
        state = self.hass.states.get(self.agent.game_running)
        return state is not None and state.state == "on"

    def _playing(self) -> str:
        state = self.hass.states.get(self.agent.game_running)
        return str(state.attributes.get("appid") or "") if state is not None and state.state == "on" else ""

    def _list(self, key: str) -> list[str]:
        return list(self._lists[key])

    def _open_ids(self) -> set[str]:
        """The games of the job, the queue and after_game, and those async_install is adding to
        one: those that keep a target."""
        return set(self._joining).union(*(self._lists[key] for key in OPEN_LISTS))

    async def _save(self) -> None:
        await self._store.async_save(
            {
                **self._lists,
                AUTO_TRIES_KEY: {appid: [state, n] for appid, (state, n) in self._auto_tries.items()},
                TARGETS_KEY: dict(self._targets),
                JOB_ID_KEY: self.progress.job_id,
                END_ID_KEY: self.progress.end_id,
                MOVE_PUSHED: self._move_pushed,
            }
        )

    async def _set_list(self, key: str, items: Iterable[str]) -> None:
        if self._put(key, items):
            await self._save()
            self._notify_listeners()

    def _put(self, key: str, items: Iterable[str]) -> bool:
        """Set a list in memory only (with the marks and targets trimmed); True when it changed."""
        values = list(dict.fromkeys(str(i) for i in items))
        if key in SORTED_LISTS:
            values.sort()
        if values == self._lists[key]:
            return False
        self._lists[key] = values
        if key in (QUEUE, WAITING, AUTO_ADDED, AUTO_NEW):
            # Marks only for games still in the queue or waiting for a game
            present = set(self._lists[QUEUE]) | set(self._lists[WAITING])
            self._lists[AUTO_ADDED] = [i for i in self._lists[AUTO_ADDED] if i in present]
            marked = set(self._lists[AUTO_ADDED])
            self._lists[AUTO_NEW] = [i for i in self._lists[AUTO_NEW] if i in marked]
        if key in OPEN_LISTS:
            # Targets and purchase marks only for games still open: a game moving between lists joins
            # the new one first
            present = self._open_ids()
            self._targets = {i: d for i, d in self._targets.items() if i in present}
            self._lists[PURCHASED] = [i for i in self._lists[PURCHASED] if i in present]
        return True

    async def async_set_reported(self, items: Iterable[str]) -> None:
        await self._set_list(REPORTED, items)

    async def async_set_auto_update(self, appid: str, enabled: bool | None) -> dict[str, Any]:
        """Switch automatic updates for one game on or off (toggle without enabled)."""
        appid = str(appid).strip()
        if not protocol.valid_appid(appid):
            return {"appid": appid, "auto_update": None, "reason": "invalid_appid"}
        current = self._list(AUTO_UPDATE)
        on = (appid not in current) if enabled is None else enabled
        await self._set_list(AUTO_UPDATE, [i for i in current if i != appid] + ([appid] if on else []))
        return {"appid": appid, "auto_update": on}

    async def async_set_muted(self, appid: str, enabled: bool | None) -> dict[str, Any]:
        """Mute the updates of one game or unmute them (toggle without enabled)."""
        appid = str(appid).strip()
        if not protocol.valid_appid(appid):
            return {"appid": appid, "muted": None, "reason": "invalid_appid"}
        current = self._list(MUTED)
        on = (appid not in current) if enabled is None else enabled
        await self._set_list(MUTED, [i for i in current if i != appid] + ([appid] if on else []))
        return {"appid": appid, "muted": on}

    async def _try_publish(self, payload: str) -> bool:
        try:
            await async_send_agent_command(self.hass, payload, self.names)
        except Exception:  # noqa: BLE001 - MQTT down must not end the caller with an exception
            _LOGGER.warning("Could not send %s to the Steam agent", payload, exc_info=True)
            return False
        return True

    def _push_unreachable(self, title_key: str, tag: str, icon: str, extra: str = "") -> None:
        send_push(
            self.hass,
            self.notify,
            {
                "title": self.tx(title_key),
                "message": self.tx("agent_unreachable") + extra,
                "data": {
                    "clickAction": self.link,
                    "tag": tag,
                    "channel": self.tx("channel_steam"),
                    "notification_icon": icon,
                },
            },
            self.names,
            PUSH_PROBLEMS,
        )

    async def _agent_ready(
        self, title_key: str, tag: str, icon: str, before_wake: Callable[[], Awaitable[None]] | None = None
    ) -> bool:
        """Wake the PC if needed and wait for the agent; a push when it does not come."""
        wake = await self.power.async_wake(self.agent.agent_online, "on", WAKE_TIMEOUT, before_wake)
        if wake["ready"]:
            return True
        self._push_unreachable(title_key, tag, icon)
        return False

    # --- status changes --------------------------------------------------------------------

    @callback
    def _on_job_state(self, _old: State | None, new: State, live: bool) -> None:
        """A change of the job state (steam_progress.py, before it evaluates busy again). An end that
        is no news (told already, or an older one read again) leaves the lists as they are."""
        if live and new.state == protocol.STARTED:
            self._fail_codes = {}
        elif live and new.state == protocol.ERROR and new.attributes.get("appid") is not None:
            self._fail_codes[str(new.attributes["appid"])] = str(new.attributes.get("reason_code") or "")
        if protocol.job_active(new.state, new.attributes):
            if live:
                self._cancel_queue_timer()  # a job runs; its end moves the queue
                job = protocol.job_id(new.attributes)
                if job and job != self.progress.job_id:
                    # The job that runs is the one whose end counts: the PC leaves out the games of
                    # a command sent while it ran unseen (after a restart of HA, between two games)
                    self.progress.job_id = job
                    start_task(self.hass, self._entry, self._save(), "job ID")
            return
        if not live or new.state not in protocol.ENDED:
            return
        # Before the pushes of this end (steam_notify.py starts after this part): they leave out
        # the games that go to the overflow drive
        overflow = self._overflow_plan(new.attributes) if new.state in protocol.FINISHED else {}
        self.overflow_now = overflow
        # Something may follow: Steam stays busy from now until the queue moved on
        if self._has_open() or overflow or new.attributes.get("game_running"):
            self._schedule_queue()
        # Not eager: the pushes of this change see the lists as they were
        start_task(
            self.hass,
            self._entry,
            self._job_ended(new.state, dict(new.attributes), overflow),
            "job end",
            eager=False,
        )

    def _overflow_plan(self, attrs: dict[str, Any]) -> dict[str, Overflow]:
        """The new purchases of this end that go to the overflow drive (see the module text)."""
        letter = self.overflow_drive
        marked = set(self._lists[PURCHASED])
        if not letter or not marked:
            return {}
        drives = drives_of(self.hass, self.names)
        entry = find_drive(drives, letter)
        free = number(entry.get("free_gb"), None) if entry is not None else None
        if free is None:
            return {}  # not there now, or its free space is not known
        star = next((d["drive"] for d in drives if d.get("default")), "")
        found: list[tuple[str, str, str, float | None]] = []  # appid, full drive, reason, needed GB
        for item in attrs.get("no_space") or []:
            if isinstance(item, dict):
                full = drive_letter(item.get("drive"))
                found.append((str(item.get("appid")), full, "no_space", number(item.get("needed_gb"), None)))
        for appid in parse_appids(attrs.get("failed") or []):
            if self._fail_codes.get(appid) == "drive_unavailable":
                size = known_game_bytes(self.hass, appid)
                found.append((appid, star, "drive_unavailable", needed_gb(size) if size else None))
        plan: dict[str, Overflow] = {}
        remaining = free  # less what the games planned before need (each with its own reserve)
        for appid, full, reason, needed in found:
            if appid not in marked or appid in self._targets or appid in plan:
                continue  # not a new purchase of automatic work or its button, or tried with a target
            if drive_letter(letter) in (full, star):
                continue  # the overflow drive is the one that was full, or the default drive
            if needed is not None:
                if remaining < needed:
                    _LOGGER.info("No space for %s on the overflow drive %s either", appid, letter)
                    continue
                remaining -= needed
            plan[appid] = Overflow(letter, full, reason)
        return plan

    async def _job_ended(self, state: str, attrs: dict[str, Any], overflow: dict[str, Overflow] | None = None) -> None:
        """A job end that is news (steam_progress.py)."""
        job = self._list(JOB)
        if state == protocol.ABORTED:
            # Interrupted (PC off, script stopped): its games stay and go out again with the queue
            remaining = job
        else:
            results = protocol.result_appids(attrs)
            # The games the end names are done with. An end without result lists ends the job.
            remaining = [i for i in job if i not in results] if results else []
        # No await from reading the job list to writing it (async_install may take the queue in
        # between). Games not started because a game ran go to after_game or the queue first, so
        # they keep their targets when they leave the job list.
        changed = self._wait_or_queue(parse_appids(attrs.get("game_running") or []), str(attrs.get("game_appid") or ""))
        if state != protocol.ABORTED:
            changed = self._give_up_without_space(attrs) or changed
        # An end with a job ID: steam_progress.end_id is kept as told
        if self._put(JOB, remaining) or changed or protocol.job_id(attrs):
            await self._save()
            self._notify_listeners()
        # Quiet only while the game is still open (a second try marks it again)
        present = set(self._lists[JOB]) | set(self._lists[QUEUE]) | set(self._lists[WAITING])
        await self._set_list(AUTO_QUIET, [i for i in self._lists[AUTO_QUIET] if i in present])
        # An end of their try on the overflow drive: told by this end's pushes already. Not started
        # because a game ran is no end yet: its later result still tells why it went there.
        waiting = set(parse_appids(attrs.get("game_running") or []))
        for appid in protocol.result_appids(attrs) if state != protocol.ABORTED else ():
            if appid not in waiting:
                self.overflowed.pop(appid, None)
        if overflow:
            # Sent again at once, as the job that just ended ran (with what else is open; the queue
            # timer set at the end keeps Steam busy until then)
            _LOGGER.info("To the overflow drive %s: %s", self.overflow_drive, list(overflow))
            self.overflowed.update(overflow)
            await self.async_install(list(overflow), drive=self.overflow_drive)
            return
        if self._has_open():
            self._schedule_queue()
        else:
            self._cancel_queue_timer()  # nothing follows: Steam is free

    @callback
    def _on_progress(self) -> None:
        """A job HA counted as running stopped without an end (the PC went off, the hold after an
        old end read again ran out, no progress for 6 h): what is open goes out once the agent is
        online."""
        was_running, self._was_running = self._was_running, self.progress.job_running
        if was_running and not self._was_running and self._has_open():
            self._schedule_queue()

    def _wait_or_queue(self, game_running: list[str], playing: str) -> bool:
        """Games not started because a game was running (in memory only; True when a list changed).
        They wait for the game to close when the agent shows one, or when the end names the game
        running as it was sent (playing, its game_appid): a game started during the job ends it
        before the agent's next round shows the game. If the agent still shows none GAME_SEEN_WAIT
        later, that game was closed before it showed it: what waits goes out then."""
        if not game_running:
            return False
        if self.game_running() or playing:
            if not self.game_running():
                self._start_game_timer()
            _LOGGER.info("Not started because a game runs, waiting for it to close: %s", game_running)
            return self._put(WAITING, self._list(WAITING) + game_running)
        # the game was closed in the meantime: next job
        return self._put(QUEUE, self._list(QUEUE) + game_running)

    @callback
    def _start_game_timer(self) -> None:
        """Look at after_game again in GAME_SEEN_WAIT s. Steam counts as busy meanwhile, as before
        the queue moves on."""
        if self._game_timer is None:
            self._game_timer = async_call_later(self.hass, GAME_SEEN_WAIT, self._on_game_timer)
            self.progress.set_handover(True)

    @callback
    def _cancel_game_timer(self) -> None:
        if self._game_timer is not None:
            self._game_timer()
            self._game_timer = None
            self.progress.set_handover(False)

    @callback
    def _on_game_timer(self, _now: datetime) -> None:
        self._game_timer = None
        self._drain_waiting()
        self.progress.set_handover(False)

    @callback
    def _on_agent_or_game(self, event: Event) -> None:
        """The game was closed, or the agent came (back) online: the waiting games may go now, and
        what is left of a job is sent. Also after a missed edge (PC shut down right after playing,
        MQTT or HA restart)."""
        old, new = event.data["old_state"], event.data["new_state"]
        if new is None or (old is not None and old.state == new.state):
            return
        self._drain_waiting()
        if event.data["entity_id"] == self.agent.agent_online and new.state == "on":
            if self._has_open():
                self._schedule_queue()

    @callback
    def _drain_waiting(self) -> None:
        game = self.hass.states.get(self.agent.game_running)
        if not self._list(WAITING) or not self._agent_online() or game is None or game.state != "off":
            return
        start_task(self.hass, self._entry, self._install_waiting(), "after game")

    async def _install_waiting(self) -> None:
        """The games leave after_game in async_install, once they are in the job or the queue (so
        they keep their targets); one run at a time, so two events do not send them twice."""
        appids = self._list(WAITING)
        if not appids or self._draining:
            return
        self._draining = True
        try:
            auto = set(self._lists[AUTO_ADDED])
            purchased = set(self._lists[PURCHASED])
            _LOGGER.info("Game closed, installing what waited for it: %s", appids)
            # By hand (a new purchase by the button of the purchase push keeps its mark), then
            # automatic work
            groups: list[tuple[list[str], dict[str, bool]]] = [
                ([i for i in appids if i not in auto and i not in purchased], {}),
                ([i for i in appids if i not in auto and i in purchased], {"purchase": True}),
                ([i for i in appids if i in auto], {"automatic": True}),
            ]
            for group, flags in groups:
                if group:
                    await self.async_install(group, **flags)
        finally:
            self._draining = False
        if set(self._lists[WAITING]) - set(appids):
            # Games that joined after_game during this run (a second event returned above)
            self._drain_waiting()

    async def async_wait_for_game(self, appids: Iterable[str], automatic: bool = False) -> None:
        """Install these games once the running game is closed (an update of the game itself)."""
        appids = list(appids)
        await self._set_list(WAITING, self._list(WAITING) + appids)
        if automatic:
            await self._mark(appids)

    async def _mark(self, appids: Iterable[str], purchase: bool = False) -> None:
        """Mark games as added by automatic work (only while the active hours limit it); purchase:
        new purchases, not updates."""
        if self.hours.limited:
            appids = list(appids)
            await self._set_list(AUTO_ADDED, self._list(AUTO_ADDED) + appids)
            if purchase:
                await self._set_list(AUTO_NEW, self._list(AUTO_NEW) + appids)

    async def _unmark(self, appids: Iterable[str]) -> None:
        """Games asked for by hand: they never wait for the active hours."""
        ids = set(appids)
        if ids & set(self._lists[AUTO_ADDED]):
            await self._set_list(AUTO_ADDED, [i for i in self._list(AUTO_ADDED) if i not in ids])

    @callback
    def _schedule_queue(self) -> None:
        """Move the queue on in QUEUE_DELAY s, unless a job starts first. Steam counts as busy
        meanwhile, so nothing shuts the PC down between a job and the next one."""
        if self._queue_timer is None:
            self._queue_timer = async_call_later(self.hass, QUEUE_DELAY, self._on_queue_timer)
            self.progress.set_handover(True)

    @callback
    def _cancel_queue_timer(self) -> None:
        if self._queue_timer is not None:
            self._queue_timer()
            self._queue_timer = None
            self.progress.set_handover(False)

    @callback
    def _on_queue_timer(self, _now: datetime) -> None:
        self._queue_timer = None
        start_task(self.hass, self._entry, self._run_queue(), "queue")

    async def _run_queue(self) -> None:
        """Send what is open (the rest of the job and the queue) as one job, once no job runs.
        Only to an agent that is online: the PC is not woken for it. Ends the busy hold of
        _schedule_queue."""
        try:
            async with self._lock:
                if self.job_running() or self.move.active or not self._has_open():
                    return
                if not self._agent_online():
                    _LOGGER.info("Steam job waits for the agent: %s", self._list(JOB) + self._sendable_queue())
                    return
                await self._take_queue([])
                _LOGGER.info("Queue moves on: %s", self._list(JOB))
                await self._send()
        finally:
            self.progress.set_handover(False)

    async def _take_queue(self, appids: list[str]) -> None:
        """The queue and these games join the job list. Only the games taken leave the queue: an end
        seen meanwhile may add some (_job_ended does not wait for the lock). Outside the active hours
        the games of automatic work stay in the queue."""
        taken = self._sendable_queue()
        await self._set_list(JOB, self._list(JOB) + taken + appids)
        await self._set_list(QUEUE, [i for i in self._list(QUEUE) if i not in taken])

    # --- install / update ------------------------------------------------------------------

    async def async_install(
        self, appids: list[str], automatic: bool = False, purchase: bool = False, drive: str | None = None
    ) -> dict[str, Any]:
        """Install or update games. While a job runs they wait in the queue; a game the job has or
        that is queued already is not added again. automatic: work the integration starts by
        itself (automatic updates, new purchases with purchase); outside the active hours it waits in
        the queue for them. Games asked for by hand go out at once, also those automatic work queued
        before. purchase: new purchases, by automatic work or by the button of the purchase push;
        they may go to the overflow drive (see the module text), games asked for by hand otherwise
        may not. drive: the letter of the drive for a first install (see the module text); None
        keeps what is stored."""
        appids = list(dict.fromkeys(appids))
        if not appids:
            return {"queued": False, "sent": False, "reason": "invalid_appid"}
        async with self._lock:
            # Until they joined a list, an end seen meanwhile (_job_ended does not wait for the lock)
            # keeps the marks and targets of these games: setting them awaits the store
            self._joining = frozenset(appids)
            try:
                if not automatic:
                    await self._unmark(appids)
                    await self._set_list(AUTO_QUIET, [i for i in self._lists[AUTO_QUIET] if i not in appids])
                # Before the games join a list, like the targets (_put keeps the marks of open and
                # joining games)
                if purchase:
                    await self._set_list(PURCHASED, self._lists[PURCHASED] + appids)
                elif not automatic:
                    await self._set_list(PURCHASED, [i for i in self._lists[PURCHASED] if i not in appids])
                retargeted = await self._set_targets(appids, drive)
                if automatic and not self.hours.is_open():
                    open_ids = set(self._list(JOB)) | set(self._list(QUEUE))
                    new = [i for i in appids if i not in open_ids]
                    await self._set_list(QUEUE, self._list(QUEUE) + new)
                    await self._mark(new, purchase)
                    await self._leave_waiting(appids)
                    _LOGGER.info("Outside the active hours, waiting for them: %s", new)
                    return {"queued": bool(new), "sent": False, "deferred": True}
                if self.job_running() or self.move.active:
                    # A move runs: the PC would hold the job back and fail it after 10 minutes
                    open_ids = set(self._list(JOB)) | set(self._list(QUEUE))
                    new = [i for i in appids if i not in open_ids]
                    await self._set_list(QUEUE, self._list(QUEUE) + new)
                    await self._leave_waiting(appids)
                    if not new and not retargeted:
                        _LOGGER.info("Already in the running job or the queue: %s", appids)
                        return {"queued": False, "sent": False, "reason": "already_queued"}
                    if automatic:
                        await self._mark(new, purchase)
                    _LOGGER.info("Job running, queued: %s (new drive: %s)", new, retargeted)
                    return {"queued": True, "sent": False}
                # No job runs: everything open goes out as one job
                await self._take_queue(appids)
                await self._leave_waiting(appids)
                self._joining = frozenset()  # joined: an end during the wake settles them as usual
                woken, sent = await self._wake_and_send()
                return {"queued": False, "sent": sent, "woken": woken}
            finally:
                self._joining = frozenset()

    async def _leave_waiting(self, appids: list[str]) -> None:
        """Sent or queued now: no longer waiting for a game (if a game still runs, the result puts
        them back). Called after they joined the job or the queue, so they keep their targets."""
        waiting = self._list(WAITING)
        if any(i in waiting for i in appids):
            await self._set_list(WAITING, [i for i in waiting if i not in appids])

    async def _set_targets(self, appids: list[str], drive: str | None) -> list[str]:
        """Store the drive for these games, except for those the PC accepted already (in the job
        list while a job runs). Without a drive nothing changes. Returns the games whose target
        changed. Before the games join a list: _put keeps the targets of the games joining."""
        if drive is None:
            return []
        accepted = set(self._lists[JOB]) if self.job_running() else set()
        changed = [i for i in appids if i not in accepted and self._targets.get(i) != drive]
        if changed:
            self._targets.update(dict.fromkeys(changed, drive))
            await self._save()
        return changed

    async def _wake_and_send(self) -> tuple[bool, bool]:
        wake = await self._wake_for_job(self._list(JOB))
        if not wake["ready"]:
            # The job list stays and goes out once the agent is online (_on_agent_or_game)
            return wake["woken"], False
        return wake["woken"], await self._send()

    async def _wake_for_job(self, appids: list[str]) -> dict[str, bool]:
        """Wake the PC if needed and wait for the agent; a push when it does not come."""
        wake = await self.power.async_wake(self.agent.agent_online, "on", WAKE_TIMEOUT)
        if not wake["ready"]:
            _LOGGER.warning("Steam agent not reachable, the job waits for it: %s", appids)
            self._push_unreachable("install_failed_title", TAG_PROGRESS, "mdi:lan-disconnect", self.tx("install_pending"))
        return wake

    async def _send(self) -> bool:
        """Hand the job list to the agent: up to SEND_TRIES commands, each waiting for a state of
        the job. Steam counts as busy meanwhile, so nothing shuts the PC down before it starts. The
        job gets a new job ID (kept before it goes out), the same in every try."""
        self.progress.set_handover(True)
        try:
            # progress.job_id may change meanwhile (a job seen running, _on_job_state)
            job_id = self.progress.job_id = secrets.token_hex(4)
            await self._save()
            for attempt in range(1, SEND_TRIES + 1):
                job = self._list(JOB)
                if not job:
                    return True  # ended meanwhile
                await self._try_publish(protocol.install_command(job, self._targets, job_id))
                if await wait_until(lambda sent=job: self._accepted(sent), SEND_WAIT):
                    _LOGGER.info("Steam job %s accepted (try %s)", job, attempt)
                    return True
            _LOGGER.warning("Steam agent did not accept the job %s after %s tries", job, SEND_TRIES)
            return False
        finally:
            self.progress.set_handover(False)

    def _accepted(self, sent: list[str]) -> bool:
        """A running job reports a state about the games sent, or an end took them off the job list.
        A state about other games (an older end read again after a reconnect, another job that still
        runs) does not count."""
        if not set(sent) & set(self._list(JOB)):
            return True
        state = self.hass.states.get(self.agent.install_status)
        if state is None or not protocol.job_active(state.state, state.attributes):
            return False
        return bool(protocol.state_appids(state.state, state.attributes) & set(sent))

    # --- active hours ----------------------------------------------------------------------

    @callback
    def _on_hours(self) -> None:
        """The active hours opened or closed: once open, what waited for them goes out."""
        self._notify_listeners()  # the updates sensor shows what waits
        if self.hours.is_open() and self._held():
            start_task(self.hass, self._entry, self._start_held(), "active hours")

    def _no_longer_needed(self, appids: list[str]) -> list[str]:
        """Of these games of automatic work: those installed and up to date by now, installed and
        outdated with automatic updates switched off meanwhile, or updates of games no longer
        installed (uninstalled on the PC meanwhile; the update must not install them again). None
        while the library is not known."""
        games = library_games(self.hass, self.agent)
        if games is None:
            return []
        installed = {str(g.get("appid")) for g in games}
        outdated = {str(g.get("appid")) for g in self.updates.outdated}
        auto = set(self._lists[AUTO_UPDATE])
        purchases = set(self._lists[AUTO_NEW])
        return [
            i
            for i in appids
            if (i in installed and (i not in outdated or i not in auto)) or (i not in installed and i not in purchases)
        ]

    async def _start_held(self) -> None:
        """The active hours are open: the games of automatic work that waited in the queue go out
        as one job (with what else is open), waking the PC. While a job runs they follow it with
        the queue; games waiting for a game to close go once it is closed (_drain_waiting)."""
        async with self._lock:
            held = [i for i in self._held() if i in self._lists[QUEUE]]
            if not held or not self.hours.is_open() or self.job_running() or self.move.active:
                return
            drop = self._no_longer_needed(held)
            if drop:
                _LOGGER.info("Waited for the active hours, not needed any more: %s", drop)
                await self._set_list(QUEUE, [i for i in self._list(QUEUE) if i not in drop])
            if not self._list(QUEUE):
                return
            # Wake first: the games leave the queue only for an agent that is there. In the job list
            # they would go out whenever the agent comes, also outside the window; in the queue
            # they wait for it inside the window (_on_agent_or_game), or for its next start.
            if not (await self._wake_for_job(self._list(JOB) + self._list(QUEUE)))["ready"]:
                return
            await self._take_queue([])
            _LOGGER.info("Active hours began, starting: %s", self._list(JOB))
            await self._send()

    # --- automatic updates -----------------------------------------------------------------

    @callback
    def _on_check_done(self, event: Event) -> None:
        if event.data.get("pc", "") != self.names.pc_name:
            return  # another PC's check
        start_task(self.hass, self._entry, self.async_auto_update(), "auto update")

    def auto_given_up(self) -> set[str]:
        """Games whose automatic updates stopped for their current public state (the check after
        the last allowed try still found them outdated)."""
        return {
            appid
            for appid, (state, tries) in self._auto_tries.items()
            if tries > AUTO_TRIES and state == self.updates.fingerprints.get(appid, "")
        }

    def _give_up_without_space(self, attrs: dict[str, Any]) -> bool:
        """Games with automatic updates the PC had no space for: no further automatic try for
        their public state (another try would not find more space, only push again), until the
        user makes room through the push (async_retry_auto) or a new public state comes. True
        when that changed something (the caller saves)."""
        auto = set(self._lists[AUTO_UPDATE])
        changed = False
        for item in attrs.get("no_space") or []:
            appid = str(item.get("appid")) if isinstance(item, dict) else ""
            if appid not in auto:
                continue
            given_up = (self.updates.fingerprints.get(appid, ""), AUTO_TRIES + 1)
            if self._auto_tries.get(appid) != given_up:
                _LOGGER.info("Auto update of %s stops: not enough space on the PC", appid)
                self._auto_tries[appid] = given_up
                changed = True
        return changed

    async def async_retry_auto(self, appid: str) -> None:
        """Room was made for a game whose automatic update found no space: the automatic updates
        may try it again."""
        if self._auto_tries.pop(str(appid), None) is not None:
            await self._save()

    async def async_auto_update(self) -> list[str]:
        """After an update check: start the updates of games with automatic updates. Not for games
        a job has or that wait already; the game being played waits until it is closed. At most
        AUTO_TRIES jobs per public state of a game: if Steam reports a game as fine while the check
        still sees it outdated (e.g. a beta branch), it is not sent again and again; a new public
        build or manifest allows new tries. A game the PC had no space for counts as tried out at
        once (_give_up_without_space). Returns the app IDs handed to a job."""
        outdated = [str(g.get("appid")) for g in self.updates.outdated]
        auto = set(self._lists[AUTO_UPDATE])
        open_ids = set(self._list(JOB)) | set(self._list(QUEUE)) | set(self._list(WAITING))
        candidates = [i for i in outdated if i in auto and i not in open_ids]
        playing = self._playing()
        if playing and playing in candidates:
            candidates.remove(playing)
            _LOGGER.info("Auto update of %s waits until the game is closed", playing)
            await self._set_list(AUTO_QUIET, self._lists[AUTO_QUIET] + [playing])
            await self.async_wait_for_game([playing], automatic=True)
        before = dict(self._auto_tries)
        allowed: list[str] = []
        for appid in candidates:
            state = self.updates.fingerprints.get(appid, "")
            last_state, tries = self._auto_tries.get(appid, ("", 0))
            tries = tries if last_state == state else 0
            if tries >= AUTO_TRIES:
                if tries == AUTO_TRIES:
                    _LOGGER.warning("Auto update of %s stops: tried %s times for this public state", appid, tries)
                    self._auto_tries[appid] = (state, tries + 1)  # said once
                else:
                    _LOGGER.debug("Auto update of %s skipped: tried for this public state", appid)
                continue
            self._auto_tries[appid] = (state, tries + 1)
            allowed.append(appid)
        # A game that is up to date starts from zero next time
        for appid in [a for a in self._auto_tries if a not in outdated]:
            del self._auto_tries[appid]
        if self._auto_tries != before:
            await self._save()
        if allowed:
            _LOGGER.info("Auto update: %s", allowed)
            await self._set_list(AUTO_QUIET, self._lists[AUTO_QUIET] + allowed)
            await self.async_install(allowed, automatic=True)
        return allowed

    # --- uninstall -------------------------------------------------------------------------

    async def async_uninstall(self, appid: str) -> dict[str, Any]:
        """Uninstall a game. The result arrives as MQTT steam/uninstall (push from steam_notify.py)."""
        appid = str(appid).strip()
        if not protocol.valid_appid(appid):
            return {"sent": False, "reason": "invalid_appid"}
        now = dt_util.utcnow()
        last = self._uninstall_sent.get(appid)
        if last is not None and now - last < UNINSTALL_GUARD:
            _LOGGER.info("Uninstall %s ignored, sent %s s ago", appid, int((now - last).total_seconds()))
            return {"sent": False, "reason": "already_sent"}
        self._uninstall_sent[appid] = now
        # An open install of the game would install it again: the PC woken for the uninstall would
        # get it from the queue at the same time. Out of the job list only while no job runs (a
        # running job with the game makes the PC refuse the uninstall with job_running).
        lists = (WAITING, QUEUE) if self.job_running() else (WAITING, QUEUE, JOB)
        changed = False
        for key in lists:
            changed = self._put(key, [i for i in self._lists[key] if i != appid]) or changed
        changed = self._put(AUTO_QUIET, [i for i in self._lists[AUTO_QUIET] if i != appid]) or changed
        if changed:
            await self._save()
            self._notify_listeners()
        if not await self._agent_ready("uninstall_failed_title", TAG_UNINSTALL, "mdi:delete-alert-outline"):
            self._uninstall_sent.pop(appid, None)  # a retry once the PC is on goes through
            return {"sent": False, "reason": "agent_offline"}
        if not await self._try_publish(f"uninstall:{appid}"):
            self._uninstall_sent.pop(appid, None)
            return {"sent": False, "reason": "publish_failed"}
        return {"sent": True}

    # --- move ------------------------------------------------------------------------------

    def _move_check(self, appid: str, letter: str) -> str:
        """What speaks against moving this game to this drive as far as HA knows (the PC checks it
        all again): a reason code, "" when nothing does. Unknown library or drives: the PC decides."""
        games = library_games(self.hass, self.agent)
        if games is not None:
            found = next((g for g in games if str(g.get("appid")) == appid), None)
            if found is None:
                return "not_installed"
            if found.get("unavailable"):
                return "drive_unavailable"
            if drive_letter(found.get("drive")) == drive_letter(letter):
                return "same_drive"
        drives = drives_of(self.hass, self.names)
        if drives and find_drive(drives, letter) is None:
            return "drive_unavailable"
        return ""

    def _move_blocked(self) -> str:
        """What keeps a move from starting on a PC that is on: a reason code or ""."""
        if self.move.active or self.job_running() or self._queue_timer is not None or self._lock.locked():
            # One Steam job at a time; the queue is about to move on, or a job is being handed over
            # (waking the PC, waiting for the agent to accept it)
            return "job_running"
        if self.game_running():
            return "game_running"  # Steam cannot be closed while a game runs
        return ""

    async def async_move(self, appid: str, drive: str) -> dict[str, Any]:
        """Move an installed game to the Steam library on another drive (a lower-case letter; one
        without a library gets a new one). Wakes the PC like an uninstall. The progress and the
        result arrive on steam/move (steam_move.py; push from steam_notify.py)."""
        appid = str(appid).strip()
        letter = str(drive or "").strip()[:1].lower()
        if not protocol.valid_appid(appid):
            return {"sent": False, "reason": "invalid_appid"}
        if not protocol.valid_drive(letter):
            return {"sent": False, "reason": "invalid_drive"}
        if reason := self._move_check(appid, letter) or self._move_blocked():
            return {"sent": False, "reason": reason}
        now = dt_util.utcnow()
        last = self._move_sent.get(appid)
        if last is not None and now - last < UNINSTALL_GUARD:
            _LOGGER.info("Move of %s ignored, sent %s s ago", appid, int((now - last).total_seconds()))
            return {"sent": False, "reason": "already_sent"}
        self._move_sent[appid] = now
        if not await self._agent_ready("move_failed_title", TAG_MOVE, "mdi:folder-alert-outline"):
            self._move_sent.pop(appid, None)  # a retry once the PC is on goes through
            return {"sent": False, "reason": "agent_offline"}
        # After a wake the library and the state of the PC are fresh only now
        if reason := self._move_check(appid, letter) or self._move_blocked():
            self._move_sent.pop(appid, None)
            return {"sent": False, "reason": reason}
        self.move.expect(appid, letter)
        if not await self._try_publish(f"move:{appid}@{letter}"):
            self.move.not_sent()
            self._move_sent.pop(appid, None)
            return {"sent": False, "reason": "publish_failed"}
        _LOGGER.info("Move of %s to %s sent", appid, drive_letter(letter))
        return {"sent": True}

    @callback
    def _on_move_change(self) -> None:
        """A move ended (or counts as ended): the queue that waited for it moves on."""
        was, self._move_was_active = self._move_was_active, self.move.active
        if was and not self.move.active and self._has_open():
            self._schedule_queue()

    @callback
    def _on_move(self, kind: str, data: dict[str, Any], live: bool) -> None:
        """The PC answered a move (or it counts as ended): it may be asked for again at once, also
        to another drive after a refusal. A successful one: the library must show the game on its
        new drive soon."""
        if kind in ("ended", "silent"):
            self._move_sent.pop(str(data.get("appid")), None)
        if kind != "ended" or not live or data.get("state") != "ok":
            return
        self._cancel_library_timer()
        appid, target = str(data.get("appid")), drive_letter(data.get("drive"))

        @callback
        def check(_now: datetime) -> None:
            self._library_timer = None
            games = library_games(self.hass, self.agent) or []
            found = next((g for g in games if str(g.get("appid")) == appid), None)
            if found is not None and target and drive_letter(found.get("drive")) != target:
                _LOGGER.info("The library still shows %s on %s after its move: asked for again", appid, found.get("drive"))
                start_task(self.hass, self._entry, self.updates.async_refresh_library(), "library after move")

        self._library_timer = async_call_later(self.hass, MOVE_LIBRARY_CHECK, check)

    @callback
    def _cancel_library_timer(self) -> None:
        if self._library_timer is not None:
            self._library_timer()
            self._library_timer = None

    # --- launch ----------------------------------------------------------------------------

    async def async_launch(self, appid: str) -> dict[str, Any]:
        """Start a game on the PC (steam://rungameid through the agent), waking the PC first. Steam
        starts itself if it does not run yet. Not while a game runs.

        With the option "start games without the lock" a PC that has to be woken for it gets a note
        first (LAUNCH_PENDING_TOPIC, retained): the lock script reads it at logon and leaves the lock
        out once. The note goes again once the command was sent, when the agent does not come, and
        after LAUNCH_PENDING_VALID at the latest. A PC that is on never gets it."""
        appid = str(appid).strip()
        if not protocol.valid_appid(appid):
            return {"sent": False, "reason": "invalid_appid"}
        if self.game_running():
            return {"sent": False, "reason": "game_running"}
        if self.move.active:
            return {"sent": False, "reason": "moving"}  # Steam is closed for a moment during a move
        noted = False

        async def leave_note() -> None:
            nonlocal noted
            if self.launch_unlocked and not noted:
                noted = await self._set_launch_note(appid)

        try:
            if not await self._agent_ready(
                "launch_failed_title", TAG_LAUNCH, "mdi:gamepad-variant-outline", leave_note
            ):
                return {"sent": False, "reason": "agent_offline"}
            if not await self._try_publish(f"launch:{appid}"):
                return {"sent": False, "reason": "publish_failed"}
            _LOGGER.info("Game %s started on the PC", appid)
            return {"sent": True}
        finally:
            if noted:
                await self._clear_launch_note()

    async def _publish_launch_note(self, payload: str) -> bool:
        try:
            await self.hass.services.async_call(
                "mqtt",
                "publish",
                {"topic": self.names.launch_pending_topic, "payload": payload, "qos": 1, "retain": True},
                blocking=True,
            )
        except Exception:  # noqa: BLE001 - without the note the PC locks as usual
            _LOGGER.warning(
                "Could not send %s to %s", payload or "an empty note", self.names.launch_pending_topic, exc_info=True
            )
            return False
        return True

    async def _set_launch_note(self, appid: str) -> bool:
        """Leave the note for the lock script, valid for LAUNCH_PENDING_VALID; a timer clears it then."""
        until_time = dt_util.utcnow() + LAUNCH_PENDING_VALID
        until = until_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        if not await self._publish_launch_note(json.dumps({"appid": appid, "until": until})):
            return False
        self._note_until = until_time
        self._cancel_note_timer()
        self._note_timer = async_call_later(self.hass, LAUNCH_PENDING_VALID, self._on_note_timer)
        _LOGGER.info("Game %s: the PC is woken and not locked at logon (valid until %s)", appid, until)
        return True

    async def _clear_launch_note(self, at_start: bool = False) -> None:
        """Clear the note (empty retained message). At the start only when MQTT is set up: there is
        no note to clear otherwise, and a missing MQTT is not worth a warning there. A clear the
        broker did not take is tried again every LAUNCH_NOTE_RETRY while the note may still be valid
        (the lock script ignores it after its "until"; one an earlier run left is valid for
        LAUNCH_PENDING_VALID from now at the most)."""
        self._cancel_note_timer()
        if at_start:
            if not self.hass.services.has_service("mqtt", "publish"):
                return
            self._note_until = dt_util.utcnow() + LAUNCH_PENDING_VALID
        if await self._publish_launch_note(""):
            self._note_until = None
            return
        # Not when a new note was set meanwhile: it has its own timer
        if self._note_timer is None and self._note_until is not None and dt_util.utcnow() < self._note_until:
            self._note_timer = async_call_later(self.hass, LAUNCH_NOTE_RETRY, self._on_note_timer)

    @callback
    def _cancel_note_timer(self) -> None:
        if self._note_timer is not None:
            self._note_timer()
            self._note_timer = None

    @callback
    def _on_note_timer(self, _now: datetime) -> None:
        self._note_timer = None
        start_task(self.hass, self._entry, self._clear_launch_note(), "launch note")
