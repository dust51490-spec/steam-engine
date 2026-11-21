"""Install size of a game that is not installed, for the dashboard card.

The card asks for it (WebSocket command steam_engine/game_size, websocket.py) when a game that is
not installed is opened, so the size can be shown before the install dialog. The size comes from
api.steamcmd.net, the same source and client as the update check (steam_updates.py), and is counted
by the same rule as the space check of the install job on the PC (Get-DepotSize in
pc/jobs/install.ps1) for a first install without DLC: the size on disk of the public branch.

Answers are kept per app ID, in memory and in a store of their own, for KEEP_FOR (a size not given
by the API for KEEP_UNKNOWN_FOR), so reopening a game does not ask again. Requests for the same
game share one fetch, and fetches run one after the other with the update check's pause. An error
or timeout answers "unknown" at once and is asked again only after RETRY_AFTER_ERROR (in memory).
Nothing is asked by itself: only what the card asks for.

Sizes do not depend on the PC, so there is one cache for all entries (hass.data[DATA_KEY]); its
store is removed with the last entry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import re
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .steam_updates import REQUEST_PAUSE, async_get_app_info

_LOGGER = logging.getLogger(__name__)

DATA_KEY = f"{DOMAIN}_game_sizes"
STORAGE_KEY = f"{DOMAIN}.game_sizes"
STORAGE_VERSION = 1
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)  # the card waits for it: answer "unknown" soon
KEEP_FOR = timedelta(days=7)  # a game's size changes only with a bigger update
KEEP_UNKNOWN_FOR = timedelta(days=1)  # the API answered, but without depot sizes
RETRY_AFTER_ERROR = timedelta(minutes=10)  # no answer (error, timeout): not asked again before
SAVE_DELAY = 10  # seconds; answers that come in a burst are saved together
MB = 1024 * 1024
_DIGITS = re.compile(r"\d+")


def _number(value: Any) -> int | None:
    """A depot value of the API (a string of digits) as a number, None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value)
    return int(text) if _DIGITS.fullmatch(text) else None


def install_size(app: Any) -> tuple[int, int | None] | None:
    """(size on disk, download) in bytes of a first install without DLC, from the app info of
    api.steamcmd.net (data.<appid>); None when no depot gives a size.

    The rule of Get-DepotSize in pc/jobs/install.ps1 with no installed depots and without DLC: the
    sum of manifests.public.size of every depot with a numeric ID, leaving out DLC depots
    (dlcappid), depots for another OS (config.oslist without windows), for another language than
    English (config.language) and low-violence depots. Depots without a public manifest (shared
    redistributables, sharedinstall/depotfromapp without manifests) add nothing.

    The download is the sum of manifests.public.download of the same depots, None when one of them
    has none or an implausible one (more than its size, as old manifests give)."""
    depots = app.get("depots") if isinstance(app, dict) else None
    if not isinstance(depots, dict):
        return None
    size = 0
    download: int | None = 0
    found = False
    for depot_id, depot in depots.items():
        if not _DIGITS.fullmatch(str(depot_id)) or not isinstance(depot, dict):
            continue
        if "dlcappid" in depot:
            continue
        config = depot.get("config")
        if isinstance(config, dict):
            oslist = str(config.get("oslist") or "")
            if oslist and "windows" not in [name.lower() for name in oslist.split(",")]:
                continue
            language = str(config.get("language") or "")
            if language and language.lower() != "english":
                continue
            if str(config.get("lowviolence") or "") == "1":
                continue
        manifests = depot.get("manifests")
        public = manifests.get("public") if isinstance(manifests, dict) else None
        depot_size = _number(public.get("size")) if isinstance(public, dict) else None
        if depot_size is None:
            continue
        size += depot_size
        found = True
        depot_download = _number(public.get("download"))
        if download is not None and depot_download is not None and 0 < depot_download <= depot_size:
            download += depot_download
        else:
            download = None
    if not found:
        return None
    return size, download


def _mb(value: int | None) -> int | None:
    return None if value is None else round(value / MB)


def size_answer(appid: str, sizes: tuple[int, int | None] | None) -> dict[str, Any]:
    """The answer of steam_engine/game_size: sizes in MB (as size_mb of the library), null when
    unknown."""
    size, download = sizes if sizes else (None, None)
    return {"appid": appid, "size_mb": _mb(size), "download_mb": _mb(download)}


class GameSizes:
    """The install sizes asked for, with their time; one instance for all entries."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # appid -> {"size": bytes | None, "download": bytes | None, "fetched": ISO time}
        self._apps: dict[str, dict[str, Any]] = {}
        # appid -> time of the last failed request (in memory only)
        self._failed: dict[str, datetime] = {}
        self._pending: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()  # one request at a time, with REQUEST_PAUSE between them
        self._last_request = 0.0
        self._removed = False  # the store was removed: answers are no longer saved

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        apps = data.get("apps") if isinstance(data, dict) else None
        if isinstance(apps, dict):
            self._apps = {str(a): v for a, v in apps.items() if isinstance(v, dict)}

    def _cached(self, appid: str) -> dict[str, Any] | None:
        """The kept answer while it is fresh enough, else None."""
        now = dt_util.utcnow()
        entry = self._apps.get(appid)
        if entry is not None:
            fetched = dt_util.parse_datetime(str(entry.get("fetched") or ""))
            size = _number(entry.get("size"))
            keep = KEEP_FOR if size is not None else KEEP_UNKNOWN_FOR
            if fetched is not None and now - fetched < keep:
                return size_answer(appid, None if size is None else (size, _number(entry.get("download"))))
        failed = self._failed.get(appid)
        if failed is not None and now - failed < RETRY_AFTER_ERROR:
            return size_answer(appid, None)
        return None

    def known_bytes(self, appid: str) -> int | None:
        """The install size of one game in bytes if a fresh answer is kept (nothing is fetched), as
        async_get would give it (whole MB); None when unknown or not asked for yet."""
        size_mb = (self._cached(str(appid)) or {}).get("size_mb")
        return size_mb * MB if isinstance(size_mb, int) and size_mb > 0 else None

    async def async_get(self, appid: str) -> dict[str, Any]:
        """The size of one game (size_answer); never raises for a missing answer."""
        cached = self._cached(appid)
        if cached is not None:
            return cached
        # Asked for while the same game is fetched: the same fetch. A task of its own, so a card
        # that goes away (its request cancelled) does not stop it for the others.
        pending = self._pending.get(appid)
        if pending is None or pending.done():
            pending = self.hass.async_create_background_task(self._fetch_safe(appid), f"{DOMAIN} game size {appid}")
            if not pending.done():
                self._pending[appid] = pending
        return await asyncio.shield(pending)

    async def _fetch_safe(self, appid: str) -> dict[str, Any]:
        try:
            return await self._fetch(appid)
        except Exception:  # noqa: BLE001 - the card gets "unknown", never an error
            _LOGGER.exception("Game size of %s failed", appid)
            self._failed[appid] = dt_util.utcnow()
            return size_answer(appid, None)
        finally:
            self._pending.pop(appid, None)

    async def _fetch(self, appid: str) -> dict[str, Any]:
        async with self._lock:
            wait = self._last_request + REQUEST_PAUSE - self.hass.loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                app = await async_get_app_info(async_get_clientsession(self.hass), appid, REQUEST_TIMEOUT)
            finally:
                self._last_request = self.hass.loop.time()
        if app is None:
            _LOGGER.debug("No app info for %s from api.steamcmd.net", appid)
            self._failed[appid] = dt_util.utcnow()
            return size_answer(appid, None)
        sizes = install_size(app)
        self._failed.pop(appid, None)
        self._apps[appid] = {
            "size": sizes[0] if sizes else None,
            "download": sizes[1] if sizes else None,
            "fetched": dt_util.utcnow().isoformat(),
        }
        if not self._removed:
            self._store.async_delay_save(self._data_to_save, SAVE_DELAY)
        return size_answer(appid, sizes)

    async def async_remove(self) -> None:
        """Remove the store through this instance, so its pending save cannot write it again, and
        save no later answer (a fetch still running)."""
        self._removed = True
        await self._store.async_remove()

    def _data_to_save(self) -> dict[str, Any]:
        """The answers still worth keeping (old ones are dropped when saving)."""
        now = dt_util.utcnow()
        keep: dict[str, dict[str, Any]] = {}
        for appid, entry in self._apps.items():
            fetched = dt_util.parse_datetime(str(entry.get("fetched") or ""))
            if fetched is not None and now - fetched < KEEP_FOR:
                keep[appid] = entry
        return {"apps": keep}


async def async_game_sizes(hass: HomeAssistant) -> GameSizes:
    """The one cache of game sizes, loaded on first use."""
    sizes = hass.data.get(DATA_KEY)
    if isinstance(sizes, GameSizes):
        return sizes
    if isinstance(sizes, asyncio.Future):
        return await asyncio.shield(sizes)
    loading: asyncio.Future[GameSizes] = hass.loop.create_future()
    hass.data[DATA_KEY] = loading
    sizes = GameSizes(hass)
    try:
        await sizes.async_load()
    except Exception:  # noqa: BLE001 - an unreadable store starts empty
        _LOGGER.warning("Could not read the kept game sizes, starting empty", exc_info=True)
    hass.data[DATA_KEY] = sizes
    loading.set_result(sizes)
    return sizes


def known_game_bytes(hass: HomeAssistant, appid: str) -> int | None:
    """GameSizes.known_bytes without loading the cache: None while it is not loaded."""
    sizes = hass.data.get(DATA_KEY)
    return sizes.known_bytes(appid) if isinstance(sizes, GameSizes) else None


async def async_remove_store(hass: HomeAssistant) -> None:
    """Remove the kept sizes (the last entry was removed)."""
    sizes = hass.data.pop(DATA_KEY, None)
    if isinstance(sizes, asyncio.Future):
        sizes = await asyncio.shield(sizes)
    if isinstance(sizes, GameSizes):
        # Its own store: Store.async_remove cancels only the delayed save of the instance it is
        # called on
        await sizes.async_remove()
    else:
        await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_remove()
