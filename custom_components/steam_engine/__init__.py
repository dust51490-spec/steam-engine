"""Steam Engine: keeps the Steam games of a Windows PC up to date and switches the PC on and off.

The integration talks to a small agent on the PC over MQTT (see PROTOCOL.md). Its parts:
presence.py (whether the PC is on), pc_power.py (wake, auto off, idle prompt, reminder, the PC
switch), steam_progress.py (download state, Steam busy), plugin_busy.py (plugins that keep the PC
busy), steam_updates.py (update check), steam_jobs.py (install, uninstall, move and launch jobs with a
queue, automatic updates), steam_move.py (what a move to another drive reports), active_hours.py (the time of day for automatic work), steam_owned.py
(owned games and new purchases), steam_images.py (the games' image URLs from the Steam store),
steam_notify.py (pushes), steam_events.py (events for automations), game_choice.py with select.py, button.py
and switch.py (pick a game to install, uninstall, start or update automatically) and issues.py
(hints under Repairs). parts.py has what they share. All settings are on one options page;
steam_account.py checks the Steam key and account there, with the Steam Web API requests of
steam_api.py. websocket.py answers the dashboard card (game_size.py: the install size of a game
that is not installed), which the integration serves itself from frontend/ (see
_async_register_card).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import timedelta
import hashlib
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .active_hours import ActiveHours
from .agent_entities import (
    AgentEntities,
    async_reload_on_rename,
    async_reload_when_agent_appears,
    resolve_agent_entities,
)
from .const import CONF_REACHABLE_ENTITY, DOMAIN, SECTION_PC, STORAGE_VERSION, merged_options
from .device import steam_engine_device
from .game_choice import GameChoice
from .game_size import async_remove_store as async_remove_game_sizes
from .issues import SteamEngineIssues, delete_all
from .own_entities import OwnEntities
from .parts import Part, start_task
from .pc import ATTR_PC, UNNAMED, PcNames, entry_pc_names, pick_entry
from .pc_power import PcPower
from .plugin_busy import PluginBusy
from .presence import PcPresence
from .steam_images import SteamImages, async_remove_store as async_remove_images
from .steam_jobs import SteamJobs, parse_appids
from .steam_move import SteamMove
from .steam_events import SteamEvents
from .steam_notify import SteamNotify
from .steam_owned import SteamOwned
from .steam_progress import SteamProgress
from .steam_updates import SteamUpdates
from .websocket import async_register_websocket

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]

# The dashboard card: one JavaScript module, served at this URL and loaded on every dashboard
CARD_URL = f"/{DOMAIN}/steam-engine-card.js"
CARD_PATH = Path(__file__).parent / "frontend" / "steam-engine-card.js"

SERVICE_PC_WAKE = "pc_wake"
SERVICE_PC_SHUTDOWN_WHEN_IDLE = "pc_shutdown_when_idle"
SERVICE_CHECK_UPDATES = "check_updates"
SERVICE_CHECK_OWNED = "check_owned"
SERVICE_INSTALL = "install"
SERVICE_UNINSTALL = "uninstall"
SERVICE_MOVE = "move"
SERVICE_SET_AUTO_UPDATE = "set_auto_update"
SERVICE_SET_MUTED = "set_muted"
SERVICE_LAUNCH = "launch"
SERVICE_REFRESH = "refresh"

# Every action takes "pc": only with several PCs, which one (_loaded_entry)
PC_FIELD = {vol.Optional(ATTR_PC): cv.string}
PC_WAKE_SCHEMA = vol.Schema(
    {
        **PC_FIELD,
        vol.Optional("wait_for_entity"): cv.entity_id,
        vol.Optional("wait_for_state"): cv.string,
        vol.Optional("timeout_minutes", default=6): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
    }
)
NO_FIELDS_SCHEMA = vol.Schema(PC_FIELD)
# The drive for a first install: its letter, with or without the colon ("E", "e:"), sent in lower case;
# empty means none (Steam's default library)
DRIVE = vol.All(
    cv.string,
    str.strip,
    vol.Match(r"^(?:[A-Za-z]:?)?$", msg="drive must be a drive letter"),
    lambda v: v[:1].lower() or None,
)
# install takes several games (a comma separated text or a list), the others exactly one
INSTALL_SCHEMA = vol.Schema(
    {**PC_FIELD, vol.Required("appid"): vol.Any(cv.string, [vol.Coerce(str)]), vol.Optional("drive"): DRIVE}
)
ONE_APPID = vol.All(cv.string, vol.Match(r"^\s*[0-9]+\s*$", msg="appid must be one app ID"))
ONE_APPID_SCHEMA = vol.Schema({**PC_FIELD, vol.Required("appid"): ONE_APPID})
# move: one game and the drive to move it to (a letter, with or without the colon), sent in lower case
MOVE_SCHEMA = vol.Schema(
    {
        **PC_FIELD,
        vol.Required("appid"): ONE_APPID,
        vol.Required("drive"): vol.All(
            cv.string, str.strip, vol.Match(r"^[A-Za-z]:?$", msg="drive must be a drive letter"), lambda v: v[:1].lower()
        ),
    }
)
AUTO_UPDATE_SCHEMA = vol.Schema({**PC_FIELD, vol.Required("appid"): ONE_APPID, vol.Optional("enabled"): cv.boolean})
PC_SHUTDOWN_SCHEMA = vol.Schema(
    {
        **PC_FIELD,
        vol.Optional("max_wait_hours", default=6): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
    }
)


@dataclass
class SteamEngineData:
    agent: AgentEntities
    presence: PcPresence
    power: PcPower
    progress: SteamProgress
    plugin_busy: PluginBusy
    updates: SteamUpdates
    jobs: SteamJobs
    owned: SteamOwned
    device: DeviceInfo
    choice: GameChoice
    images: SteamImages
    move: SteamMove
    names: PcNames = UNNAMED


type SteamEngineConfigEntry = ConfigEntry[SteamEngineData]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_websocket(hass)
    _register_services(hass)
    await _async_register_card(hass)
    return True


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the dashboard card and load it on every dashboard, so it needs no entry under Resources
    (works in storage and YAML dashboards). Once per Home Assistant run: async_setup is not called
    again when the entry reloads, and a second static path for the same URL would fail. The URL
    carries the version and a hash of the file, so browsers and the app fetch the card anew whenever
    it changed (also a new card in the same version), without clearing their cache. It is served
    without Home Assistant's long-term cache headers (31 days)."""
    integration = await async_get_integration(hass, DOMAIN)
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_PATH), cache_headers=False)])
    add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}-{await hass.async_add_executor_job(card_hash)}")


def card_hash() -> str:
    """The first 8 hex characters of the SHA-256 of the card file (in the executor: reads the file)."""
    return hashlib.sha256(CARD_PATH.read_bytes()).hexdigest()[:8]


async def async_setup_entry(hass: HomeAssistant, entry: SteamEngineConfigEntry) -> bool:
    options = merged_options(dict(entry.options))
    names = entry_pc_names(entry.data)
    agent, agent_complete = resolve_agent_entities(hass, names)
    if not agent_complete:
        entry.async_on_unload(async_reload_when_agent_appears(hass, entry.entry_id, names))
    entry.async_on_unload(async_reload_on_rename(hass, entry.entry_id, names))
    issues = SteamEngineIssues(hass, options, agent, names)
    own = OwnEntities(hass, entry.entry_id, names)
    presence = PcPresence(hass, agent, options[SECTION_PC][CONF_REACHABLE_ENTITY] or "")
    progress = SteamProgress(hass, agent, presence)
    plugin_busy = PluginBusy(hass, names)
    power = PcPower(hass, options, agent, own, presence, progress, plugin_busy, names)
    updates = SteamUpdates(hass, entry, options, agent, progress, names)
    hours = ActiveHours(hass, options)
    move = SteamMove(hass, entry, progress, names)
    jobs = SteamJobs(hass, entry, options, power, progress, updates, agent, hours, move, names)
    await jobs.async_load()
    owned = SteamOwned(hass, entry, options, agent, own, jobs, names)
    images = SteamImages(hass, entry, agent, updates, owned, names)
    await images.async_load()
    notify = SteamNotify(hass, entry, options, presence, progress, jobs, updates, owned, agent, names)
    events = SteamEvents(hass, agent, progress, jobs, owned, own, names)
    choice = GameChoice(hass)
    entry.runtime_data = SteamEngineData(
        agent,
        presence,
        power,
        progress,
        plugin_busy,
        updates,
        jobs,
        owned,
        steam_engine_device(hass, entry, names),
        choice,
        images,
        move,
        names,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # In this order: each part reads the ones before it
    parts: tuple[Part, ...] = (
        presence, progress, plugin_busy, power, updates, hours, move, jobs, owned, images, notify, events, issues
    )

    # Start only when HA is running, so the watched entities have their states.
    @callback
    def _start(_hass: HomeAssistant) -> None:
        for part in parts:
            part.async_start()
        # Now that every part listens: the end of the job handed over, when it came while HA was down
        progress.async_found_end()

    entry.async_on_unload(async_at_started(hass, _start))
    for part in parts:
        entry.async_on_unload(part.async_stop)
    entry.async_on_unload(choice.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SteamEngineConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: SteamEngineConfigEntry) -> None:
    names = entry_pc_names(entry.data)
    delete_all(hass, names)
    # A new entry starts with empty lists (no old job or queue that starts by itself)
    await Store(hass, STORAGE_VERSION, names.store_key).async_remove()
    await async_remove_images(hass, names)
    # The game sizes are kept for all PCs together: removed with the last one (an ignored found PC
    # is none)
    if not [e for e in hass.config_entries.async_entries(DOMAIN, include_ignore=False) if e.entry_id != entry.entry_id]:
        await async_remove_game_sizes(hass)


async def _async_options_updated(hass: HomeAssistant, entry: SteamEngineConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _loaded_entry(hass: HomeAssistant, pc: str | None = None) -> SteamEngineConfigEntry:
    """The loaded entry of the PC asked for (the action's field "pc"). Without one: the only PC,
    else the PC without a name, else an error; so automations of one PC keep working when a second
    PC comes. While that PC's entry is not loaded the action fails, it never goes to another PC
    (pick_entry). The services stay registered while an entry reloads."""
    entries: list[SteamEngineConfigEntry] = hass.config_entries.async_entries(DOMAIN, include_ignore=False)
    entry, problem = pick_entry(entries, pc)
    if problem == "not_loaded":
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key=problem)
    if entry is None:
        known = ", ".join(entry_pc_names(e.data).pc_name or "Steam PC" for e in entries)
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=problem,
            translation_placeholders={"pc": pc or "", "pcs": known},
        )
    return entry


async def _respond_or_start(
    hass: HomeAssistant,
    entry: SteamEngineConfigEntry,
    call: ServiceCall,
    target: Coroutine[Any, Any, ServiceResponse],
    name: str,
) -> ServiceResponse:
    """With a response the caller waits for the result. Without, the job runs on its own (waking
    the PC can take minutes, and a plugin's own "PC busy" label would block its shutdown); a reload
    cancels it with a log line."""
    if call.return_response:
        return await target
    start_task(hass, entry, target, name)
    return None


async def _pc_wake(hass: HomeAssistant, entry: SteamEngineConfigEntry, call: ServiceCall) -> ServiceResponse:
    return await entry.runtime_data.power.async_wake(
        call.data.get("wait_for_entity"),
        call.data.get("wait_for_state"),
        timedelta(minutes=call.data["timeout_minutes"]),
    )


async def _pc_shutdown_when_idle(
    hass: HomeAssistant, entry: SteamEngineConfigEntry, call: ServiceCall
) -> ServiceResponse:
    target = entry.runtime_data.power.async_shutdown_when_idle(timedelta(hours=call.data["max_wait_hours"]))
    return await _respond_or_start(hass, entry, call, target, SERVICE_PC_SHUTDOWN_WHEN_IDLE)


async def _check_updates(hass: HomeAssistant, entry: SteamEngineConfigEntry, call: ServiceCall) -> ServiceResponse:
    return await _respond_or_start(hass, entry, call, entry.runtime_data.updates.async_check(), SERVICE_CHECK_UPDATES)


async def _check_owned(hass: HomeAssistant, entry: SteamEngineConfigEntry, call: ServiceCall) -> ServiceResponse:
    return await _respond_or_start(hass, entry, call, entry.runtime_data.owned.async_check(), SERVICE_CHECK_OWNED)


async def _refresh(hass: HomeAssistant, entry: SteamEngineConfigEntry, call: ServiceCall) -> ServiceResponse:
    """Everything fresh: the PC's library (if the agent is online), then updates and purchases."""

    async def refresh() -> dict[str, Any]:
        data = entry.runtime_data
        library = await data.updates.async_refresh_library()
        updates = await data.updates.async_check()
        owned = await data.owned.async_check()
        return {
            "library_refreshed": library,
            "games": updates["games"],
            "skipped": updates["skipped"],
            "new_purchases": [g for g in owned["games"] if g["appid"] in owned["new"]],
            "owned_skipped": owned["skipped"],
        }

    return await _respond_or_start(hass, entry, call, refresh(), SERVICE_REFRESH)


async def _install(hass: HomeAssistant, entry: SteamEngineConfigEntry, call: ServiceCall) -> ServiceResponse:
    target = entry.runtime_data.jobs.async_install(parse_appids(call.data["appid"]), drive=call.data.get("drive"))
    return await _respond_or_start(hass, entry, call, target, SERVICE_INSTALL)


async def _uninstall(hass: HomeAssistant, entry: SteamEngineConfigEntry, call: ServiceCall) -> ServiceResponse:
    target = entry.runtime_data.jobs.async_uninstall(call.data["appid"])
    return await _respond_or_start(hass, entry, call, target, SERVICE_UNINSTALL)


async def _move(hass: HomeAssistant, entry: SteamEngineConfigEntry, call: ServiceCall) -> ServiceResponse:
    target = entry.runtime_data.jobs.async_move(call.data["appid"], call.data["drive"])
    return await _respond_or_start(hass, entry, call, target, SERVICE_MOVE)


async def _launch(hass: HomeAssistant, entry: SteamEngineConfigEntry, call: ServiceCall) -> ServiceResponse:
    jobs = entry.runtime_data.jobs
    if not call.return_response and jobs.game_running():
        # Said here: the answer of a job running on its own would be lost
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="game_running")
    if not call.return_response and jobs.move.active:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="moving")
    return await _respond_or_start(hass, entry, call, jobs.async_launch(call.data["appid"]), SERVICE_LAUNCH)


async def _set_auto_update(hass: HomeAssistant, entry: SteamEngineConfigEntry, call: ServiceCall) -> ServiceResponse:
    result = await entry.runtime_data.jobs.async_set_auto_update(call.data["appid"], call.data.get("enabled"))
    return result if call.return_response else None


async def _set_muted(hass: HomeAssistant, entry: SteamEngineConfigEntry, call: ServiceCall) -> ServiceResponse:
    result = await entry.runtime_data.jobs.async_set_muted(call.data["appid"], call.data.get("enabled"))
    return result if call.return_response else None


ServiceHandler = Callable[[HomeAssistant, SteamEngineConfigEntry, ServiceCall], Awaitable[ServiceResponse]]

SERVICES: tuple[tuple[str, ServiceHandler, vol.Schema], ...] = (
    (SERVICE_PC_WAKE, _pc_wake, PC_WAKE_SCHEMA),
    (SERVICE_PC_SHUTDOWN_WHEN_IDLE, _pc_shutdown_when_idle, PC_SHUTDOWN_SCHEMA),
    (SERVICE_CHECK_UPDATES, _check_updates, NO_FIELDS_SCHEMA),
    (SERVICE_CHECK_OWNED, _check_owned, NO_FIELDS_SCHEMA),
    (SERVICE_REFRESH, _refresh, NO_FIELDS_SCHEMA),
    (SERVICE_INSTALL, _install, INSTALL_SCHEMA),
    (SERVICE_UNINSTALL, _uninstall, ONE_APPID_SCHEMA),
    (SERVICE_MOVE, _move, MOVE_SCHEMA),
    (SERVICE_LAUNCH, _launch, ONE_APPID_SCHEMA),
    (SERVICE_SET_AUTO_UPDATE, _set_auto_update, AUTO_UPDATE_SCHEMA),
    (SERVICE_SET_MUTED, _set_muted, AUTO_UPDATE_SCHEMA),
)


@callback
def _register_services(hass: HomeAssistant) -> None:
    """Once in async_setup, so the services are there while the entry reloads; a call without a
    loaded entry fails with a clear message."""

    def bind(handler: ServiceHandler) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
        async def call_handler(call: ServiceCall) -> ServiceResponse:
            return await handler(hass, _loaded_entry(hass, call.data.get(ATTR_PC)), call)

        return call_handler

    for name, handler, schema in SERVICES:
        hass.services.async_register(
            DOMAIN, name, bind(handler), schema=schema, supports_response=SupportsResponse.OPTIONAL
        )
