"""The push "not enough space" and what its buttons lead to.

When a job ends with games the PC had no space for (no_space of done/done_with_errors, PROTOCOL.md),
a push of its own goes out besides the result push (TAG_NO_SPACE; kind problems, as it needs an
answer: it comes also when result pushes are not chosen; a job with nothing else to tell has no
result push then, steam_notify.py). It is about the first of these games, the
others are named in its text. Its buttons, at most three:

- "Free up space" (ROOM), when removing one installed game on the full drive frees enough
  (space.deletion_candidates): a push with up to two such games follows, a game's button asks
  first (ASK), "Yes, delete" (YES) uninstalls it;
- "Move a game" (MOVE), when one of those games also fits on another drive (space.move_targets:
  its size and workshop content from the library plus the reserve, as move.ps1 counts it): a push
  with up to two such games follows (the same order), a game's button (MOVE_GAME) a push with up
  to two drives it fits on, a drive's button (MOVE_TO) moves it there at once (moving deletes
  nothing, so no question);
- "Other drive" (OTHER), only for a first install, when the game fits on another drive of the PC
  (its install size from game_size.py, the free space from the agent's drives, the rule of the
  install job): a push with up to two such drives follows, a drive's button (DRIVE) installs the
  game there;
- "View" when there is neither.

The pushes that follow show at most two choices and "Back" (BACK), which brings the first push
again; "Cancel" of the question does the same.

A new purchase that went to the overflow drive (steam_jobs.py) and found no space there either says
so in the text. Each push that follows replaces the one before (same tag). After "Yes, delete" the game without
space is remembered, in memory only, until the result of that uninstall: when it went well and the
drive has enough free space by then (or the PC did not say), the game goes out again, a first
install to the drive it was meant for, an update as it was; otherwise a short push says why not.
The same after a move started here: its result push (steam_notify.py) says that the game goes out
again, or that it does not as the move failed (that push then comes whatever kinds are chosen); a
move that went well but left too little space brings the short push; a move that ended without an
end (silent, steam_move.py) brings the first push again. An uninstall or move whose result never
came is forgotten after AFTER_KEEP.
Everything a button starts goes out at once, as asked for by hand (also outside the active hours).
A button whose game is no longer known (installed or sent again meanwhile: in the job, the queue or
after_game, or a first install in the library; or Home Assistant was restarted: it is kept in memory
only) does nothing but say so; "Yes, delete" then deletes nothing, "Other drive" installs nothing.

Action names: <prefix><appid>... with a prefix per kind (ACTION_ROOM, ACTION_MOVE, ACTION_MOVE_GAME,
ACTION_MOVE_TO, ACTION_OTHER, ACTION_DRIVE, ACTION_BACK, ACTION_UNINSTALL); a named PC's pushes carry
@<id> after them (push.for_pc, PcNames.action_of).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
import math
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from . import protocol
from .const import PUSH_PROBLEMS
from .game_size import MB, async_game_sizes
from .parts import start_task
from .push import TAG_NO_SPACE, TAG_UNINSTALL_ASK
from .values import known_name, number, whole
from .space import (
    SUGGEST_LIMIT,
    deletion_candidates,
    drive_label,
    drive_letter,
    drive_rank,
    drive_short,
    drives_of,
    fits,
    frees_enough_alone,
    missing_whole_gb,
    move_needed_gb,
    move_targets,
    needed_gb,
)

if TYPE_CHECKING:
    from .steam_jobs import Overflow
    from .steam_notify import SteamNotify

_LOGGER = logging.getLogger(__name__)

AFTER_KEEP = timedelta(hours=6)  # an uninstall or move from the push whose result never came is forgotten then
SUGGEST_RECENT_DAYS = 14  # games played this recently are suggested for deletion last
CHOICES_LIMIT = 2  # games or drives per push that follows: two and "Back" (three buttons on Android)
# Buttons: ..._ASK_<appid>[_<blocked>] asks first, only ..._YES_<appid>[_<blocked>] uninstalls,
# ..._NO_0[_<blocked>] cancels; <blocked> is the game without space that waits for it
ACTION_UNINSTALL = "STEAM_ENGINE_UNINSTALL_"
ACTION_ROOM = "STEAM_ENGINE_ROOM_"  # <blocked>: the deletion candidates for it
ACTION_OTHER = "STEAM_ENGINE_OTHER_"  # <blocked>: the other drives it fits on
ACTION_DRIVE = "STEAM_ENGINE_DRIVE_"  # <blocked>_<letter>: install it on that drive
ACTION_BACK = "STEAM_ENGINE_BACK_"  # <blocked>: the first push again
ACTION_MOVE = "STEAM_ENGINE_MOVE_"  # <blocked>: the games to move off its drive
ACTION_MOVE_GAME = "STEAM_ENGINE_MOVEGAME_"  # <game>_<blocked>: the drives that game fits on
ACTION_MOVE_TO = "STEAM_ENGINE_MOVETO_"  # <game>_<blocked>_<letter>: move it there


@dataclass
class Blocked:
    """A game the PC had no space for, as its job ended."""

    appid: str
    name: str
    entry: dict[str, Any]  # the PC's no_space entry: appid, needed_gb, free_gb, drive
    update: bool  # installed already: an update (no other drive for it)
    target: str | None  # the drive letter it was sent with ("e"), None for Steam's default
    group: frozenset[str] = field(default_factory=frozenset)  # the games without space of its result
    overflow: Overflow | None = None  # a new purchase sent to the overflow drive (steam_jobs.py)

    @property
    def drive(self) -> str:
        return drive_letter(self.entry.get("drive"))


class NoSpace:
    """The "not enough space" push of one PC and its buttons; part of SteamNotify."""

    def __init__(self, notify: SteamNotify) -> None:
        self.n = notify
        self.hass = notify.hass
        self.tx = notify.tx
        # The games without space of the recent job ends, until they are installed or sent again
        self._blocked: dict[str, Blocked] = {}
        # Uninstalls confirmed for a game without space: uninstalled appid -> (blocked appid, when)
        self._after: dict[str, tuple[str, Any]] = {}
        # Moves started for a game without space: moved appid -> (blocked appid, when)
        self._after_move: dict[str, tuple[str, Any]] = {}

    # --- helpers ---------------------------------------------------------------------------

    def _push(
        self,
        kind: str | None,
        title: str,
        message: str,
        actions: list[dict[str, str]],
        tag: str = TAG_NO_SPACE,
        icon: str = "mdi:harddisk-remove",
    ) -> None:
        self.n._push(  # noqa: SLF001 - the owner's way to push
            kind,
            {
                "title": title,
                "message": message,
                "data": {
                    "tag": tag,
                    "channel": self.tx("channel_steam"),
                    "notification_icon": icon,
                    "clickAction": self.n.link,
                    "actions": actions,
                },
            },
        )

    def _view(self) -> list[dict[str, str]]:
        return [{"action": "URI", "title": self.tx("button_view"), "uri": self.n.link}]

    def _back(self, blocked: Blocked) -> dict[str, str]:
        return {"action": f"{ACTION_BACK}{blocked.appid}", "title": self.tx("button_back")}

    def _start(self, coro: Any, name: str) -> None:
        start_task(self.hass, self.n._entry, coro, name)  # noqa: SLF001

    def _game_name(self, appid: str) -> str:
        source = self.n._name_source()  # noqa: SLF001
        return known_name(source, appid) or self.tx("game_fallback", appid=appid)

    def _candidates(
        self, blocked: Blocked, done: frozenset[str] = frozenset(), *, limit: int | None = SUGGEST_LIMIT
    ) -> list[dict[str, Any]]:
        """Installed games on the full drive whose removal alone frees enough for this game
        (space.deletion_candidates): never the running game, a game the job, the queue or
        after_game holds, nor a game without space of the same result. done: the games the job
        that just ended installed (ok); the job list still holds them then (steam_jobs.py settles
        it later), but they are free already."""
        jobs = self.n.jobs
        held = (set(jobs.job) - done) | set(jobs.queue) | set(jobs.waiting)
        outdated = {str(g.get("appid")) for g in self.n.updates.outdated}
        return deletion_candidates(
            self.n._library(),  # noqa: SLF001
            blocked.entry,
            exclude=held | {jobs._playing()} | blocked.group | {blocked.appid},  # noqa: SLF001
            recent_since=(dt_util.now() - timedelta(days=SUGGEST_RECENT_DAYS)).strftime("%Y-%m-%d"),
            auto_pending=set(jobs.auto_update) & outdated,
            limit=limit,
        )

    def _move_candidates(
        self, blocked: Blocked, drives: list[dict[str, Any]], done: frozenset[str] = frozenset()
    ) -> list[dict[str, Any]]:
        """The games to move off the full drive for this game: the deletion candidates (all of
        them, in their order) that fit on another drive (space.move_targets), at most
        CHOICES_LIMIT."""
        if not blocked.drive:
            return []
        found = [g for g in self._candidates(blocked, done, limit=None) if move_targets(g, drives)]
        return found[:CHOICES_LIMIT]

    def _candidate_items(self, candidates: list[dict[str, Any]]) -> str:
        listed = []
        for game in candidates:
            played = str(game.get("last_played") or "")
            when = self.tx("played_last", date=self.tx.date(played)) if played else self.tx("played_never")
            size = whole(number(game.get("size_mb")) / 1024)
            listed.append(self.tx("suggest_item", name=game.get("name"), size=size, played=when))
        return ", ".join(listed)

    def _no_candidates_text(self, blocked: Blocked, label: str) -> str:
        """Why nothing is suggested: no single game frees enough, or one would but is held."""
        key = "suggest_none" if frees_enough_alone(self.n._library(), blocked.entry) is False else "suggest_later"  # noqa: SLF001
        return self.tx(key, drive=label, name=blocked.name, missing=missing_whole_gb(blocked.entry))

    async def _size(self, appid: str) -> float | None:
        """The install size of a first install in bytes (game_size.py), None when unknown."""
        sizes = await async_game_sizes(self.hass)
        size_mb = (await sizes.async_get(appid)).get("size_mb")
        return size_mb * MB if isinstance(size_mb, (int, float)) and size_mb > 0 else None

    async def _fitting_drives(self, blocked: Blocked, drives: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float | None]:
        """The other drives a first install of this game fits on, by the rule of the install job:
        the drives with a Steam library before those without one, an SSD before others, then in
        the PC's order; with the size (bytes, None when unknown: then no drive)."""
        others = [d for d in drives if d.get("drive") != blocked.drive]
        if blocked.update or not others:
            return [], None
        size = await self._size(blocked.appid)
        if size is None:
            return [], None
        fitting = [d for d in others if fits(size, d.get("free_gb"))]
        return sorted(fitting, key=drive_rank), size

    def _drives_text(self, drives: list[dict[str, Any]], all_drives: list[dict[str, Any]]) -> str:
        return ", ".join(
            self.tx("drive_free", drive=drive_label(d.get("drive"), all_drives), free=whole(number(d.get("free_gb"))))
            for d in drives
        )

    # --- the end of a job ------------------------------------------------------------------

    def job_ended(self, no_space: list[dict[str, Any]], ok: list[str], installed: set[str], new: dict[str, bool]) -> bool:
        """A job ended (live): remember its games without space and push about the first one.
        installed: the app IDs of the library; new: first install or not per game, as known at
        the start of the job. True when there is such a push."""
        for appid in ok:
            self._blocked.pop(appid, None)
        targets = self.n.jobs.targets  # still those of the job: steam_jobs.py settles it later
        group = frozenset(str(item.get("appid") or "") for item in no_space)
        found: list[Blocked] = []
        for item in no_space:
            appid = str(item.get("appid") or "")
            if not protocol.valid_appid(appid):
                continue
            blocked = Blocked(
                appid=appid,
                name=self._game_name(appid),
                entry=dict(item),
                update=not new.get(appid, appid not in installed),
                target=targets.get(appid),
                group=group,
                overflow=self.n.jobs.overflowed.get(appid),
            )
            self._blocked[appid] = blocked
            found.append(blocked)
        if found:
            self._start(self._push_first(found, frozenset(ok)), "no space push")
        return bool(found)

    async def _push_first(self, found: list[Blocked], done: frozenset[str], kind: str | None = PUSH_PROBLEMS) -> None:
        """The push "not enough space" for the first game, the others named in its text."""
        first = found[0]
        drives = drives_of(self.hass, self.n.names)
        label = drive_label(first.drive, drives)
        needed = whole(number(first.entry.get("needed_gb")))
        free = whole(number(first.entry.get("free_gb")))
        if not label:
            title = f"Steam: {self.tx('done_no_space')}"
        else:
            title = self.tx("no_space_update_title" if first.update else "no_space_title", drive=label)
        if first.update:
            parts = [self.tx("no_space_update", name=first.name, missing=missing_whole_gb(first.entry), needed=needed, free=free)]
        else:
            parts = [self.tx("no_space_install", name=first.name, needed=needed, free=free)]
        if first.overflow is not None:
            # It was on the overflow drive that there was no space either
            parts.append(self.tx(first.overflow.text_key("overflow_tried"), full=drive_label(first.overflow.full, drives)))
        candidates = self._candidates(first, done) if first.drive else []
        if candidates:
            parts.append(self.tx("suggest", drive=label, name=first.name, items=self._candidate_items(candidates)))
        elif first.drive:
            parts.append(self._no_candidates_text(first, label))
        movable = self._move_candidates(first, drives, done)
        if movable:
            parts.append(self.tx("move_suggest", names=", ".join(str(g.get("name")) for g in movable)))
        fitting, _size = await self._fitting_drives(first, drives)
        if fitting:
            parts.append(self.tx("fits_on", drives=self._drives_text(fitting, drives)))
        if len(found) > 1:
            parts.append(self.tx("no_space_others", names=", ".join(b.name for b in found[1:])))
        actions = []
        if candidates:
            actions.append({"action": f"{ACTION_ROOM}{first.appid}", "title": self.tx("button_room")})
        if movable:
            actions.append({"action": f"{ACTION_MOVE}{first.appid}", "title": self.tx("button_move")})
        if fitting:
            actions.append({"action": f"{ACTION_OTHER}{first.appid}", "title": self.tx("button_other")})
        self._push(kind, title, " ".join(parts), actions or self._view())

    # --- buttons ---------------------------------------------------------------------------

    def on_action(self, action: str) -> bool:
        """A button of these pushes (the PC's own, without @<id>); False for any other action."""
        if action.startswith(ACTION_ROOM):
            self._room(action.removeprefix(ACTION_ROOM))
        elif action.startswith(ACTION_MOVE):
            self._move_games(action.removeprefix(ACTION_MOVE))
        elif action.startswith(ACTION_MOVE_GAME):
            game, _, blocked = action.removeprefix(ACTION_MOVE_GAME).partition("_")
            self._move_drives(game, blocked)
        elif action.startswith(ACTION_MOVE_TO):
            game, _, rest = action.removeprefix(ACTION_MOVE_TO).partition("_")
            blocked, _, letter = rest.partition("_")
            self._move_to(game, blocked, letter)
        elif action.startswith(ACTION_OTHER):
            self._start(self._other(action.removeprefix(ACTION_OTHER)), "other drive push")
        elif action.startswith(ACTION_BACK):
            self._first_again(action.removeprefix(ACTION_BACK))
        elif action.startswith(ACTION_DRIVE):
            appid, _, letter = action.removeprefix(ACTION_DRIVE).partition("_")
            self._drive(appid, letter)
        elif action.startswith(ACTION_UNINSTALL):
            kind, _, rest = action.removeprefix(ACTION_UNINSTALL).partition("_")
            appid, _, blocked = rest.partition("_")
            if kind == "ASK":
                self._ask(appid, blocked)
            elif kind == "YES":
                self._yes(appid, blocked)
            elif kind == "NO":
                if blocked in self._blocked:
                    self._first_again(blocked)  # back to the first push, its other way stays open
                else:
                    self.n._clear(TAG_NO_SPACE if blocked else TAG_UNINSTALL_ASK)  # noqa: SLF001
        else:
            return False
        return True

    def _current(self, appid: str) -> Blocked | None:
        """The game without space, None when it is not known or no longer waits: sent again
        meanwhile (in the job, the queue or after_game) or, a first install, in the library now.
        Such a game is forgotten (its pushes are out of date)."""
        blocked = self._blocked.get(appid)
        if blocked is None:
            return None
        jobs = self.n.jobs
        sent = appid in set(jobs.job) | set(jobs.queue) | set(jobs.waiting)
        installed = not blocked.update and any(str(g.get("appid")) == appid for g in self.n._library())  # noqa: SLF001
        if sent or installed:
            _LOGGER.info("Game %s without space no longer waits (%s)", appid, "sent again" if sent else "installed")
            del self._blocked[appid]
            return None
        return blocked

    def _known(self, appid: str, kept: str = "") -> Blocked | None:
        """The game without space a button is about (_current); when it is not known any more a
        push says so. kept: the game a "Yes, delete" was about, which is then not deleted."""
        blocked = self._current(appid)
        if blocked is None and protocol.valid_appid(appid):
            _LOGGER.info("Button for game %s without space, which is no longer known", appid)
            message = self.tx("no_space_gone", name=self._game_name(appid))
            if kept:
                message += self.tx("no_space_gone_kept", game=kept)
            self._push(None, f"Steam: {self.tx('done_no_space')}", message, self._view())
        return blocked

    def _first_again(self, appid: str) -> None:
        """"Back" (or "Cancel" of the question): the first push of this game again, as the
        answer to a button (sent whatever kinds are chosen)."""
        blocked = self._known(appid)
        if blocked is not None:
            # The others of its job still without space are named again, as in the first push
            others = [b for b in self._blocked.values() if b is not blocked and b.group == blocked.group]
            self._start(self._push_first([blocked, *others], frozenset(), kind=None), "no space push again")

    def _room(self, appid: str) -> None:
        """"Free up space": the games whose removal alone frees enough, a button each."""
        blocked = self._known(appid)
        if blocked is None:
            return
        drives = drives_of(self.hass, self.n.names)
        label = drive_label(blocked.drive, drives)
        candidates = (self._candidates(blocked) if blocked.drive else [])[:CHOICES_LIMIT]
        if candidates:
            message = self.tx("suggest", drive=label, name=blocked.name, items=self._candidate_items(candidates))
            message += self.tx("room_tap")
            actions = [
                {"action": f"{ACTION_UNINSTALL}ASK_{g.get('appid')}_{blocked.appid}", "title": str(g.get("name"))}
                for g in candidates
            ]
        else:
            message = self._no_candidates_text(blocked, label)
            actions = []
        actions.append(self._back(blocked))
        self._push(None, self.tx("room_title", drive=label), message, actions)

    async def _other(self, appid: str) -> None:
        """"Other drive": the drives the game fits on, a button each."""
        blocked = self._known(appid)
        if blocked is None:
            return
        drives = drives_of(self.hass, self.n.names)
        fitting, size = await self._fitting_drives(blocked, drives)
        fitting = fitting[:CHOICES_LIMIT]
        if fitting and size is not None:
            message = self.tx(
                "other_message",
                name=blocked.name,
                needed=math.ceil(round(needed_gb(size), 3)),
                drives=self._drives_text(fitting, drives),
            )
            actions = [
                {
                    "action": f"{ACTION_DRIVE}{blocked.appid}_{d['drive'][0].lower()}",
                    "title": drive_short(d.get("drive"), drives),
                }
                for d in fitting
            ]
        else:
            message = self.tx("other_none", name=blocked.name)
            actions = []
        actions.append(self._back(blocked))
        self._push(None, self.tx("other_title", name=blocked.name), message, actions)

    def _move_games(self, appid: str) -> None:
        """"Move a game": the games whose move to another drive frees enough, a button each."""
        blocked = self._known(appid)
        if blocked is None:
            return
        drives = drives_of(self.hass, self.n.names)
        label = drive_label(blocked.drive, drives)
        movable = self._move_candidates(blocked, drives)
        if movable:
            message = self.tx("move_games_message", drive=label, name=blocked.name, items=self._candidate_items(movable))
            actions = [
                {"action": f"{ACTION_MOVE_GAME}{g.get('appid')}_{blocked.appid}", "title": str(g.get("name"))}
                for g in movable
            ]
        else:
            message = self.tx("move_games_none", drive=label)
            actions = []
        actions.append(self._back(blocked))
        self._push(None, self.tx("move_games_title", drive=label), message, actions, icon="mdi:folder-move-outline")

    def _move_drives(self, game_id: str, blocked_id: str) -> None:
        """A game to move was tapped: the drives it fits on, a button each, naming what follows."""
        blocked = self._known(blocked_id)
        if blocked is None or not protocol.valid_appid(game_id):
            return
        drives = drives_of(self.hass, self.n.names)
        game = next((g for g in self.n._library() if str(g.get("appid")) == game_id), None)  # noqa: SLF001
        name = str(game.get("name")) if game is not None and game.get("name") else self._game_name(game_id)
        if game is not None and drive_letter(game.get("drive")) != blocked.drive:
            game = None  # a stale push: the game is not on the full drive (any more)
        targets = [d for d in move_targets(game, drives) if drive_letter(d.get("drive")) != blocked.drive] if game else []
        targets = targets[:CHOICES_LIMIT]
        needed = move_needed_gb(game) if game is not None else None
        if targets and needed is not None:
            message = self.tx(
                "move_to_message", game=name, needed=math.ceil(round(needed, 3)), drives=self._drives_text(targets, drives)
            )
            if blocked.update:
                message += self.tx("uninstall_then_update", name=blocked.name)
            else:
                message += self.tx("uninstall_then_install", name=blocked.name, drive=drive_label(blocked.drive, drives))
            actions = [
                {
                    "action": f"{ACTION_MOVE_TO}{game_id}_{blocked.appid}_{d['drive'][0].lower()}",
                    "title": drive_short(d.get("drive"), drives),
                }
                for d in targets
            ]
        else:
            message = self.tx("move_to_none", game=name)
            actions = []
        actions.append(self._back(blocked))
        self._push(None, self.tx("move_to_title", game=name), message, actions, icon="mdi:folder-move-outline")

    def _move_to(self, game_id: str, blocked_id: str, letter: str) -> None:
        """A drive picked: move the game there at once (it deletes nothing); the game without
        space waits for the result of the move."""
        if not protocol.valid_appid(game_id) or int(game_id) <= 0 or not protocol.valid_drive(letter):
            return
        blocked = self._known(blocked_id)
        if blocked is None:
            return  # the game it was for is not known any more: nothing is moved
        game = next((g for g in self.n._library() if str(g.get("appid")) == game_id), None)  # noqa: SLF001
        if game is None or drive_letter(game.get("drive")) != blocked.drive or drive_letter(letter) == blocked.drive:
            _LOGGER.info("Move of %s to %s from an old push ignored: not on %s", game_id, letter, blocked.drive)
            return
        if self.n.jobs.move.active or self._waits(self._after_move, game_id):
            _LOGGER.info("Move of %s from the push ignored: a move runs already", game_id)
            return  # a second tap (another phone): the running move keeps its entry
        self.n._clear(TAG_NO_SPACE)  # noqa: SLF001
        self._after_move[game_id] = (blocked_id, dt_util.utcnow())
        _LOGGER.info("Move of %s to drive %s for %s without space (push)", game_id, letter, blocked_id)
        self._start(self._move(game_id, letter), "move from push")

    async def _move(self, appid: str, letter: str) -> None:
        result = await self.n.jobs.async_move(appid, letter)
        if result.get("sent") or result.get("reason") == "already_sent":
            return  # the result comes from the PC (a move sent before as well)
        blocked_id = self._pop_after(self._after_move, appid)
        blocked = self._current(blocked_id) if blocked_id else None
        # agent_offline: steam_jobs.py has pushed "Steam agent unreachable" already
        if blocked is not None and result.get("reason") != "agent_offline":
            key = "move_room_failed_update" if blocked.update else "move_room_failed_install"
            reason = self.tx.reason(result.get("reason"), "move_")
            self._push(
                None,
                self.tx("move_failed_title"),
                self.tx(key, name=blocked.name, game=self._game_name(appid), reason=reason),
                self._view(),
                icon="mdi:folder-alert-outline",
            )

    def _drive(self, appid: str, letter: str) -> None:
        """A drive picked: install the game there, as asked for by hand (at once). Only for a first
        install that still waits (_known)."""
        if not protocol.valid_appid(appid) or not protocol.valid_drive(letter):
            return
        blocked = self._known(appid)
        if blocked is None or blocked.update:
            return
        self.n._clear(TAG_NO_SPACE)  # noqa: SLF001
        self._blocked.pop(appid, None)
        _LOGGER.info("Game %s without space goes to drive %s (push)", appid, letter)
        self._start(self.n.jobs.async_install([appid], drive=letter), "install on other drive")

    def _ask(self, appid: str, blocked_id: str) -> None:
        """A game to delete was tapped: ask first, naming the game that waits for the space."""
        game = next((g for g in self.n._library() if str(g.get("appid")) == appid), {})  # noqa: SLF001
        text = str(game.get("name") or self.tx("game_fallback", appid=appid))
        if "size_mb" in game:
            text += f" ({whole(number(game.get('size_mb')) / 1024)} GB)"
        message = self.tx("uninstall_ask_message", game=text)
        blocked = None
        if blocked_id:
            # Like "Yes, delete": no question for a game without space that no longer waits
            blocked = self._known(blocked_id)
            if blocked is None:
                return
        if blocked is not None:
            drives = drives_of(self.hass, self.n.names)
            if blocked.update:
                message += self.tx("uninstall_then_update", name=blocked.name)
            else:
                message += self.tx("uninstall_then_install", name=blocked.name, drive=drive_label(blocked.drive, drives))
        suffix = f"_{blocked.appid}" if blocked is not None else ""
        self._push(
            None,  # the answer to a button: sent whatever kinds are chosen
            self.tx("uninstall_ask_title"),
            message,
            [
                {"action": f"{ACTION_UNINSTALL}YES_{appid}{suffix}", "title": self.tx("uninstall_yes")},
                {"action": f"{ACTION_UNINSTALL}NO_0{suffix}", "title": self.tx("uninstall_no")},
            ],
            tag=TAG_NO_SPACE if blocked is not None else TAG_UNINSTALL_ASK,
            icon="mdi:delete-alert",
        )

    def _yes(self, appid: str, blocked_id: str) -> None:
        """"Yes, delete": uninstall; the game without space waits for its result."""
        self.n._clear(TAG_NO_SPACE if blocked_id else TAG_UNINSTALL_ASK)  # noqa: SLF001
        if not protocol.valid_appid(appid) or int(appid) <= 0:
            return
        if blocked_id:
            # The push promised that the game without space follows: when it is not known any
            # more, that cannot be kept, so nothing is deleted
            if self._known(blocked_id, kept=self._game_name(appid)) is None:
                return
            self._after[appid] = (blocked_id, dt_util.utcnow())
        _LOGGER.info("Uninstall %s confirmed from push", appid)
        self._start(self._uninstall(appid), "uninstall")

    async def _uninstall(self, appid: str) -> None:
        result = await self.n.jobs.async_uninstall(appid)
        if result.get("sent") or result.get("reason") == "already_sent":
            return  # the result comes from the PC (an uninstall sent before as well)
        blocked_id = self._pop_after(self._after, appid)
        blocked = self._current(blocked_id) if blocked_id else None
        # agent_offline: steam_jobs.py has pushed "Steam agent unreachable" already
        if blocked is not None and result.get("reason") != "agent_offline":
            self._push_failed(blocked, self._game_name(appid), self.tx.reason(result.get("reason")))

    def _push_failed(self, blocked: Blocked, game: str, reason: str) -> None:
        key = "room_failed_update" if blocked.update else "room_failed_install"
        self._push(None, self.tx("uninstall_failed_title"), self.tx(key, name=blocked.name, game=game, reason=reason), self._view())

    # --- the uninstall result --------------------------------------------------------------

    def uninstalled(self, appid: str, ok: bool, name: str, reason: Any, free_gb: Any) -> str:
        """The result of an uninstall. If a game without space waits for it, it goes out again
        when the uninstall went well and the drive has enough space by now (free_gb of the PC; not
        known: the PC decides); otherwise a short push. Returns what the uninstall push adds
        (" Cyberpunk 2077 is being installed now."), "" when nothing follows."""
        blocked_id = self._pop_after(self._after, str(appid))
        blocked = self._current(blocked_id) if blocked_id else None
        if blocked is None:
            return ""
        if not ok:
            self._push_failed(blocked, name, self.tx.reason(reason, "uninstall_"))
            return ""
        needed = number(blocked.entry.get("needed_gb"))
        if free_gb is not None and number(free_gb) < needed:
            drives = drives_of(self.hass, self.n.names)
            self._push(
                None,
                self.tx("still_no_space_title"),
                self.tx(
                    "still_no_space",
                    name=blocked.name,
                    drive=drive_label(blocked.drive, drives),
                    missing=math.ceil(round(needed - number(free_gb), 3)),
                ),
                self._view(),
            )
            return ""
        self._blocked.pop(blocked.appid, None)
        _LOGGER.info("Space freed for %s, sending it again", blocked.appid)
        self._start(self._again(blocked), "install after uninstall")
        return self.tx("uninstalled_then_update" if blocked.update else "uninstalled_then_install", name=blocked.name)

    async def _again(self, blocked: Blocked) -> None:
        """Send the game again as asked for by hand: a first install to the drive it was meant
        for, an update with the automatic tries reset (the user acted)."""
        jobs = self.n.jobs
        if blocked.update:
            await jobs.async_retry_auto(blocked.appid)
        target = blocked.target if protocol.valid_drive(blocked.target) else None
        await jobs.async_install([blocked.appid], drive=None if blocked.update else target)

    # --- the move result -------------------------------------------------------------------

    @staticmethod
    def _waits(after: dict[str, tuple[str, Any]], appid: str) -> bool:
        """An uninstall or move of appid for a game without space whose result may still come."""
        entry = after.get(appid)
        return entry is not None and dt_util.utcnow() - entry[1] <= AFTER_KEEP

    @staticmethod
    def _pop_after(after: dict[str, tuple[str, Any]], appid: str) -> str | None:
        """The game without space an uninstall or move of appid was started for; None without one
        or when it was started long ago (AFTER_KEEP: its result never came)."""
        entry = after.pop(appid, None)
        if entry is None or dt_util.utcnow() - entry[1] > AFTER_KEEP:
            return None
        return entry[0]

    def move_silent(self, appid: str) -> None:
        """A move ended without an end (silent, steam_move.py): if it was started for a game without
        space, the first push of that game comes again (the answer to the button), so that the
        user can choose once more."""
        blocked_id = self._pop_after(self._after_move, appid)
        blocked = self._current(blocked_id) if blocked_id else None
        if blocked is not None:
            _LOGGER.info("Move of %s for %s ended without news: the push comes again", appid, blocked.appid)
            self._first_again(blocked.appid)

    def moved(self, data: dict[str, Any]) -> str:
        """The end of a move (live, steam_notify.py). If a game without space waits for it: when
        the move went well and the drive it left has enough free space now (source_free_gb; not
        known: the PC decides), the game goes out again as after an uninstall; too little space: a
        short push. Returns what the move's result push adds (" Cyberpunk 2077 is being installed
        now.", after a failure " So Cyberpunk 2077 is not installed."), "" when nothing follows."""
        blocked_id = self._pop_after(self._after_move, str(data.get("appid") or ""))
        blocked = self._current(blocked_id) if blocked_id else None
        if blocked is None:
            return ""
        if data.get("state") != "ok":
            return self.tx("move_then_not_update" if blocked.update else "move_then_not_install", name=blocked.name)
        free_gb = data.get("source_free_gb")
        if drive_letter(data.get("from")) not in ("", blocked.drive):
            free_gb = None  # not the full drive that was freed: only the PC can tell
        needed = number(blocked.entry.get("needed_gb"))
        if free_gb is not None and number(free_gb) < needed:
            drives = drives_of(self.hass, self.n.names)
            self._push(
                None,
                self.tx("still_no_space_title"),
                self.tx(
                    "still_no_space",
                    name=blocked.name,
                    drive=drive_label(blocked.drive, drives),
                    missing=math.ceil(round(needed - number(free_gb), 3)),
                ),
                self._view(),
            )
            return ""
        self._blocked.pop(blocked.appid, None)
        _LOGGER.info("Space freed for %s by a move, sending it again", blocked.appid)
        self._start(self._again(blocked), "install after move")
        return self.tx("uninstalled_then_update" if blocked.update else "uninstalled_then_install", name=blocked.name)
