"""Steam update check: compares the installed build IDs with the public ones.

The PC stays off: api.steamcmd.net answers for every app without a login. Runs on the settings
page's interval and after Steam finished something (then with a fresh library from the agent).
The games with automatic updates are started by steam_jobs.py after each check.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import logging
from typing import Any

import aiohttp

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from . import protocol
from .agent_entities import AgentEntities, async_send_agent_command, library_games
from .const import CONF_CHECK_EVERY_HOURS, SECTION_STEAM
from .pc import UNNAMED, PcNames
from .parts import Part, start_task
from .steam_events import EVENT_UPDATES_FOUND, fire, game
from .steam_progress import SteamProgress
from .texts import Texts

_LOGGER = logging.getLogger(__name__)

BUILD_URL = "https://api.steamcmd.net/v1/info/{appid}"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
REQUEST_PAUSE = 1  # seconds between two apps, keeps api.steamcmd.net friendly
LIBRARY_WAIT = 60  # seconds to wait for a fresh library after a Steam download
DEPOTS_WAIT = 10  # seconds after subscribing until a missing depot list counts as none
STALE_AFTER = timedelta(hours=3)  # no answer this long (at least the interval + 1 h) = query disturbed
QUERY_TICK = timedelta(minutes=5)
DUE_MARGIN = timedelta(minutes=5)  # a check this much short of the interval counts as due
# Fired after each check: steam_jobs.py starts the automatic updates, steam_notify.py reports new ones
EVENT_CHECK_DONE = "steam_engine_check_done"


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def async_get_app_info(
    session: aiohttp.ClientSession, appid: str, timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT
) -> dict[str, Any] | None:
    """The app info of one app from api.steamcmd.net (data.<appid>: depots, branches, ...), None
    without a usable answer (error status, no JSON, not reachable, timeout, no entry for the app).
    Also used for the install size of a game (game_size.py)."""
    try:
        async with session.get(BUILD_URL.format(appid=appid), timeout=timeout) as response:
            if response.status != 200:
                return None
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    try:
        app = data["data"][appid]
    except (KeyError, TypeError):
        return None
    return app if isinstance(app, dict) else None


class SteamUpdates(Part):
    """Finds installed games with a newer public build, or with a newer manifest for one of their
    installed depots (an update of a single depot can keep the build ID)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        options: dict[str, dict],
        agent: AgentEntities,
        progress: SteamProgress,
        names: PcNames = UNNAMED,
    ) -> None:
        super().__init__(hass)
        self._entry = entry
        self.names = names
        self.tx = Texts(hass)
        self.agent = agent
        self.progress = progress
        self.every_hours: int = options[SECTION_STEAM][CONF_CHECK_EVERY_HOURS]
        self._lock = asyncio.Lock()
        self._again = False  # a check asked for while one runs follows right after it
        self._last_check: datetime | None = None
        self._was_busy = False
        # Public state for the sensors
        self.outdated: list[dict[str, Any]] = []
        self.last_answer: datetime | None = None
        self.running: bool = False
        # Installed depots per game from the PC (DEPOTS_TOPIC); empty until the PC sent its first
        # list, then only the build IDs are compared
        self._depots: dict[str, dict[str, str]] = {}
        # Set once the retained depot list had its chance to arrive (or there is none)
        self._depots_ready = asyncio.Event()
        # Public state per outdated game (build and manifests of its installed depots); steam_jobs
        # limits automatic updates per state, so a mismatch Steam does not clear cannot loop
        self.fingerprints: dict[str, str] = {}
        # The installed build per outdated game when the check last found it outdated: without an
        # answer a game stays outdated only while this build is still installed
        self.installed_builds: dict[str, int] = {}
        self._stopped = False

    # --- lifecycle -------------------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        self._unsubs.append(async_track_time_change(self.hass, self._on_full_hour, minute=0, second=0))
        self._unsubs.append(self.progress.async_add_listener(self._on_progress))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_query_tick, QUERY_TICK))
        self._was_busy = self.progress.busy
        start_task(self.hass, self._entry, self._subscribe_depots(), "depots subscribe")

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        super().async_stop()

    async def _subscribe_depots(self) -> None:
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            _LOGGER.warning("MQTT not available, update check compares build IDs only")
            self._depots_ready.set()
            return
        unsub = await mqtt.async_subscribe(self.hass, self.names.depots_topic, self._on_depots)
        if self._stopped:
            unsub()
            return
        self._unsubs.append(unsub)
        # A retained list arrives within moments; none before the PC's first library report
        # @callback: runs in the event loop (asyncio.Event is not thread-safe)
        self._unsubs.append(async_call_later(self.hass, DEPOTS_WAIT, callback(lambda _now: self._depots_ready.set())))

    @callback
    def _on_depots(self, msg: mqtt.ReceiveMessage) -> None:
        self._depots_ready.set()
        if not msg.payload:
            # retained topic cleared (Uninstall-SteamEngine.ps1)
            self._depots = {}
            return
        try:
            data = json.loads(msg.payload)
        except (TypeError, ValueError):
            _LOGGER.warning("Depot list is no JSON")
            self._depots = {}
            return
        if not isinstance(data, dict):
            self._depots = {}
            return
        self._depots = {
            str(appid): {str(d): str(gid) for d, gid in depots.items()}
            for appid, depots in data.items()
            if isinstance(depots, dict)
        }

    @callback
    def restore(
        self,
        outdated: list[dict[str, Any]] | None,
        last_answer: datetime | None,
        installed_builds: dict[str, int] | None = None,
    ) -> None:
        """Take over the last known result after a restart (from the sensors' restore state)."""
        if outdated is not None and not self.outdated:
            self.outdated = outdated
            self.installed_builds = dict(installed_builds or {})
        if last_answer is not None and self.last_answer is None:
            self.last_answer = last_answer
        self._notify_listeners()

    @property
    def query_state(self) -> str:
        if self.last_answer is None:
            return "never"
        stale_after = max(STALE_AFTER, timedelta(hours=self.every_hours + 1))
        return "ok" if dt_util.utcnow() - self.last_answer < stale_after else "stale"

    # --- triggers --------------------------------------------------------------------------

    @callback
    def _on_full_hour(self, now: datetime) -> None:
        """Check when the interval has passed since the last check (also one after a download)."""
        last = self._last_check or self.last_answer
        if last is not None and now - last < timedelta(hours=self.every_hours) - DUE_MARGIN:
            return
        if self.running or not library_games(self.hass, self.agent):
            return
        start_task(self.hass, self._entry, self.async_check(), "update check")

    @callback
    def _on_progress(self) -> None:
        """Steam finished something (our job or its own updater): reload the library, then check.
        Not when the PC went off, a job only got stuck for 6 h, or the agent is offline."""
        was_busy, self._was_busy = self._was_busy, self.progress.busy
        if not was_busy or self.progress.busy:
            return
        agent = self.hass.states.get(self.agent.agent_online)
        status = self.hass.states.get(self.agent.install_status)
        if agent is None or agent.state != "on":
            return
        if status is not None and protocol.job_active(status.state, status.attributes):
            return
        start_task(self.hass, self._entry, self._after_steam_activity(), "check after download")

    async def _after_steam_activity(self) -> None:
        await self.async_refresh_library()
        await self.async_check()

    async def async_refresh_library(self) -> bool:
        """Ask the agent for its library now and wait up to LIBRARY_WAIT for it. False when the agent
        is offline (nothing asked) or no fresh library came; the old one is used then.

        A library that did not change leaves the sensor's state as it is (no state change), so the
        fresh message on its topic counts too: one sent now, not the retained one the subscription
        gets first. The sensor subscribed long before, so it has read the message already."""
        agent = self.hass.states.get(self.agent.agent_online)
        if agent is None or agent.state != "on":
            return False
        fresh = asyncio.Event()

        @callback
        def _on_library(_event: Event) -> None:
            fresh.set()

        @callback
        def _on_message(msg: mqtt.ReceiveMessage) -> None:
            if not msg.retain:
                fresh.set()

        unsubs = [async_track_state_change_event(self.hass, [self.agent.library], _on_library)]
        try:
            try:
                unsubs.append(await mqtt.async_subscribe(self.hass, self.names.library_topic, _on_message))
            except HomeAssistantError:
                pass  # MQTT not set up: the state change alone (the command below fails then anyway)
            await async_send_agent_command(self.hass, "library", self.names)
            try:
                await asyncio.wait_for(fresh.wait(), LIBRARY_WAIT)
            except TimeoutError:
                _LOGGER.info("No fresh library within %s s, checking with the old one", LIBRARY_WAIT)
                return False
        except Exception:  # noqa: BLE001 - MQTT missing must not stop the check
            _LOGGER.warning("Could not ask the Steam agent for the library", exc_info=True)
            return False
        finally:
            for unsub in unsubs:
                unsub()
        return True

    @callback
    def _on_query_tick(self, _now: datetime) -> None:
        self._notify_listeners()  # the query sensor turns "stale" by time alone

    # --- the check -------------------------------------------------------------------------

    async def async_check(self) -> dict[str, Any]:
        """Check all installed games. Returns the outdated games (also for the service response).
        A check asked for while one runs follows right after it: the running one may have read the
        library before a download ended."""
        if self._lock.locked():
            self._again = True
            _LOGGER.info("Update check already running, another one follows")
            return {"games": self.outdated, "skipped": True}
        async with self._lock:
            result = await self._check()
            while self._again:
                self._again = False
                result = await self._check()
        if not result["skipped"]:
            self.hass.bus.async_fire(EVENT_CHECK_DONE, {"pc": self.names.pc_name})
        return result

    async def _check(self) -> dict[str, Any]:
        library = library_games(self.hass, self.agent)
        if not library:
            return {"games": self.outdated, "skipped": True}
        self.running = True
        self._notify_listeners()
        # The interval counts from the start: a check longer than DUE_MARGIN must not make the next
        # one on the hour fall out
        started = dt_util.utcnow()
        # Right after a start the retained depot list may not be there yet
        try:
            await asyncio.wait_for(self._depots_ready.wait(), DEPOTS_WAIT + 20)
        except TimeoutError:
            pass
        try:
            # A game on a library whose drive is not there (unavailable) cannot be updated now
            outdated, fingerprints, builds = await self._check_games([g for g in library if not g.get("unavailable")])
        finally:
            self.running = False
        before = {str(g.get("appid")) for g in self.outdated}
        self.outdated, self.fingerprints, self.installed_builds = outdated, fingerprints, builds
        self._last_check = started
        self._notify_listeners()
        found = [game(g.get("appid"), g.get("name")) for g in outdated if str(g.get("appid")) not in before]
        if found:
            fire(self.hass, EVENT_UPDATES_FOUND, {"games": found, "total": len(outdated)}, self.names)
        return {"games": outdated, "skipped": False}

    async def _check_games(
        self, library: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, int]]:
        """The outdated games, the public state of each (the last known one for a game without an
        answer) and its installed build."""
        session = async_get_clientsession(self.hass)
        previous = {game["appid"] for game in self.outdated}
        outdated: list[dict[str, Any]] = []
        fingerprints: dict[str, str] = {}
        builds: dict[str, int] = {}
        answered = 0
        for index, game in enumerate(library):
            appid = str(game.get("appid", ""))
            if index:
                await asyncio.sleep(REQUEST_PAUSE)
            public = await self._public_info(session, appid)
            installed = _as_int(game.get("buildid"))
            if public is None:
                # No answer: keep what the last check knew instead of dropping the update, unless
                # another build is installed by now (updated meanwhile: the next answer tells)
                is_outdated = appid in previous and self.installed_builds.get(appid, installed) == installed
                if appid in previous and not is_outdated:
                    _LOGGER.info("Update check: no answer for %s, its installed build changed: not outdated", appid)
                fingerprint = self.fingerprints.get(appid)
            else:
                answered += 1
                self.last_answer = dt_util.utcnow()
                build, manifests = public
                # depots the PC reports as installed (report.ps1, DEPOTS_TOPIC), each with its manifest
                # ID; only depots with a public manifest count
                depots = self._depots.get(appid, {})
                changed = [d for d, gid in depots.items() if manifests.get(d) and manifests[d] != gid]
                if changed:
                    _LOGGER.info("Update check: %s has new depot manifests %s", appid, changed)
                is_outdated = (build > 0 and installed > 0 and build != installed) or bool(changed)
                fingerprint = f"{build}|" + ",".join(sorted(f"{d}:{manifests[d]}" for d in depots if d in manifests))
            if is_outdated:
                if fingerprint is not None:
                    fingerprints[appid] = fingerprint
                builds[appid] = installed
                outdated.append(
                    {
                        "appid": appid,
                        "name": game.get("name") or self.tx("game_fallback", appid=appid),
                        "size_mb": _as_int(game.get("size_mb")),
                    }
                )
        _LOGGER.info("Update check: %s of %s answered, %s outdated", answered, len(library), len(outdated))
        return sorted(outdated, key=lambda g: g["name"]), fingerprints, builds

    async def _public_info(
        self, session: aiohttp.ClientSession, appid: str
    ) -> tuple[int, dict[str, str]] | None:
        """Public build ID and the public manifest ID per depot, None without an answer."""
        app = await async_get_app_info(session, appid)
        if app is None:
            return None
        try:
            depots = app["depots"]
            build = _as_int(depots["branches"]["public"]["buildid"])
        except (KeyError, TypeError):
            return None
        if not build:
            return None
        manifests: dict[str, str] = {}
        for depot, info in depots.items():
            try:
                gid = info["manifests"]["public"]["gid"]
            except (KeyError, TypeError):
                continue
            if str(depot).isdigit() and gid:
                manifests[str(depot)] = str(gid)
        return build, manifests
