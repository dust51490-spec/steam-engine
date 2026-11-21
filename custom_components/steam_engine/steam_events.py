"""Events on Home Assistant's bus for automations of one's own, fired whether pushes are set up or not.

    steam_engine_job_done      result (done, done_with_errors, aborted), installed, updated,
                               failed (with reason), no_space (with needed_gb, free_gb, drive),
                               in_client, game_running, open (aborted: games still to do)
    steam_engine_uninstalled   appid, name, success, reason, free_gb
    steam_engine_moved         appid, name, success, reason (reason_code of move.ps1), warning
                               (source_not_deleted, also with not_installed), from, drive (like
                               "F:"), new_library, source_free_gb, target_free_gb
    steam_engine_updates_found games (new since the last check), total
    steam_engine_purchased     games, auto_install
    steam_engine_game_started  appid, name
    steam_engine_game_ended    appid, name, minutes

Every event also has "pc": the PC's name ("" for the PC without a name).

Every game is {appid, name}. Here: the job end (only one that is news, steam_progress.py: it came
from the PC just now, or it is the end of the job handed over that came while HA was not listening;
not an older end read again after a restart or an MQTT reconnect) and the games that start and end;
the update check (steam_updates.py), the owned games (steam_owned.py) and the uninstall and move
results (steam_notify.py) fire theirs with fire(); a move result only when it is news (it came from
the PC just now, or it ends the move whose progress was pushed).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import protocol
from .agent_entities import library_games
from .parts import Part
from .pc import UNNAMED, PcNames
from .texts import Texts
from .values import id_list, known_name

if TYPE_CHECKING:  # these import fire() from here
    from .agent_entities import AgentEntities
    from .own_entities import OwnEntities
    from .steam_jobs import SteamJobs
    from .steam_owned import SteamOwned
    from .steam_progress import SteamProgress

EVENT_JOB_DONE = "steam_engine_job_done"
EVENT_UNINSTALLED = "steam_engine_uninstalled"
EVENT_MOVED = "steam_engine_moved"
EVENT_UPDATES_FOUND = "steam_engine_updates_found"
EVENT_PURCHASED = "steam_engine_purchased"
EVENT_GAME_STARTED = "steam_engine_game_started"
EVENT_GAME_ENDED = "steam_engine_game_ended"


@callback
def fire(hass: HomeAssistant, event_type: str, data: dict[str, Any], names: PcNames = UNNAMED) -> None:
    """Fire an event with the PC it is about ("pc": its name, "" for the PC without a name)."""
    hass.bus.async_fire(event_type, {**data, "pc": names.pc_name})


def game(appid: Any, name: Any) -> dict[str, str]:
    return {"appid": str(appid), "name": str(name)}


class SteamEvents(Part):
    """Fires the job end and the start and end of a game."""

    def __init__(
        self,
        hass: HomeAssistant,
        agent: AgentEntities,
        progress: SteamProgress,
        jobs: SteamJobs,
        owned: SteamOwned,
        own: OwnEntities,
        names: PcNames = UNNAMED,
    ) -> None:
        super().__init__(hass)
        self.names = names
        self.own = own
        self.agent = agent
        self.progress = progress
        self.jobs = jobs
        self.owned = owned
        self._reasons: dict[str, str] = {}  # reason code of each game of the running job that failed

    @callback
    def async_start(self) -> None:
        self._unsubs.append(self.progress.async_add_job_listener(self._on_job_state))
        # The sensor "Current game" of this PC by its unique ID: renamed, it reloads the integration
        self._unsubs.append(async_track_state_change_event(self.hass, [self.own.current_game], self._on_game))

    def _name(self, appid: str) -> str:
        games = (library_games(self.hass, self.agent) or []) + list(self.owned.missing)
        return known_name(games, appid) or Texts(self.hass)("game_fallback", appid=appid)

    def _games(self, appids: Any) -> list[dict[str, str]]:
        return [game(i, self._name(i)) for i in id_list(appids)]

    # --- job --------------------------------------------------------------------------------

    @callback
    def _on_job_state(self, old: State | None, new: State, live: bool) -> None:
        if not live:
            return
        state = new.state
        if state == protocol.ERROR and new.attributes.get("appid") is not None:
            self._reasons[str(new.attributes["appid"])] = str(new.attributes.get("reason_code") or "")
        # An end that is news after another end (e.g. one read again just before) is a new end
        if old is not None and old.state == state and state not in protocol.ENDED:
            return
        if state == protocol.STARTED:
            self._reasons = {}
        elif state in protocol.FINISHED:
            self._done(state, new.attributes)
        elif state == protocol.ABORTED:
            open_ids = list(dict.fromkeys(self.jobs.job + self.jobs.queue))
            fire(self.hass, EVENT_JOB_DONE, {**self._empty(), "result": state, "open": self._games(open_ids)}, self.names)

    @staticmethod
    def _empty() -> dict[str, Any]:
        keys = ("installed", "updated", "failed", "no_space", "in_client", "game_running", "open")
        return {key: [] for key in keys}

    def _done(self, state: str, attrs: dict[str, Any]) -> None:
        ok_ids = id_list(attrs.get("ok"))
        new_ids = set(id_list(attrs.get("new")))
        job_code = str(attrs.get("reason_code") or "")
        no_space = [
            {
                **game(k.get("appid"), self._name(str(k.get("appid")))),
                "needed_gb": k.get("needed_gb"),
                "free_gb": k.get("free_gb"),
                "drive": k.get("drive"),
            }
            for k in attrs.get("no_space") or []
            if isinstance(k, dict)
        ]
        data = {
            **self._empty(),
            "result": state,
            "installed": self._games([i for i in ok_ids if i in new_ids]),
            "updated": self._games([i for i in ok_ids if i not in new_ids]),
            "failed": [
                {**g, "reason": self._reasons.get(g["appid"]) or job_code} for g in self._games(attrs.get("failed"))
            ],
            "no_space": no_space,
            "in_client": self._games(attrs.get("skipped")),
            "game_running": self._games(attrs.get("game_running")),
        }
        fire(self.hass, EVENT_JOB_DONE, data, self.names)

    # --- game -------------------------------------------------------------------------------

    @callback
    def _on_game(self, event: Event[EventStateChangedData]) -> None:
        """From the sensor "Current game": it keeps its game over a restart of HA, so a restart is
        neither an end nor a start. A sensor that is not there (yet or any more) is neither."""
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None or STATE_UNAVAILABLE in (old.state, new.state):
            return
        old_id = str(old.attributes.get("appid") or "")
        new_id = str(new.attributes.get("appid") or "")
        if old_id == new_id:
            return
        if old_id:
            fire(
                self.hass,
                EVENT_GAME_ENDED,
                {**game(old_id, old.state), "minutes": _minutes(old.attributes.get("started"))},
                self.names,
            )
        if new_id:
            fire(self.hass, EVENT_GAME_STARTED, game(new_id, new.state), self.names)


def _minutes(started: Any) -> int | None:
    begin = dt_util.parse_datetime(str(started or "")) if started else None
    if not isinstance(begin, datetime):
        return None
    return max(0, round((dt_util.utcnow() - begin).total_seconds() / 60))
