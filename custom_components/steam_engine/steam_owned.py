"""Owned Steam games: which bought games are not installed, and new purchases.

The PC stays off: the Steam Web API answers for the account (key and SteamID64 on the settings page).
Free games count once played (Steam gives the licence then); free games never played are not part
of the answer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change

from . import protocol
from .agent_entities import AgentEntities, library_games
from .const import (
    DOMAIN,
    CONF_API_KEY,
    CONF_STEAM_ID,
    CONF_STEAM_LINK,
    PUSH_PURCHASES,
    SECTION_NOTIFY,
    SECTION_STEAM,
)
from .issues import ISSUE_API_KEY, ISSUE_PRIVATE, set_issue
from .own_entities import OwnEntities
from .parts import Part, start_task
from .pc import UNNAMED, PcNames
from .push import TAG_PURCHASE, send_push
from .steam_api import SteamUnreachable, async_get_owned
from .steam_events import EVENT_PURCHASED, fire, game
from .steam_jobs import SteamJobs
from .texts import Texts

_LOGGER = logging.getLogger(__name__)

# Button of the purchase push on several PCs: install these games on this PC ("-" between the app IDs)
ACTION_INSTALL_PURCHASES = "STEAM_ENGINE_INSTALL_PURCHASES_"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


def _appids(state: State | None) -> set[str] | None:
    games = state.attributes.get("games") if state is not None else None
    return {str(g.get("appid")) for g in games if isinstance(g, dict)} if isinstance(games, list) else None


class SteamOwned(Part):
    """Compares the owned games with the installed ones."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        options: dict[str, dict],
        agent: AgentEntities,
        own: OwnEntities,
        jobs: SteamJobs,
        names: PcNames = UNNAMED,
    ) -> None:
        super().__init__(hass)
        self.names = names
        self._entry = entry
        self.tx = Texts(hass)
        self.agent = agent
        self.own = own
        self.jobs = jobs
        self.api_key: str = (options[SECTION_STEAM].get(CONF_API_KEY) or "").strip()
        self.steam_id: str = (options[SECTION_STEAM].get(CONF_STEAM_ID) or "").strip()
        self.notify: dict[str, Any] = options[SECTION_NOTIFY]
        self.link: str = options[SECTION_STEAM][CONF_STEAM_LINK]
        self._lock = asyncio.Lock()
        self._again = False
        self._empty_answers = 0  # empty answers of Steam in a row (profile_private hint)
        # Public state for the sensor, always of the account steam_id (see restore)
        self.missing: list[dict[str, str]] = []  # owned, not installed: [{appid, name}] by name
        self.owned: list[str] = []
        self.new: list[str] = []

    # --- lifecycle -------------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        self._unsubs.append(self.hass.bus.async_listen("mobile_app_notification_action", self._on_push_action))
        if not self.api_key or not self.steam_id:
            # Optional: without key and Steam ID nothing to check (a change of the settings reloads)
            set_issue(self.hass, ISSUE_API_KEY, False, names=self.names)
            set_issue(self.hass, ISSUE_PRIVATE, False, names=self.names)
            return
        self._unsubs.append(async_track_time_change(self.hass, self._on_full_hour, minute=0, second=0))
        self._unsubs.append(async_track_state_change_event(self.hass, [self.agent.library], self._on_library))
        # One check right away, so a hint under Repairs fits the settings at once (also after a change)
        start_task(self.hass, self._entry, self.async_check(), "owned check at start")

    @callback
    def restore(self, missing: list[dict[str, str]] | None, owned: list[str] | None, steam_id: str | None) -> None:
        """Take over the last known result after a restart or reload (from the sensor's restore
        state), only when it was checked for the Steam account set now (steam_id). The lists of
        another account would make its games look like new purchases, and without key and Steam ID
        nothing would check them again: they are dropped, so the first check stays quiet as on a
        first start."""
        if not self.api_key or not self.steam_id or steam_id != self.steam_id:
            return
        if missing is not None and not self.missing:
            self.missing = missing
        if owned is not None and not self.owned:
            self.owned = owned
        self._notify_listeners()

    # --- triggers --------------------------------------------------------------------------

    def _installed(self) -> set[str] | None:
        """App IDs of the installed games; None while the library is not known."""
        games = library_games(self.hass, self.agent)
        return {str(g.get("appid")) for g in games} if games is not None else None

    @callback
    def _on_full_hour(self, _now: datetime) -> None:
        start_task(self.hass, self._entry, self.async_check(), "owned check")

    @callback
    def _on_library(self, event: Event) -> None:
        """The PC reported a changed library (e.g. uninstalled in the Steam client): compare at once.
        Only a changed set of games counts, not their sizes or play times."""
        old, new = event.data["old_state"], event.data["new_state"]
        if new is None or new.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        if _appids(old) == _appids(new):
            return
        start_task(self.hass, self._entry, self.async_check(), "owned check")

    # --- the check -------------------------------------------------------------------------

    async def async_check(self) -> dict[str, Any]:
        """Ask Steam for the owned games. A check asked for while one runs follows right after."""
        if self._lock.locked():
            self._again = True
            return {"games": self.missing, "new": [], "skipped": True}
        async with self._lock:
            result = await self._check()
            while self._again:
                self._again = False
                result = await self._check()
            return result

    async def _check(self) -> dict[str, Any]:
        installed = self._installed()
        if installed is None:
            return {"games": self.missing, "new": [], "skipped": True}  # library of the PC not known yet
        if not self.api_key or not self.steam_id:
            _LOGGER.debug("Steam API key or Steam ID missing (settings page), owned games not checked")
            return {"games": self.missing, "new": [], "skipped": True}
        owned_games = await self._fetch()
        if owned_games is None:
            return {"games": self.missing, "new": [], "skipped": True}
        owned = [str(g.get("appid")) for g in owned_games if isinstance(g, dict)]
        missing = sorted(
            (
                {"appid": str(g.get("appid")), "name": g.get("name") or self.tx("game_fallback", appid=g.get("appid"))}
                for g in owned_games
                if isinstance(g, dict) and str(g.get("appid")) not in installed
            ),
            key=lambda g: g["name"].lower(),  # not case sensitive
        )
        # New = not owned before. Uninstalled games were owned already and do not count; on the
        # very first check (nothing known) it stays quiet.
        before = set(self.owned)
        new = [g["appid"] for g in missing if g["appid"] not in before] if before else []
        self.missing, self.owned, self.new = missing, owned, new
        self._notify_listeners()
        _LOGGER.info("Owned games: %s, not installed %s, new %s", len(owned), len(missing), new)
        if new and not self._is_main():
            _LOGGER.debug("New purchases %s: reported by the main PC of this Steam account", new)
        elif new:
            auto = self.hass.states.get(self.own.auto_install_purchases)
            fire(
                self.hass,
                EVENT_PURCHASED,
                {
                    "games": [game(g["appid"], g["name"]) for g in missing if g["appid"] in new],
                    "auto_install": auto is not None and auto.state == "on",
                },
                self.names,
            )
            await self._report_purchases(new)
        return {"games": missing, "new": new, "skipped": False}

    async def _fetch(self) -> list[dict[str, Any]] | None:
        try:
            status, answer = await async_get_owned(
                self.hass, self.api_key, self.steam_id, REQUEST_TIMEOUT, appinfo=True
            )
        except SteamUnreachable:
            return None
        if status != 200:
            _LOGGER.warning("Steam Web API answered %s for the owned games", status)
            # Other codes (rate limit, maintenance) say nothing about the key
            if status in (401, 403):
                set_issue(self.hass, ISSUE_API_KEY, True, names=self.names)
            return None
        set_issue(self.hass, ISSUE_API_KEY, False, names=self.names)
        # An empty answer: a private profile, or one of Steam's occasional empty answers for a public
        # one, so the hint comes after two in a row
        if answer is not None:
            self._empty_answers = 0
            set_issue(self.hass, ISSUE_PRIVATE, False, names=self.names)
        else:
            self._empty_answers += 1
            if self._empty_answers >= 2:
                set_issue(self.hass, ISSUE_PRIVATE, True, names=self.names)
        games = answer.get("games") if answer is not None else None
        return games if isinstance(games, list) else None

    # --- one Steam account on several PCs ---------------------------------------------------

    def _sharing(self) -> list[tuple[ConfigEntry, PcNames, AgentEntities]]:
        """The PCs with this Steam account, this one too, in the order they were set up."""
        found = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id == self._entry.entry_id:
                found.append((entry, self.names, self.agent))
                continue
            data = getattr(entry, "runtime_data", None)
            if data is not None and entry.state is ConfigEntryState.LOADED and data.owned.steam_id == self.steam_id:
                found.append((entry, data.names, data.agent))
        return found

    def _is_main(self) -> bool:
        """New purchases of a Steam account on several PCs are reported (and installed by
        themselves) by one of them only: the PC without a name, else the first one set up."""
        sharing = self._sharing()
        main = next((e for e, n, _ in sharing if not n.named), sharing[0][0])
        return main.entry_id == self._entry.entry_id

    def _install_buttons(self, new: list[str]) -> list[dict[str, str]]:
        """With several PCs on this account: a button per PC that lacks a new game (at most 3)."""
        sharing = self._sharing()
        if len(sharing) < 2:
            return []
        buttons = []
        for _entry, names, agent in sharing:
            installed = {str(g.get("appid")) for g in library_games(self.hass, agent) or []}
            missing = [a for a in new if a not in installed]
            if missing:
                buttons.append(
                    {
                        "action": names.action(ACTION_INSTALL_PURCHASES + "-".join(missing)),
                        "title": self.tx("purchase_install_on", pc=names.pc_name or "Steam PC"),
                    }
                )
        return buttons[:3]

    @callback
    def _on_push_action(self, event: Event) -> None:
        """"Install on <PC>" of the purchase push: only the PC the button is for installs (at once,
        as asked for by hand; a new purchase, so it may go to the overflow drive)."""
        action = self.names.action_of(str(event.data.get("action") or ""))
        if action is None or not action.startswith(ACTION_INSTALL_PURCHASES):
            return
        appids = [a for a in action.removeprefix(ACTION_INSTALL_PURCHASES).split("-") if protocol.valid_appid(a)]
        if appids:
            start_task(self.hass, self._entry, self.jobs.async_install(appids, purchase=True), "install purchases from push")

    async def _report_purchases(self, new: list[str]) -> None:
        """New purchases that are not installed. With the switch on, they are installed at once, or
        at the next start of the active hours (steam_jobs.py)."""
        names = ", ".join(g["name"] for g in self.missing if g["appid"] in new)
        auto = self.hass.states.get(self.own.auto_install_purchases)
        if auto is not None and auto.state == "on":
            hours = self.jobs.hours
            if hours.is_open():
                message = self.tx("purchase_auto", names=names)
            else:
                message = self.tx("purchase_auto_deferred", names=names, time=hours.start_text())
            start_task(
                self.hass,
                self._entry,
                self.jobs.async_install(list(new), automatic=True, purchase=True),
                "install new purchases",
            )
            icon = "mdi:cart-arrow-down"
        else:
            message = self.tx("purchase_manual", names=names)
            icon = "mdi:cart-outline"
        send_push(
            self.hass,
            self.notify,
            {
                "title": self.tx("purchase_title"),
                "message": message,
                "data": {
                    "clickAction": self.link,
                    "tag": TAG_PURCHASE,
                    "channel": self.tx("channel_steam"),
                    "notification_icon": icon,
                    **({"actions": buttons} if (buttons := self._install_buttons(new)) else {}),
                },
            },
            self.names,
            PUSH_PURCHASES,
        )
