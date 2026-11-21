"""Images of the games: the URLs of their header and capsule images from the Steam store.

The old fixed CDN paths (steam/apps/<appid>/header.jpg) give newer games a grey placeholder with the
right size and HTTP 200, so a browser cannot tell it from a real image. The real ones have a hash in
their path, which only the store knows: its public appdetails API (no key, one app per request, rate
limited) answers them. So this part asks it, one game after the other with a pause, for the games
the integration knows (installed, with an update, owned and not installed, running), and keeps the
answers in a store of their own. An answer is asked again after REFRESH_AFTER; a game the store has
no images for (removed, not sold in this region) after RETRY_FAILED_AFTER. New games are asked for
soon after they appear, not all of them again at every start.

The dashboard card subscribes to the answers with the WebSocket command steam_engine/subscribe_images
(websocket.py), which sends them again on SIGNAL_IMAGES_UPDATED; a game without an answer keeps the
old CDN path. steam_engine/images and the event EVENT_IMAGES_UPDATED are for others (scripts,
automations): Home Assistant lets only administrators subscribe to the event.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .agent_entities import AgentEntities, library_games
from .parts import Part, start_task
from .pc import UNNAMED, PcNames
from .steam_owned import SteamOwned
from .steam_updates import SteamUpdates

_LOGGER = logging.getLogger(__name__)

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
# The store allows roughly 200 requests in 5 minutes; one every 2 s stays well below
REQUEST_PAUSE = 2.0
START_DELAY = 60  # seconds after the start before the first round (HA is busy starting)
NEW_GAME_DELAY = 30  # seconds after a new game appeared (collects a burst of them)
ROUND_EVERY = timedelta(hours=6)  # look for old answers and failures to ask again
REFRESH_AFTER = timedelta(days=30)  # a game's images change rarely (a new hash with a new artwork)
RETRY_FAILED_AFTER = timedelta(days=7)
MAX_REFRESH_PER_ROUND = 50  # old answers asked again per round; new games are not limited
BACKOFF_FIRST = timedelta(minutes=5)  # store busy (429) or not reachable: wait, doubling up to BACKOFF_MAX
BACKOFF_MAX = timedelta(hours=2)
FLUSH_EVERY = 10  # save after this many answers within a round (and tell the card if images changed)
# Fired when answers were added or changed (for automations; the card uses the subscription)
EVENT_IMAGES_UPDATED = "steam_engine_images_updated"
# Sent with resolved() when answers were added or changed, and when an instance starts (after a
# reload its answers replace those of the one before); steam_engine/subscribe_images listens to it.
# Not bound to an instance, so a subscription outlives a reload of the integration.
SIGNAL_IMAGES_UPDATED = "steam_engine_images_signal"
STORAGE_KEY = UNNAMED.images_store_key  # of the unnamed PC (pc.py)


def images_signal(names: PcNames) -> str:
    """The dispatcher signal of one PC's images (the card subscribes to its PC's)."""
    return f"{SIGNAL_IMAGES_UPDATED}_{names.pc_id}"
STORAGE_VERSION = 1
IMAGE_HOST = "steamstatic.com"  # the store's CDN; other URLs are not taken over


class StoreUnavailable(Exception):
    """The store did not answer usefully (rate limit, error status, no JSON, not reachable)."""


def _image_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host == IMAGE_HOST or host.endswith(f".{IMAGE_HOST}")):
        return None
    return value


def parse_appdetails(appid: str, data: Any) -> dict[str, str] | None:
    """{"header", "capsule"} from an appdetails answer, None when the store has no images for the game
    (success false, no data, no header image). Without a capsule the header stands in for it."""
    app = data.get(appid) if isinstance(data, dict) else None
    if not isinstance(app, dict) or app.get("success") is not True or not isinstance(app.get("data"), dict):
        return None
    header = _image_url(app["data"].get("header_image"))
    if header is None:
        return None
    return {"header": header, "capsule": _image_url(app["data"].get("capsule_image")) or header}


async def async_get_images(hass: HomeAssistant, appid: str) -> dict[str, str] | None:
    """The images of one game from the store (parse_appdetails). Raises StoreUnavailable, also for an
    answer without an entry for the game (the store answers null to requests it rejects), so only a
    real answer is remembered as "no images"."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            APPDETAILS_URL, params={"appids": appid, "filters": "basic"}, timeout=REQUEST_TIMEOUT
        ) as response:
            if response.status != 200:
                raise StoreUnavailable(f"HTTP {response.status}")
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise StoreUnavailable(type(err).__name__) from err
    app = data.get(appid) if isinstance(data, dict) else None
    if not isinstance(app, dict) or not isinstance(app.get("success"), bool):
        raise StoreUnavailable("no answer for the game")
    return parse_appdetails(appid, data)


def _appid(value: Any) -> str | None:
    appid = str(value or "").strip()
    return appid if appid.isdigit() and appid != "0" else None


def _valid(item: Any) -> bool:
    """A stored entry: a failure or both URLs, with the time of the answer."""
    if not isinstance(item, dict) or not isinstance(item.get("fetched"), str):
        return False
    return item.get("failed") is True or (
        _image_url(item.get("header")) is not None and _image_url(item.get("capsule")) is not None
    )


class SteamImages(Part):
    """Resolves and keeps the image URLs per app ID."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        agent: AgentEntities,
        updates: SteamUpdates,
        owned: SteamOwned,
        names: PcNames = UNNAMED,
    ) -> None:
        super().__init__(hass)
        self._entry = entry
        self.names = names
        self.agent = agent
        self.updates = updates
        self.owned = owned
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, names.images_store_key)
        # appid: {"header", "capsule", "fetched"} or {"failed": True, "fetched"} (fetched: ISO time)
        self._apps: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._again = False
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._backoff: timedelta | None = None  # set while the store refused; the next wait
        self._stopped = False

    # --- lifecycle -------------------------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        apps = data.get("apps")
        if isinstance(apps, dict):
            self._apps = {str(appid): dict(item) for appid, item in apps.items() if _valid(item)}

    @callback
    def async_start(self) -> None:
        self._unsubs.append(
            async_track_state_change_event(self.hass, [self.agent.library, self.agent.game_running], self._on_state)
        )
        self._unsubs.append(self.owned.async_add_listener(self._on_games))
        self._unsubs.append(self.updates.async_add_listener(self._on_games))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_round, ROUND_EVERY))
        self._arm(START_DELAY)
        async_dispatcher_send(self.hass, images_signal(self.names), self.resolved())

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        super().async_stop()

    # --- public ----------------------------------------------------------------------------

    def get(self, appid: str) -> dict[str, str] | None:
        """{"header", "capsule"} of a resolved game, else None."""
        item = self._apps.get(appid)
        if item is None or item.get("failed"):
            return None
        return {"header": item["header"], "capsule": item["capsule"]}

    def resolved(self) -> dict[str, dict[str, str]]:
        """All resolved games: {appid: {"header", "capsule"}} (for steam_engine/images)."""
        return {
            appid: {"header": item["header"], "capsule": item["capsule"]}
            for appid, item in self._apps.items()
            if not item.get("failed")
        }

    # --- triggers --------------------------------------------------------------------------

    @callback
    def _on_state(self, _event: Event) -> None:
        self._on_games()

    @callback
    def _on_games(self) -> None:
        """A game list changed: a game never asked for is asked for shortly after."""
        if any(appid not in self._apps for appid in self._wanted()):
            self._arm(NEW_GAME_DELAY)

    @callback
    def _on_round(self, _now: datetime) -> None:
        self._arm(0)

    @callback
    def _arm(self, delay: float) -> None:
        """One timer at a time: a planned round (or a wait after a refusal) is not pushed back."""
        if self._cancel_timer is not None or self._stopped:
            return
        self._cancel_timer = async_call_later(self.hass, delay, self._on_timer)

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._cancel_timer = None
        start_task(self.hass, self._entry, self.async_resolve(), "game images")

    # --- resolving -------------------------------------------------------------------------

    def _wanted(self) -> list[str]:
        """App IDs of the games the integration knows, the running one first."""
        wanted: dict[str, None] = {}
        state = self.hass.states.get(self.agent.game_running)
        if state is not None and state.state == "on" and (appid := _appid(state.attributes.get("appid"))):
            wanted[appid] = None
        for game in library_games(self.hass, self.agent) or []:
            if appid := _appid(game.get("appid")):
                wanted[appid] = None
        for game in self.updates.outdated + self.owned.missing:
            if appid := _appid(game.get("appid")):
                wanted[appid] = None
        return list(wanted)

    def _due(self, now: datetime) -> list[str]:
        """Games never asked for, then (at most MAX_REFRESH_PER_ROUND) old answers and failures."""
        new: list[str] = []
        old: list[tuple[datetime, str]] = []
        for appid in self._wanted():
            item = self._apps.get(appid)
            if item is None:
                new.append(appid)
                continue
            fetched = dt_util.parse_datetime(item["fetched"]) or datetime.min.replace(tzinfo=dt_util.UTC)
            if now - fetched >= (RETRY_FAILED_AFTER if item.get("failed") else REFRESH_AFTER):
                old.append((fetched, appid))
        return new + [appid for _, appid in sorted(old)[:MAX_REFRESH_PER_ROUND]]

    async def async_resolve(self) -> None:
        """Ask the store for the due games. Asked for while a round runs: another follows."""
        if self._lock.locked():
            self._again = True
            return
        async with self._lock:
            self._again = True
            while self._again and not self._stopped:
                self._again = False
                if not await self._round():
                    break

    async def _round(self) -> bool:
        """One round; False when the store refused (then the next try waits)."""
        due = self._due(dt_util.utcnow())
        if not due:
            return True
        _LOGGER.debug("Game images: asking the Steam store for %s games", len(due))
        resolved = failed = 0
        unsaved = changed = 0  # answers and changed games since the last save
        try:
            for index, appid in enumerate(due):
                if index:
                    await asyncio.sleep(REQUEST_PAUSE)
                try:
                    images = await async_get_images(self.hass, appid)
                except StoreUnavailable as err:
                    self._back_off(str(err))
                    return False
                self._backoff = None
                fetched = dt_util.utcnow().isoformat()
                before = self.get(appid)
                if images is None:
                    failed += 1
                    self._apps[appid] = {"failed": True, "fetched": fetched}
                else:
                    resolved += 1
                    self._apps[appid] = {**images, "fetched": fetched}
                unsaved += 1
                if self.get(appid) != before:
                    changed += 1
                if unsaved >= FLUSH_EVERY:
                    await self._flush(changed > 0)
                    unsaved = changed = 0
        finally:
            if resolved or failed:
                _LOGGER.info("Game images: %s found, %s without images on the Steam store", resolved, failed)
            # Also when a reload or unload stopped the round: the answers so far are kept (nobody
            # is told then, the entities and the card go away)
            if unsaved:
                await self._flush(changed > 0 and not self._stopped)
        return True

    @callback
    def _back_off(self, reason: str) -> None:
        wait = self._backoff or BACKOFF_FIRST
        self._backoff = min(wait * 2, BACKOFF_MAX)
        _LOGGER.warning(
            "Game images: Steam store not available (%s), next try in %s minutes",
            reason,
            int(wait.total_seconds() // 60),
        )
        # The wait replaces a round planned meanwhile (new games), which would ask too early
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        self._arm(wait.total_seconds())

    async def _flush(self, tell: bool) -> None:
        """Save the answers; with tell, also tell the entities and the card that images changed."""
        await self._store.async_save({"apps": self._apps})
        if tell:
            self._notify_listeners()
            async_dispatcher_send(self.hass, images_signal(self.names), self.resolved())
            self.hass.bus.async_fire(EVENT_IMAGES_UPDATED)


async def async_remove_store(hass: HomeAssistant, names: PcNames = UNNAMED) -> None:
    await Store(hass, STORAGE_VERSION, names.images_store_key).async_remove()
