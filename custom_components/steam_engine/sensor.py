"""Sensors of the PC state, the Steam update check, the owned games, the download progress and the running game."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoredExtraData, RestoreEntity
from homeassistant.util import dt as dt_util

from .agent_entities import AgentEntities, library_games
from .const import (
    ENTITY_CURRENT_GAME,
    ENTITY_DOWNLOAD_PROGRESS,
    ENTITY_PC_STATE,
    LAST_ANSWER_SENSOR_ENTITY_ID,
    NOT_INSTALLED_SENSOR_ENTITY_ID,
    QUERY_SENSOR_ENTITY_ID,
    KEY_CURRENT_GAME,
    KEY_DOWNLOAD_PROGRESS,
    KEY_LAST_ANSWER,
    KEY_NOT_INSTALLED,
    KEY_PC_STATE,
    KEY_QUERY,
    KEY_UPDATES,
    UPDATES_SENSOR_ENTITY_ID,
)
from .entity import SteamEngineEntity
from .parts import Part
from .pc_power import PC_PHASES, PcPower
from .steam_images import SteamImages
from .steam_jobs import SteamJobs
from .steam_owned import SteamOwned
from .steam_progress import SteamProgress
from .steam_updates import SteamUpdates
from .texts import Texts
from .values import known_name


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    updates: SteamUpdates = entry.runtime_data.updates
    async_add_entities(
        [
            PcStateSensor(entry, entry.runtime_data.power),
            SteamUpdatesSensor(entry, updates, entry.runtime_data.jobs),
            SteamLastAnswerSensor(entry, updates),
            SteamQuerySensor(entry, updates),
            SteamNotInstalledSensor(entry, entry.runtime_data.owned),
            SteamDownloadProgressSensor(entry, entry.runtime_data.progress, entry.runtime_data.jobs),
            SteamCurrentGameSensor(
                entry, entry.runtime_data.agent, entry.runtime_data.owned, entry.runtime_data.images
            ),
        ]
    )


class _SourceSensor[T: Part](SteamEngineEntity, SensorEntity):
    """A sensor that shows what one part (its source) knows, written again when it changes."""

    def __init__(self, entry: ConfigEntry, source: T, key: str, entity_id: str) -> None:
        super().__init__(entry, key, entity_id)
        self._source = source

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._source.async_add_listener(self._on_update))
        self._on_update()

    @callback
    def _on_update(self) -> None:
        self._write_if_changed((self.native_value, self.extra_state_attributes))


class PcStateSensor(_SourceSensor[PcPower]):
    """off, starting, on, shutting_down or restarting (PcPower.phase); shown translated. The PC switch
    turns on only once the PC reports, this shows at once that a wake or a shutdown is on its way."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PC_PHASES
    _ICONS = {
        "off": "mdi:desktop-tower-monitor",
        "starting": "mdi:power",
        "on": "mdi:desktop-tower-monitor",
        "shutting_down": "mdi:power-off",
        "restarting": "mdi:restart",
    }

    def __init__(self, entry: ConfigEntry, power: PcPower) -> None:
        super().__init__(entry, power, KEY_PC_STATE, ENTITY_PC_STATE)

    @property
    def native_value(self) -> str:
        return self._source.phase

    @property
    def icon(self) -> str:
        return self._ICONS[self._source.phase]


class SteamUpdatesSensor(_SourceSensor[SteamUpdates], RestoreEntity):
    """Number of installed games with an update, muted games left out; attribute games [{appid,
    name, size_mb}] (all of them); auto_update: app IDs whose updates start without asking; muted:
    app IDs whose updates are not announced; active_hours: {start, end} (HH:MM)
    while they are switched on, else None; deferred: app IDs of automatic work (updates, new
    purchases) waiting for them; deferred_until: their next start (local ISO time) while games
    wait, else None."""

    _attr_icon = "mdi:steam"

    def __init__(self, entry: ConfigEntry, updates: SteamUpdates, jobs: SteamJobs) -> None:
        super().__init__(entry, updates, KEY_UPDATES, UPDATES_SENSOR_ENTITY_ID)
        self._jobs = jobs

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and isinstance(last.attributes.get("games"), list):
            extra = await self.async_get_last_extra_data()
            builds = extra.as_dict().get("installed_builds") if extra is not None else None
            self._source.restore(
                list(last.attributes["games"]),
                None,
                {str(k): v for k, v in builds.items() if isinstance(v, int)} if isinstance(builds, dict) else None,
            )
        self.async_on_remove(self._jobs.async_add_listener(self._on_update))
        await super().async_added_to_hass()

    @property
    def extra_restore_state_data(self) -> RestoredExtraData:
        """The installed build of each outdated game when it was found (kept for the restore, not
        shown): without an answer of api.steamcmd.net a game stays outdated only while it has it."""
        return RestoredExtraData({"installed_builds": self._source.installed_builds})

    @property
    def native_value(self) -> int:
        muted = set(self._jobs.muted)
        return sum(1 for g in self._source.outdated if str(g.get("appid")) not in muted)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        hours = self._jobs.hours
        deferred = self._jobs.deferred
        until = hours.next_start if deferred else None
        return {
            "games": self._source.outdated,
            "check_running": self._source.running,
            "auto_update": self._jobs.auto_update,
            "muted": self._jobs.muted,
            "active_hours": hours.attribute(),
            "deferred": deferred,
            "deferred_until": dt_util.as_local(until).isoformat() if until is not None else None,
        }


class SteamLastAnswerSensor(_SourceSensor[SteamUpdates], RestoreEntity):
    """When api.steamcmd.net last answered."""

    _attr_icon = "mdi:cloud-check"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ConfigEntry, updates: SteamUpdates) -> None:
        super().__init__(entry, updates, KEY_LAST_ANSWER, LAST_ANSWER_SENSOR_ENTITY_ID)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self._source.restore(None, dt_util.parse_datetime(last.state))
        await super().async_added_to_hass()

    @property
    def native_value(self) -> datetime | None:
        return self._source.last_answer


class SteamQuerySensor(_SourceSensor[SteamUpdates]):
    """ok, stale (no answer for 3 h) or never; shown translated."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "stale", "never"]
    _attr_icon = "mdi:cloud-search"

    def __init__(self, entry: ConfigEntry, updates: SteamUpdates) -> None:
        super().__init__(entry, updates, KEY_QUERY, QUERY_SENSOR_ENTITY_ID)

    @property
    def native_value(self) -> str:
        return self._source.query_state


class SteamNotInstalledSensor(_SourceSensor[SteamOwned], RestoreEntity):
    """Owned games that are not installed, attributes games [{appid, name}], owned, new."""

    _attr_icon = "mdi:tray-arrow-down"
    # Large accounts would go over the recorder's attribute limit; the state (the count) is recorded
    _unrecorded_attributes = frozenset({"games", "owned", "new"})

    def __init__(self, entry: ConfigEntry, owned: SteamOwned) -> None:
        super().__init__(entry, owned, KEY_NOT_INSTALLED, NOT_INSTALLED_SENSOR_ENTITY_ID)

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None:
            games = last.attributes.get("games")
            owned = last.attributes.get("owned")
            extra = await self.async_get_last_extra_data()
            self._source.restore(
                list(games) if isinstance(games, list) else None,
                [str(i) for i in owned] if isinstance(owned, list) else None,
                extra.as_dict().get("steam_id") if extra is not None else None,
            )
        await super().async_added_to_hass()

    @property
    def extra_restore_state_data(self) -> RestoredExtraData:
        """The Steam account the lists were checked for (kept for the restore, not shown)."""
        return RestoredExtraData({"steam_id": self._source.steam_id})

    @property
    def native_value(self) -> int:
        return len(self._source.missing)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "games": self._source.missing,
            "owned": self._source.owned,
            "new": self._source.new,
        }


class SteamDownloadProgressSensor(_SourceSensor[SteamProgress]):
    """Percentage of the running Steam job (0 when none runs), attributes job and queue, and for the
    card move (the move to another drive that runs) and move_result (the last one's end), see
    SteamMove.attributes."""

    _attr_icon = "mdi:progress-download"
    _attr_native_unit_of_measurement = "%"
    # The move changes every few seconds while it runs; the card reads it live, the history needs it not
    _unrecorded_attributes = frozenset({"move", "move_result"})

    def __init__(self, entry: ConfigEntry, progress: SteamProgress, jobs: SteamJobs) -> None:
        super().__init__(entry, progress, KEY_DOWNLOAD_PROGRESS, ENTITY_DOWNLOAD_PROGRESS)
        self._jobs = jobs

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._jobs.async_add_listener(self._on_update))
        self.async_on_remove(self._jobs.move.async_add_listener(self._on_update))
        await super().async_added_to_hass()

    @property
    def native_value(self) -> int:
        return self._source.percent

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"job": self._jobs.job, "queue": self._jobs.queue, **self._jobs.move.attributes}


# The old fixed CDN path, for a game the Steam store has not answered for yet (steam_images.py)
GAME_PICTURE_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"


class SteamCurrentGameSensor(SteamEngineEntity, SensorEntity, RestoreEntity):
    """Name of the Steam game running on the PC (unknown when none runs), attributes appid and
    started; the entity picture is the game's header image (the URL the Steam store gave, else the
    old CDN path)."""

    _attr_icon = "mdi:gamepad-variant"

    def __init__(self, entry: ConfigEntry, agent: AgentEntities, owned: SteamOwned, images: SteamImages) -> None:
        super().__init__(entry, KEY_CURRENT_GAME, ENTITY_CURRENT_GAME)
        self._agent = agent
        self._owned = owned
        self._images = images
        self._appid = ""
        self._started: datetime | None = None
        self._restored: tuple[str, datetime | None] | None = None

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and last.attributes.get("appid"):
            # Keep the start time over a restart of HA if the same game still runs (the agent's
            # sensor may come back only after this one).
            self._restored = (
                str(last.attributes["appid"]),
                dt_util.parse_datetime(str(last.attributes.get("started") or "")),
            )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._agent.game_running, self._agent.agent_online, self._agent.library],
                self._on_state_change,
            )
        )
        self.async_on_remove(self._owned.async_add_listener(self._refresh))
        self.async_on_remove(self._images.async_add_listener(self._refresh))
        self._refresh()

    @callback
    def _on_state_change(self, event: Event[EventStateChangedData]) -> None:
        self._refresh()

    def _running_appid(self) -> str:
        state = self.hass.states.get(self._agent.game_running)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # Tells nothing (HA's MQTT connection is down or reconnecting, presence.py): the game
            # shown keeps running, with its start, unless the agent is known to be gone (PC off)
            online = self.hass.states.get(self._agent.agent_online)
            return self._appid if online is None or online.state != "off" else ""
        if state.state != "on":
            return ""
        appid = str(state.attributes.get("appid") or "")
        return "" if appid in ("", "0") else appid

    def _name(self, appid: str) -> str:
        games = (library_games(self.hass, self._agent) or []) + self._owned.missing
        return known_name(games, appid) or Texts(self.hass)("game_fallback", appid=appid)

    def _picture(self, appid: str) -> str:
        images = self._images.get(appid)
        return images["header"] if images is not None else GAME_PICTURE_URL.format(appid=appid)

    @callback
    def _refresh(self) -> None:
        appid = self._running_appid()
        if self._restored is not None and appid != self._restored[0]:
            # The agent reported that no game or another one runs: the remembered start is void
            state = self.hass.states.get(self._agent.game_running)
            if state is not None and state.state in ("on", "off"):
                self._restored = None
        if appid != self._appid:
            self._appid = appid
            self._started = dt_util.utcnow() if appid else None
            if appid and self._restored is not None:
                if self._restored[0] == appid and self._restored[1] is not None:
                    self._started = self._restored[1]
                self._restored = None
        self._attr_native_value = self._name(appid) if appid else None
        self._attr_entity_picture = self._picture(appid) if appid else None
        self._attr_extra_state_attributes = {
            "appid": appid or None,
            "started": self._started.isoformat() if self._started else None,
        }
        self._write_if_changed((self._attr_native_value, self._attr_entity_picture, self._attr_extra_state_attributes))
