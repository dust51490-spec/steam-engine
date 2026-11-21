"""The riskiest paths of the logic: Steam busy between two games of a job and between a job and the
queued one (no shutdown in the middle), the job queue (an aborted job, the retained end state after
a restart or an MQTT reconnect: the end of the job handed over told once by its job ID, an older end
that keeps the next job of the same game; a hand-over a reload stopped), the wake flag without the optional
"PC reachable" sensor (also during an MQTT outage and before HA's MQTT connection is up), who sits at
the PC (input right after the logon counts, the logon itself and a clock set right after the start
do not, also after the agent reconnects; a start by hand through the PC switch with an old wake
flag; a wake that does not bring the PC up takes its flag back, a slow boot keeps it; a game end
counts like input, also over a restart; an idle push a game made outdated does not shut down), the
PC switch, automatic updates that must not be sent again while their own job runs, the update push
while a job runs, and the active hours (automatic work outside them waits in the queue and starts
when they begin, also after a restart, and stays there when the PC cannot be woken then; an update
of a game uninstalled meanwhile is dropped; work by hand never waits; a running job is not stopped
when they end; a late timer or a corrected clock does not turn the window around).

The agent's entities are plain states (their default entity IDs, as before the agent reported),
time moves with async_fire_time_changed, MQTT commands are caught with async_mock_service and
api.steamcmd.net answers through aioclient_mock. Nothing waits for real minutes; every entry is
unloaded at the end, which stops its timers and jobs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import timedelta
import re
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
    async_mock_service,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.steam_engine import protocol
from custom_components.steam_engine.active_hours import next_at
from custom_components.steam_engine.const import (
    ACTION_PC_SHUTDOWN,
    CONF_ACTIVE_ENABLED,
    CONF_ACTIVE_END,
    CONF_ACTIVE_START,
    CONF_AFTER_DOWNLOAD_MINUTES,
    CONF_AFTER_GAME_MINUTES,
    CONF_NOTIFY_SERVICE,
    CONF_PUSH_KINDS,
    CONF_QUIET_AUTO_UPDATES,
    CONF_WAKE_ENTITY,
    DOMAIN,
    ENTITY_AUTO_OFF_ENABLED,
    ENTITY_PC,
    ENTITY_PC_STATE,
    ENTITY_STEAM_BUSY,
    ENTITY_WOKEN_BY_AUTOMATION,
    PUSH_KINDS,
    SAFE_SENSOR_ENTITY_ID,
    SECTION_ACTIVE_HOURS,
    SECTION_IDLE_PROMPT,
    SECTION_NOTIFY,
    SECTION_PC,
    STORAGE_KEY,
    STORAGE_VERSION,
    UPDATES_SENSOR_ENTITY_ID,
    merged_options,
)
from custom_components.steam_engine.push import (
    TAG_NO_SPACE,
    TAG_PC_IDLE,
    TAG_PC_LOCK,
    TAG_PROGRESS,
    TAG_STALLED,
    TAG_UPDATES,
)
from custom_components.steam_engine.steam_jobs import GAME_SEEN_WAIT, QUEUE_DELAY, parse_appids
from custom_components.steam_engine.steam_notify import ACTION_INSTALL_ALL
from custom_components.steam_engine.steam_progress import _progress_time
from custom_components.steam_engine.steam_updates import BUILD_URL

# Default entity IDs of the agent
STATUS = "sensor.steam_pc_install_status"
LIBRARY = "sensor.steam_pc_library"
AGENT_ONLINE = "binary_sensor.steam_pc_agent_online"
GAME_RUNNING = "binary_sensor.steam_pc_game_running"
NETWORK = "sensor.steam_pc_network"
LAST_INPUT = "sensor.steam_pc_last_input"
SHUTDOWN = "button.steam_pc_shutdown"
RESTART = "button.steam_pc_restart"
WAKE = "input_button.wake_pc"
PHONE = "mobile_app_phone"
NOTIFY = {SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: PHONE}}

Setup = Callable[..., Awaitable[MockConfigEntry]]


def _done(**lists: list[Any]) -> dict[str, Any]:
    """Attributes of done/done_with_errors (install.ps1)."""
    keys = ("ok", "failed", "skipped", "no_space", "game_running", "new")
    result: dict[str, Any] = {key: list(lists.get(key, [])) for key in keys}
    result["count"] = sum(len(result[key]) for key in keys[:5])
    return result


def _pc_on(hass: HomeAssistant) -> None:
    """The PC is on and the agent reports; nobody used the PC for two hours, no game runs."""
    hass.states.async_set(NETWORK, "100", {"unit_of_measurement": "B/s"})
    hass.states.async_set(AGENT_ONLINE, "on")
    hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
    hass.states.async_set(SHUTDOWN, "unknown")
    hass.states.async_set(LAST_INPUT, (dt_util.utcnow() - timedelta(hours=2)).isoformat())


def _store(hass_storage: dict[str, Any], **lists: Any) -> None:
    """The lists of steam_jobs.py as left by an earlier run."""
    hass_storage[STORAGE_KEY] = {"version": STORAGE_VERSION, "minor_version": 1, "key": STORAGE_KEY, "data": lists}


def _installs(calls: list[ServiceCall]) -> list[str]:
    """The install commands sent, without their job ID (install:<job>:<appids>, a new one per job)."""
    payloads = [str(c.data["payload"]) for c in calls if str(c.data.get("payload", "")).startswith("install:")]
    return [re.sub(r"^install:[0-9a-f]{8}:", "install:", p) for p in payloads]


def _job_ids(calls: list[ServiceCall]) -> list[str]:
    """The job IDs of the install commands sent."""
    return [m.group(1) for c in calls if (m := re.match(r"install:([0-9a-f]{8}):", str(c.data.get("payload", ""))))]


def _update_pushes(calls: list[ServiceCall]) -> list[ServiceCall]:
    return [
        c for c in calls if any(a.get("action") == ACTION_INSTALL_ALL for a in c.data.get("data", {}).get("actions", []))
    ]


def _build_answer(aioclient_mock: AiohttpClientMocker, appid: str, build: str) -> None:
    """api.steamcmd.net knows this public build of the game."""
    aioclient_mock.get(
        BUILD_URL.format(appid=appid), json={"data": {appid: {"depots": {"branches": {"public": {"buildid": build}}}}}}
    )


async def _settle(hass: HomeAssistant, seconds: float = 0) -> None:
    """Let jobs run on; with seconds, move time on first (timers and the 2 s waits of the jobs)."""
    if seconds:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    for _ in range(3):
        await hass.async_block_till_done()


@pytest.fixture
async def setup_entry(hass: HomeAssistant) -> AsyncGenerator[Setup]:
    entries: list[MockConfigEntry] = []

    async def setup(options: dict[str, Any] | None = None) -> MockConfigEntry:
        entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options(options or {}))
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entries.append(entry)
        return entry

    yield setup
    for entry in entries:
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_job_active() -> None:
    assert protocol.job_active("started", {})
    assert protocol.job_active("downloading", {"appid": "10"})
    assert protocol.job_active("installed", {"appid": "10", "position": 1})
    # A game skipped or failed in the middle of a job: the next game follows
    assert protocol.job_active("skipped", {"appid": "10", "position": 1})
    assert protocol.job_active("error", {"appid": "10", "position": 2})
    assert not protocol.job_active("skipped", {})
    for state in ("done", "done_with_errors", "aborted", "unknown", "unavailable", None):
        assert not protocol.job_active(state, {})
    no_space = [{"appid": "40", "needed_gb": 50, "free_gb": 10, "drive": "F:"}]
    assert protocol.result_appids(_done(ok=["10"], failed=["20"], no_space=no_space)) == {"10", "20", "40"}


def test_disk_activity_counts_as_progress() -> None:
    """In commit and verify only 'updated' moves (real disk activity); downloading comes every 10 s
    unchanged, which is no progress."""
    attrs = {"appid": "10", "position": 1, "count": 1, "phase": "commit", "phase_percent": 0, "percent": 99.9}
    old = State(STATUS, "downloading", {**attrs, "updated": "2026-10-31T12:00:00+00:00"})
    same = State(STATUS, "downloading", {**attrs, "updated": "2026-10-31T12:00:00+00:00"})
    newer = State(STATUS, "downloading", {**attrs, "updated": "2026-10-31T12:00:10+00:00"})
    assert _progress_time(old, same) is None
    assert _progress_time(old, newer) is not None


async def test_steam_busy_between_two_games(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A plugin's pc_shutdown_when_idle in the gap between two games of a job waits for its end."""
    _pc_on(hass)
    await setup_entry()
    presses = async_mock_service(hass, "button", "press")
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_WOKEN_BY_AUTOMATION}, blocking=True)

    hass.states.async_set(STATUS, "started", {"appids": ["10", "20"], "count": 2})
    hass.states.async_set(
        STATUS, "downloading", {"appid": "10", "position": 1, "count": 2, "percent": 40.0, "phase": "download"}
    )
    hass.states.async_set(STATUS, "installed", {"appid": "10", "position": 1, "count": 2, "percent": 100})
    await _settle(hass)
    assert hass.states.get(ENTITY_STEAM_BUSY).state == "on"

    # Without waiting for the answer, like the backup blueprint
    await hass.services.async_call(DOMAIN, "pc_shutdown_when_idle", {}, blocking=True)
    hass.states.async_set(
        STATUS, "skipped", {"appid": "20", "position": 2, "count": 2, "reason_code": "steam_busy"}
    )
    await _settle(hass, 3)
    assert hass.states.get(ENTITY_STEAM_BUSY).state == "on"
    assert presses == []
    # The PC switch refuses too, and says why
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: ENTITY_PC}, blocking=True)
    assert presses == []

    hass.states.async_set(STATUS, "done", _done(ok=["10"], skipped=["20"]))
    await _settle(hass)
    assert hass.states.get(ENTITY_STEAM_BUSY).state == "off"
    await _settle(hass, 3)
    assert len(presses) == 1
    assert presses[0].data[ATTR_ENTITY_ID] in (SHUTDOWN, [SHUTDOWN])


async def test_steam_busy_until_the_queue_moved_on(hass: HomeAssistant, setup_entry: Setup) -> None:
    """Between the end of a job and the queued one (QUEUE_DELAY) Steam stays busy: a waiting
    pc_shutdown_when_idle does not shut the PC down with games queued."""
    _pc_on(hass)
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    presses = async_mock_service(hass, "button", "press")
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_WOKEN_BY_AUTOMATION}, blocking=True)
    await hass.services.async_call(DOMAIN, "install", {"appid": "10"}, blocking=True)
    await _settle(hass)
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass, 3)
    assert (await jobs.async_install(["20"]))["queued"]
    await hass.services.async_call(DOMAIN, "pc_shutdown_when_idle", {}, blocking=True)

    hass.states.async_set(STATUS, "done", _done(ok=["10"]))
    await _settle(hass, 3)
    assert hass.states.get(ENTITY_STEAM_BUSY).state == "on"
    assert presses == []
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10,", "install:20,"]
    assert presses == []
    hass.states.async_set(STATUS, "started", {"appids": ["20"], "count": 1})
    await _settle(hass, 3)
    assert hass.states.get(ENTITY_STEAM_BUSY).state == "on"
    assert presses == []


async def test_mqtt_reconnect_replays_an_old_end(hass: HomeAssistant, setup_entry: Setup) -> None:
    """On an MQTT reconnect HA writes the job state it had, then gets the retained end of an older
    job. The running job goes on: no aborted push, its games stay, Steam stays busy, no shutdown."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _pc_on(hass)
    aborted = {"reason": "The job was interrupted (PC off or script stopped). Start it again."}
    hass.states.async_set(STATUS, "aborted", aborted)  # retained end of an older job
    entry = await setup_entry(NOTIFY)
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    presses = async_mock_service(hass, "button", "press")
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_WOKEN_BY_AUTOMATION}, blocking=True)
    await hass.services.async_call(DOMAIN, "install", {"appid": "10"}, blocking=True)
    await _settle(hass)
    downloading = {"appid": "10", "position": 1, "count": 1, "percent": 40.0, "phase": "download"}
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    hass.states.async_set(STATUS, "downloading", downloading)
    await _settle(hass, 3)
    assert jobs.job == ["10"]
    await hass.services.async_call(DOMAIN, "pc_shutdown_when_idle", {}, blocking=True)
    pushes_before = len(pushes)

    # HA's MQTT connection drops and comes back: unavailable, the cached state, the retained end
    hass.states.async_set(STATUS, "unavailable")
    await _settle(hass, 3)
    assert hass.states.get(ENTITY_STEAM_BUSY).state == "on"
    hass.states.async_set(STATUS, "downloading", downloading)
    hass.states.async_set(STATUS, "aborted", aborted)
    await _settle(hass, 3)
    assert jobs.job == ["10"]
    assert hass.states.get(ENTITY_STEAM_BUSY).state == "on"
    assert presses == []
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10,"]  # not sent again: the job runs
    assert len(pushes) == pushes_before
    assert presses == []

    # The job goes on and ends: now the PC may shut down
    hass.states.async_set(STATUS, "downloading", {**downloading, "percent": 60.0})
    hass.states.async_set(STATUS, "done", _done(ok=["10"]))
    await _settle(hass)
    assert jobs.job == []
    assert hass.states.get(ENTITY_STEAM_BUSY).state == "off"
    await _settle(hass, 3)
    assert len(presses) == 1


async def test_wake_flag_cleared_without_reachable_sensor(hass: HomeAssistant, setup_entry: Setup) -> None:
    """Without a "PC reachable" sensor the agent's telemetry tells that the PC is off."""
    _pc_on(hass)
    await setup_entry()
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_WOKEN_BY_AUTOMATION}, blocking=True)
    assert hass.states.get(ENTITY_PC).state == "on"

    # Shut down: the agent's last will, and its network sensor expires
    hass.states.async_set(AGENT_ONLINE, "off")
    hass.states.async_set(NETWORK, "unavailable")
    await _settle(hass)
    assert hass.states.get(ENTITY_PC).state == "off"
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "on"

    await _settle(hass, 3 * 60)
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "off"


async def test_mqtt_outage_keeps_the_wake_flag(hass: HomeAssistant, setup_entry: Setup) -> None:
    """HA's own MQTT outage makes all agent entities unavailable at once: that says nothing about
    the PC, so it stays on and keeps its wake flag."""
    _pc_on(hass)
    await setup_entry()
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_WOKEN_BY_AUTOMATION}, blocking=True)
    hass.states.async_set(AGENT_ONLINE, "unavailable")
    hass.states.async_set(NETWORK, "unavailable")
    await _settle(hass, 3 * 60)
    assert hass.states.get(ENTITY_PC).state == "on"
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "on"


async def test_pc_switch_on(hass: HomeAssistant, setup_entry: Setup) -> None:
    """Switching on presses the wake entity only while the PC is off."""
    presses = async_mock_service(hass, "input_button", "press")
    _pc_on(hass)
    await setup_entry({SECTION_PC: {CONF_WAKE_ENTITY: WAKE}})
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_PC}, blocking=True)
    assert presses == []

    # Shut down: the agent's last will, and its network sensor expires
    hass.states.async_set(AGENT_ONLINE, "off")
    hass.states.async_set(NETWORK, "unavailable")
    await _settle(hass)
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_PC}, blocking=True)
    assert len(presses) == 1


async def test_pc_switch_on_without_wake_entity(hass: HomeAssistant, setup_entry: Setup) -> None:
    await setup_entry()
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_PC}, blocking=True)
    assert err.value.translation_key == "no_wake_entity"


async def test_wake_flag_kept_while_mqtt_is_not_up_at_the_start(hass: HomeAssistant, setup_entry: Setup) -> None:
    """HA starts before its MQTT connection is up: the agent's entities tell nothing, so the restored
    wake flag stays, also when the PC then turns out to be on (that is no start of the PC)."""
    mock_restore_cache(hass, [State(ENTITY_WOKEN_BY_AUTOMATION, "on")])
    hass.states.async_set(AGENT_ONLINE, "unavailable")
    hass.states.async_set(NETWORK, "unavailable")
    await setup_entry()
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "on"
    await _settle(hass, 3 * 60)
    await _settle(hass, 6 * 60)
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "on"

    _pc_on(hass)
    await _settle(hass)
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "on"


async def test_input_right_after_the_logon_counts(hass: HomeAssistant, setup_entry: Setup) -> None:
    """Someone sits down at an automatically woken PC right after it came on: their input takes the
    PC over, and a plugin's shut down when idle leaves it on. The first last input the agent reports
    is the logon's own and does not count, also with the PC's clock ahead of Home Assistant's."""
    await setup_entry()
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_WOKEN_BY_AUTOMATION}, blocking=True)
    presses = async_mock_service(hass, "button", "press")
    # The PC comes on, its agent reports; the logon a bit "after now" by the PC's clock
    hass.states.async_set(NETWORK, "100", {"unit_of_measurement": "B/s"})
    hass.states.async_set(AGENT_ONLINE, "on")
    hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
    hass.states.async_set(SHUTDOWN, "unknown")
    logon = dt_util.utcnow() + timedelta(seconds=20)
    hass.states.async_set(LAST_INPUT, logon.isoformat())
    await _settle(hass)
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "on"
    # The PC sets its clock a few seconds right after the start: no input
    hass.states.async_set(LAST_INPUT, (logon + timedelta(seconds=5)).isoformat())
    await _settle(hass)
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "on"

    hass.states.async_set(LAST_INPUT, (logon + timedelta(seconds=50)).isoformat())
    await _settle(hass)
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "off"
    # Even with the flag on again (a wake path of one's own), the fresh input keeps the PC on
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_WOKEN_BY_AUTOMATION}, blocking=True)
    result = await hass.services.async_call(DOMAIN, "pc_shutdown_when_idle", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert result == {"shutdown": False, "reason": "user_took_over"}
    assert presses == []


async def test_agent_back_with_the_same_last_input(hass: HomeAssistant, setup_entry: Setup) -> None:
    """The agent reconnects (a short network gap) and reports the same last input: that stays the
    base, the next input counts."""
    _pc_on(hass)
    await setup_entry()
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_WOKEN_BY_AUTOMATION}, blocking=True)
    hass.states.async_set(AGENT_ONLINE, "off")
    await _settle(hass)
    hass.states.async_set(AGENT_ONLINE, "on")
    await _settle(hass)
    hass.states.async_set(LAST_INPUT, dt_util.utcnow().isoformat())
    await _settle(hass)
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "off"


async def test_failed_wake_takes_its_flag_back(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A wake that has not brought the PC up when its window ends takes its wake flag back: a later
    start (by hand) is not that wake's. A PC that comes up late, after the caller gave up but within
    the window, still counts as woken."""
    async_mock_service(hass, "input_button", "press")
    await setup_entry({SECTION_PC: {CONF_WAKE_ENTITY: WAKE}})
    with patch("custom_components.steam_engine.pc_power.wait_until", AsyncMock(return_value=False)):
        result = await hass.services.async_call(DOMAIN, "pc_wake", {}, blocking=True, return_response=True)
    assert result == {"woken": True, "ready": False}
    await _settle(hass, 10 * 60)
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "on"
    await _settle(hass, 21 * 60)
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "off"


async def test_slow_boot_keeps_the_wake_flag(hass: HomeAssistant, setup_entry: Setup) -> None:
    async_mock_service(hass, "input_button", "press")
    await setup_entry({SECTION_PC: {CONF_WAKE_ENTITY: WAKE}})
    with patch("custom_components.steam_engine.pc_power.wait_until", AsyncMock(return_value=False)):
        await hass.services.async_call(DOMAIN, "pc_wake", {}, blocking=True, return_response=True)
    await _settle(hass, 9 * 60)
    _pc_on(hass)
    await _settle(hass, 21 * 60)
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "on"


async def test_switched_on_by_hand_with_an_old_wake_flag(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A wake flag left on from before a restart: switching the PC on by hand through the PC switch
    is a start by hand (no auto off), though it presses the same wake entity."""
    mock_restore_cache(hass, [State(ENTITY_WOKEN_BY_AUTOMATION, "on")])

    async def press(_call: ServiceCall) -> None:
        hass.states.async_set(WAKE, dt_util.utcnow().isoformat())

    hass.services.async_register("input_button", "press", press)
    hass.states.async_set(AGENT_ONLINE, "off")
    hass.states.async_set(NETWORK, "unavailable")
    await setup_entry({SECTION_PC: {CONF_WAKE_ENTITY: WAKE}})
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "on"

    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_PC}, blocking=True)
    assert hass.states.get(WAKE) is not None
    _pc_on(hass)
    await _settle(hass)
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "off"


async def test_no_second_press_while_the_pc_starts(
    hass: HomeAssistant, setup_entry: Setup, freezer: FrozenDateTimeFactory
) -> None:
    """Wakes at the same time share one press of the wake entity, and neither a later wake nor the
    PC switch presses it again while the PC still starts from it (a device at the power button would
    switch the PC off again). Once the boot window is over, a wake presses again."""
    presses = async_mock_service(hass, "input_button", "press")
    await setup_entry({SECTION_PC: {CONF_WAKE_ENTITY: WAKE}})
    with patch("custom_components.steam_engine.pc_power.wait_until", AsyncMock(return_value=False)):
        results = await asyncio.gather(
            hass.services.async_call(DOMAIN, "pc_wake", {}, blocking=True, return_response=True),
            hass.services.async_call(DOMAIN, "pc_wake", {}, blocking=True, return_response=True),
        )
        assert results == [{"woken": True, "ready": False}] * 2
        assert len(presses) == 1
        freezer.tick(timedelta(minutes=1))
        await hass.services.async_call(DOMAIN, "pc_wake", {}, blocking=True, return_response=True)
        await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_PC}, blocking=True)
        assert len(presses) == 1

        freezer.tick(timedelta(minutes=3))
        await hass.services.async_call(DOMAIN, "pc_wake", {}, blocking=True, return_response=True)
    assert len(presses) == 2
    assert hass.states.get(ENTITY_WOKEN_BY_AUTOMATION).state == "on"


def _pc_off(hass: HomeAssistant) -> None:
    """Shut down: the agent's last will, and its network sensor expires."""
    hass.states.async_set(AGENT_ONLINE, "off")
    hass.states.async_set(NETWORK, "unavailable")


async def test_pc_state_while_starting(
    hass: HomeAssistant, setup_entry: Setup, freezer: FrozenDateTimeFactory
) -> None:
    """The PC state shows starting from the press of the wake entity until the PC is on, and goes
    back to off when the PC does not come within the boot window."""
    async_mock_service(hass, "input_button", "press")
    _pc_off(hass)
    await setup_entry({SECTION_PC: {CONF_WAKE_ENTITY: WAKE}})
    assert hass.states.get(ENTITY_PC_STATE).state == "off"

    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_PC}, blocking=True)
    assert hass.states.get(ENTITY_PC_STATE).state == "starting"
    assert hass.states.get(ENTITY_PC).state == "off"
    _pc_on(hass)
    await _settle(hass)
    assert hass.states.get(ENTITY_PC_STATE).state == "on"

    _pc_off(hass)
    await _settle(hass)
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_PC}, blocking=True)
    assert hass.states.get(ENTITY_PC_STATE).state == "starting"
    freezer.tick(timedelta(minutes=3))
    await _settle(hass, 30)
    assert hass.states.get(ENTITY_PC_STATE).state == "off"


async def test_pc_state_shutting_down_and_restarting(
    hass: HomeAssistant, setup_entry: Setup, freezer: FrozenDateTimeFactory
) -> None:
    """A press of the agent's shutdown button (by anyone) shows shutting down until the PC is off,
    one of its restart button restarting until the agent is back, also while the PC counts as off
    in between; the buttons turning unavailable with the agent change nothing."""
    _pc_on(hass)
    await setup_entry()
    assert hass.states.get(ENTITY_PC_STATE).state == "on"

    freezer.tick(timedelta(seconds=5))
    hass.states.async_set(SHUTDOWN, dt_util.utcnow().isoformat())
    await _settle(hass)
    assert hass.states.get(ENTITY_PC_STATE).state == "shutting_down"
    hass.states.async_set(AGENT_ONLINE, "off")
    hass.states.async_set(SHUTDOWN, "unavailable")
    await _settle(hass)
    assert hass.states.get(ENTITY_PC_STATE).state == "shutting_down"  # the network sensor has not expired yet
    hass.states.async_set(NETWORK, "unavailable")
    await _settle(hass)
    assert hass.states.get(ENTITY_PC_STATE).state == "off"

    _pc_on(hass)
    await _settle(hass)
    assert hass.states.get(ENTITY_PC_STATE).state == "on"
    freezer.tick(timedelta(seconds=5))
    hass.states.async_set(RESTART, dt_util.utcnow().isoformat())
    await _settle(hass)
    assert hass.states.get(ENTITY_PC_STATE).state == "restarting"
    _pc_off(hass)
    hass.states.async_set(RESTART, "unavailable")
    await _settle(hass)
    assert hass.states.get(ENTITY_PC_STATE).state == "restarting"
    freezer.tick(timedelta(seconds=40))
    _pc_on(hass)
    await _settle(hass)
    assert hass.states.get(ENTITY_PC_STATE).state == "on"


async def test_game_end_counts_like_input(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A game played with a controller leaves no input: right after it ends the PC is not safe to
    shut down, the idle time counts from its end."""
    _pc_on(hass)
    await setup_entry()
    await _settle(hass, 30)
    assert hass.states.get(SAFE_SENSOR_ENTITY_ID).state == "on"

    hass.states.async_set(GAME_RUNNING, "on", {"appid": 10})
    await _settle(hass, 30)
    assert hass.states.get(SAFE_SENSOR_ENTITY_ID).state == "off"
    hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
    await _settle(hass, 30)
    assert hass.states.get(SAFE_SENSOR_ENTITY_ID).state == "off"


async def test_game_end_counts_over_a_restart(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A game that ended just before a restart (or a reload) still counts like input."""
    mock_restore_cache_with_extra_data(
        hass, [(State(SAFE_SENSOR_ENTITY_ID, "off"), {"game_ended": dt_util.utcnow().isoformat()})]
    )
    _pc_on(hass)
    await setup_entry()
    await _settle(hass, 30)
    assert hass.states.get(SAFE_SENSOR_ENTITY_ID).state == "off"


async def _idle_push(hass: HomeAssistant, setup_entry: Setup) -> tuple[list[ServiceCall], list[ServiceCall]]:
    """A PC started by hand, idle for two hours: the idle push with its shut down button is sent."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _pc_on(hass)
    await setup_entry({**NOTIFY, SECTION_IDLE_PROMPT: {CONF_AFTER_DOWNLOAD_MINUTES: 0, CONF_AFTER_GAME_MINUTES: 0}})
    presses = async_mock_service(hass, "button", "press")
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_AUTO_OFF_ENABLED}, blocking=True)
    await _settle(hass)
    assert any(
        c.data.get("data", {}).get("tag") == TAG_PC_IDLE and c.data["data"].get("actions") for c in pushes
    )
    return pushes, presses


async def test_idle_push_shuts_down(hass: HomeAssistant, setup_entry: Setup) -> None:
    pushes, presses = await _idle_push(hass, setup_entry)
    hass.bus.async_fire("mobile_app_notification_action", {"action": ACTION_PC_SHUTDOWN})
    await _settle(hass)
    assert len(presses) == 1
    assert presses[0].data[ATTR_ENTITY_ID] in (SHUTDOWN, [SHUTDOWN])


async def test_idle_push_outdated_by_a_game(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A game starts (played with a controller, no input): the idle push is removed, and its shut
    down button, still on another phone, no longer shuts the PC down."""
    pushes, presses = await _idle_push(hass, setup_entry)
    hass.states.async_set(GAME_RUNNING, "on", {"appid": 10})
    await _settle(hass)
    assert pushes[-1].data["message"] == "clear_notification"
    assert pushes[-1].data["data"]["tag"] == TAG_PC_IDLE

    hass.bus.async_fire("mobile_app_notification_action", {"action": ACTION_PC_SHUTDOWN})
    await _settle(hass)
    assert presses == []
    assert pushes[-1].data["data"]["tag"] == TAG_PC_LOCK


async def test_aborted_job_goes_on_with_the_queue(hass: HomeAssistant, setup_entry: Setup) -> None:
    """An interrupted job goes on by itself, together with the games queued behind it."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _pc_on(hass)
    hass.states.async_set(STATUS, "done", _done(ok=["99"]))  # the end of an earlier job
    entry = await setup_entry(NOTIFY)
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")

    await hass.services.async_call(DOMAIN, "install", {"appid": "10"}, blocking=True)
    await _settle(hass)
    assert _installs(publishes) == ["install:10,"]
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass, 3)
    assert jobs.job == ["10"]

    # While the job runs, new games wait; a game the job or the queue has is not added again
    assert await jobs.async_install(["20"]) == {"queued": True, "sent": False}
    assert (await jobs.async_install(["10", "20"]))["reason"] == "already_queued"
    assert jobs.queue == ["20"]

    # The PC was switched off in the middle (report.ps1 says so after the next start)
    hass.states.async_set(STATUS, "aborted", {"reason": "The job was interrupted"})
    await _settle(hass)
    assert jobs.job == ["10"]
    aborted = [c for c in pushes if c.data.get("data", {}).get("tag") == TAG_STALLED]
    assert len(aborted) == 1
    assert "Game 10" in aborted[0].data["message"]
    assert "Game 20" in aborted[0].data["message"]
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10,", "install:10,20,"]
    assert jobs.job == ["10", "20"]
    assert jobs.queue == []


async def test_interrupted_job_does_not_block_installs(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A job state left over from a PC that went off in the middle does not queue new games."""
    _pc_on(hass)
    hass.states.async_set(STATUS, "downloading", {"appid": "10", "position": 1, "count": 1, "percent": 20.0})
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    assert jobs.job_running()

    hass.states.async_set(AGENT_ONLINE, "off")
    hass.states.async_set(NETWORK, "unavailable")
    await _settle(hass)
    assert not jobs.job_running()
    # No wake entity: the job cannot go out, but it is not queued behind the dead one
    result = await hass.services.async_call(DOMAIN, "install", {"appid": "20"}, blocking=True, return_response=True)
    assert result == {"queued": False, "sent": False, "woken": False}
    assert jobs.job == ["20"]
    assert jobs.queue == []


async def test_retained_end_after_restart(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """The job handed over ended while HA was down: its end, found at the start, is told once (the
    games it names leave the job list, the result push replaces the progress push)."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _store(hass_storage, job=["10"], queue=["20"], job_id="0000000b", end_id="0000000a")
    _pc_on(hass)
    hass.states.async_set(STATUS, "done", {**_done(ok=["10"]), "job": "0000000b"})
    entry = await setup_entry(NOTIFY)
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    await _settle(hass)
    assert jobs.job == []
    assert jobs.queue == ["20"]
    [result] = [c for c in pushes if c.data.get("data", {}).get("tag") == TAG_PROGRESS]
    assert result.data["data"]["sticky"] is False
    assert entry.runtime_data.progress.end_id == "0000000b"

    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:20,"]
    assert jobs.job == ["20"]


async def test_old_end_read_again_keeps_the_next_job(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """The end of the last job, told already, read again after a restart while the next job of the
    same game waits for the PC: the game stays in the job list and goes out with a new job ID; an
    MQTT reconnect reading that old end again does not end the job handed over either."""
    _store(hass_storage, job=["10"], job_id="0000000a", end_id="0000000a")
    _pc_on(hass)
    old_end = {**_done(ok=["10"]), "job": "0000000a"}
    hass.states.async_set(STATUS, "done", old_end)
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    await _settle(hass)
    assert jobs.job == ["10"]

    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10,"]
    [job] = _job_ids(publishes)
    assert job != "0000000a"
    assert entry.runtime_data.progress.job_id == job

    hass.states.async_set(STATUS, "unavailable")
    hass.states.async_set(STATUS, "done", old_end)
    await _settle(hass)
    assert jobs.job == ["10"]


async def test_end_missed_while_the_connection_was_down(hass: HomeAssistant, setup_entry: Setup) -> None:
    """The end of the job handed over came while HA's MQTT connection was down: read after the
    reconnect it is told once, its result replacing the progress push; read again, it is not."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _pc_on(hass)
    entry = await setup_entry(NOTIFY)
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    await hass.services.async_call(DOMAIN, "install", {"appid": "10"}, blocking=True)
    await _settle(hass)
    [job] = _job_ids(publishes)
    downloading = {"appid": "10", "position": 1, "count": 1, "percent": 40.0, "phase": "download", "job": job}
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1, "job": job})
    hass.states.async_set(STATUS, "downloading", downloading)
    await _settle(hass, 3)
    progress = [c for c in pushes if c.data.get("data", {}).get("tag") == TAG_PROGRESS]
    assert progress[-1].data["data"]["sticky"] is True

    # The connection drops and the job ends meanwhile: HA writes the state it had, then gets the end
    done = {**_done(ok=["10"]), "job": job}
    hass.states.async_set(STATUS, "unavailable")
    hass.states.async_set(STATUS, "downloading", downloading)
    hass.states.async_set(STATUS, "done", done)
    await _settle(hass)
    assert jobs.job == []
    result = [c for c in pushes if c.data.get("data", {}).get("tag") == TAG_PROGRESS][-1]
    assert result.data["data"]["sticky"] is False
    assert not entry.runtime_data.progress.job_running

    count = len(pushes)
    hass.states.async_set(STATUS, "unavailable")
    hass.states.async_set(STATUS, "done", done)
    await _settle(hass)
    assert len(pushes) == count


async def test_end_of_the_job_seen_running(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """A command sent again while a job ran unseen (HA restarted between two of its games) is left
    out by the PC: the job seen running is the one whose end counts (its job ID is taken over and
    kept), also when that end is read only after an MQTT reconnect."""
    _store(hass_storage, job=["10", "20"], job_id="0000000c", end_id="0000000a")
    _pc_on(hass)
    hass.states.async_set(STATUS, "done", {**_done(ok=["5"]), "job": "0000000a"})
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    progress = entry.runtime_data.progress
    downloading = {"appid": "20", "position": 2, "count": 2, "percent": 40.0, "phase": "download", "job": "0000000b"}
    hass.states.async_set(STATUS, "downloading", downloading)
    await _settle(hass)
    assert progress.job_id == "0000000b"
    assert hass_storage[STORAGE_KEY]["data"]["job_id"] == "0000000b"

    # The connection drops and the job ends meanwhile: HA writes the state it had, then gets the end
    hass.states.async_set(STATUS, "unavailable")
    hass.states.async_set(STATUS, "downloading", downloading)
    hass.states.async_set(STATUS, "done", {**_done(ok=["10", "20"]), "job": "0000000b"})
    await _settle(hass)
    assert jobs.job == []
    assert progress.end_id == "0000000b"


async def test_retained_end_of_another_job(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """An end state that does not name the job's games is not taken for its end: the games stay
    and go out again with the queue (the PC leaves out those of a job that still runs)."""
    _store(hass_storage, job=["10"], queue=["20"])
    _pc_on(hass)
    hass.states.async_set(STATUS, "done", _done(ok=["99"]))
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    await _settle(hass)
    assert jobs.job == ["10"]

    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10,20,"]


async def test_hand_over_stopped_by_a_reload_goes_out(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """A job left in the job list without any end state known (a reload stopped its hand-over, a
    broker without retained messages) goes out once the agent is online."""
    _store(hass_storage, job=["10"])
    _pc_on(hass)
    await setup_entry()
    publishes = async_mock_service(hass, "mqtt", "publish")
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10,"]


async def test_auto_update_not_sent_again_while_its_job_runs(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    setup_entry: Setup,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    _store(hass_storage, job=["10"], auto_update=["10"])
    _pc_on(hass)
    hass.states.async_set(LIBRARY, "1", {"games": [{"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}]})
    hass.states.async_set(
        STATUS, "downloading", {"appid": "10", "position": 1, "count": 1, "percent": 20.0, "phase": "download"}
    )
    _build_answer(aioclient_mock, "10", "2")
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    assert jobs.job_running()

    # The library keeps the old build until the job is done: the check still finds the update
    result = await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    assert [g["appid"] for g in result["games"]] == ["10"]
    await _settle(hass)
    assert _installs(publishes) == []
    assert jobs.queue == []

    # The job failed: the next check starts the update once
    hass.states.async_set(STATUS, "done_with_errors", _done(failed=["10"]))
    await _settle(hass)
    assert jobs.job == []
    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert _installs(publishes) == ["install:10,"]
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass, 3)

    # A check while that job runs sends nothing again
    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert _installs(publishes) == ["install:10,"]
    assert jobs.queue == []


async def test_update_push_waits_for_the_running_job(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    setup_entry: Setup,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """No update push while a job runs (the check after its end announces what is still open), and
    never a game that a job has."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _store(hass_storage, job=["10"])
    _pc_on(hass)
    alpha = {"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}
    beta = {"appid": "20", "name": "Beta", "buildid": "1", "size_mb": 100}
    hass.states.async_set(LIBRARY, "2", {"games": [alpha, beta]})
    hass.states.async_set(
        STATUS, "downloading", {"appid": "10", "position": 1, "count": 1, "percent": 20.0, "phase": "download"}
    )
    _build_answer(aioclient_mock, "10", "2")
    _build_answer(aioclient_mock, "20", "2")
    await setup_entry(NOTIFY)
    async_mock_service(hass, "mqtt", "publish")

    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert _update_pushes(pushes) == []

    # The job is done; asked for it after the download, the agent reports the new build of Alpha,
    # and the check after the download runs (without its pause between two requests)
    with patch("custom_components.steam_engine.steam_updates.REQUEST_PAUSE", 0):
        hass.states.async_set(STATUS, "done", _done(ok=["10"]))
        await _settle(hass)
        hass.states.async_set(LIBRARY, "2", {"games": [{**alpha, "buildid": "2"}, beta]})
        await _settle(hass)
    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    updates = _update_pushes(pushes)
    assert len(updates) == 1
    assert updates[0].data["message"] == "Beta"
    assert updates[0].data["data"]["tag"] == TAG_UPDATES


async def test_update_push_stays_after_a_job_of_another_game(
    hass: HomeAssistant, setup_entry: Setup, aioclient_mock: AiohttpClientMocker
) -> None:
    """The update push has a tag of its own: the end of a job of another game clears the job's
    progress bar, not the announcement of games that count as announced. It goes once no update
    is left to announce."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _pc_on(hass)
    alpha = {"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}
    beta = {"appid": "20", "name": "Beta", "buildid": "1", "size_mb": 100}
    hass.states.async_set(LIBRARY, "2", {"games": [alpha, beta]})
    _build_answer(aioclient_mock, "10", "1")
    _build_answer(aioclient_mock, "20", "2")
    entry = await setup_entry({SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: PHONE, CONF_PUSH_KINDS: ["updates"]}})
    async_mock_service(hass, "mqtt", "publish")
    cleared = {"message": "clear_notification", "data": {"tag": TAG_UPDATES}}

    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert [u.data["data"]["tag"] for u in _update_pushes(pushes)] == [TAG_UPDATES]
    assert entry.runtime_data.jobs.reported == ["20"]

    # A job of Alpha ends: its progress bar is cleared, the announcement of Beta stays
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass)
    hass.states.async_set(STATUS, "done", _done(ok=["10"]))
    await _settle(hass, 3)
    assert {"message": "clear_notification", "data": {"tag": TAG_PROGRESS}} in [c.data for c in pushes]
    assert cleared not in [c.data for c in pushes]

    # Beta is up to date: nothing is left to announce, the push goes
    hass.states.async_set(LIBRARY, "2", {"games": [alpha, {**beta, "buildid": "2"}]})
    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert pushes[-1].data == cleared
    assert entry.runtime_data.jobs.reported == []


async def test_check_without_answer_after_an_update(
    hass: HomeAssistant, setup_entry: Setup, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without an answer from api.steamcmd.net a game stays outdated only while the build it was
    found with is still installed: one updated meanwhile is not outdated any more."""
    _pc_on(hass)
    alpha = {"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}
    beta = {"appid": "20", "name": "Beta", "buildid": "1", "size_mb": 100}
    hass.states.async_set(LIBRARY, "2", {"games": [alpha, beta]})
    _build_answer(aioclient_mock, "10", "2")
    _build_answer(aioclient_mock, "20", "2")
    entry = await setup_entry()
    async_mock_service(hass, "mqtt", "publish")
    updates = entry.runtime_data.updates
    with patch("custom_components.steam_engine.steam_updates.REQUEST_PAUSE", 0):
        await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
        assert [g["appid"] for g in updates.outdated] == ["10", "20"]

        # Alpha was updated; api.steamcmd.net does not answer
        aioclient_mock.clear_requests()
        aioclient_mock.get(BUILD_URL.format(appid="10"), status=500)
        aioclient_mock.get(BUILD_URL.format(appid="20"), status=500)
        hass.states.async_set(LIBRARY, "2", {"games": [{**alpha, "buildid": "2"}, beta]})
        await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    assert [g["appid"] for g in updates.outdated] == ["20"]
    assert updates.installed_builds == {"20": 1}


async def test_hourly_check_counts_from_its_start(
    hass: HomeAssistant, setup_entry: Setup, freezer: FrozenDateTimeFactory
) -> None:
    """A check that takes longer than the margin of the interval does not make the next check on
    the hour fall out: the interval counts from the start of the check."""
    freezer.move_to("2026-11-20 10:00:00+00:00")
    _pc_on(hass)
    hass.states.async_set(LIBRARY, "1", {"games": [{"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}]})
    await setup_entry()
    async_mock_service(hass, "mqtt", "publish")

    async def slow_answer(*_args: Any, **_kwargs: Any) -> None:
        freezer.tick(timedelta(minutes=10))  # no answer, after 10 minutes

    with patch("custom_components.steam_engine.steam_updates.async_get_app_info", side_effect=slow_answer) as get:
        await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
        assert get.call_count == 1
        freezer.move_to("2026-11-20 11:00:00+00:00")
        async_fire_time_changed(hass, dt_util.utcnow())
        await _settle(hass)
    assert get.call_count == 2


def _active_hours(open_now: bool) -> dict[str, Any]:
    """Active hours by the local clock of the test instance: open now (from an hour ago for three
    hours), or closed (from two hours ahead for an hour)."""
    now = dt_util.now()
    if open_now:
        start, end = now - timedelta(hours=1), now + timedelta(hours=2)
    else:
        start, end = now + timedelta(hours=2), now + timedelta(hours=3)
    return {
        SECTION_ACTIVE_HOURS: {
            CONF_ACTIVE_ENABLED: True,
            CONF_ACTIVE_START: start.strftime("%H:%M:00"),
            CONF_ACTIVE_END: end.strftime("%H:%M:00"),
        }
    }


def _updates_attributes(hass: HomeAssistant) -> dict[str, Any]:
    return dict(hass.states.get(UPDATES_SENSOR_ENTITY_ID).attributes)


async def test_without_active_hours_nothing_waits(hass: HomeAssistant, setup_entry: Setup) -> None:
    """Switched off (the default) or all day, the window is always open and the sensor says so."""
    entry = await setup_entry()
    assert entry.runtime_data.jobs.hours.is_open()
    attrs = _updates_attributes(hass)
    assert attrs["active_hours"] is None
    assert attrs["deferred"] == []
    assert attrs["deferred_until"] is None


async def test_all_day_active_hours(hass: HomeAssistant, setup_entry: Setup) -> None:
    all_day = {CONF_ACTIVE_ENABLED: True, CONF_ACTIVE_START: "08:00:00", CONF_ACTIVE_END: "08:00:00"}
    entry = await setup_entry({SECTION_ACTIVE_HOURS: all_day})
    assert entry.runtime_data.jobs.hours.is_open()
    attrs = _updates_attributes(hass)
    assert attrs["active_hours"] == {"start": "08:00", "end": "08:00"}
    assert attrs["deferred_until"] is None


async def test_auto_update_waits_for_the_active_hours(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    setup_entry: Setup,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An automatic update found outside the active hours waits in the queue (no job, no wake) and
    starts by itself when they begin."""
    _store(hass_storage, auto_update=["10"])
    _pc_on(hass)
    hass.states.async_set(LIBRARY, "1", {"games": [{"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}]})
    _build_answer(aioclient_mock, "10", "2")
    options = _active_hours(open_now=False)
    entry = await setup_entry(options)
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    assert not jobs.hours.is_open()

    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert _installs(publishes) == []
    assert jobs.queue == ["10"]
    start = jobs.hours.next_start
    attrs = _updates_attributes(hass)
    assert attrs["deferred"] == ["10"]
    assert attrs["deferred_until"] == dt_util.as_local(start).isoformat()
    section = options[SECTION_ACTIVE_HOURS]
    assert attrs["active_hours"] == {"start": section[CONF_ACTIVE_START][:5], "end": section[CONF_ACTIVE_END][:5]}
    # The queue does not move on with it, and the next check does not add it again
    await _settle(hass, QUEUE_DELAY + 1)
    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert _installs(publishes) == []
    assert jobs.queue == ["10"]

    # The active hours begin
    async_fire_time_changed(hass, start)
    await _settle(hass)
    assert jobs.hours.is_open()
    assert _installs(publishes) == ["install:10,"]
    assert jobs.job == ["10"]
    assert jobs.queue == []
    attrs = _updates_attributes(hass)
    assert attrs["deferred"] == []
    assert attrs["deferred_until"] is None


async def test_work_by_hand_does_not_wait_for_the_active_hours(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """Outside the active hours a game asked for by hand goes at once and leaves the waiting
    automatic work in the queue; asked for by hand, a waiting game goes too."""
    _store(hass_storage, queue=["10"], auto_added=["10"])
    _pc_on(hass)
    entry = await setup_entry(_active_hours(open_now=False))
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")

    await hass.services.async_call(DOMAIN, "install", {"appid": "20"}, blocking=True)
    await _settle(hass)
    assert _installs(publishes) == ["install:20,"]
    assert jobs.queue == ["10"]
    assert jobs.deferred == ["10"]
    hass.states.async_set(STATUS, "started", {"appids": ["20"], "count": 1})
    await _settle(hass, 3)
    hass.states.async_set(STATUS, "done", _done(ok=["20"]))
    await _settle(hass)
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:20,"]

    await hass.services.async_call(DOMAIN, "install", {"appid": "10"}, blocking=True)
    await _settle(hass)
    assert _installs(publishes) == ["install:20,", "install:10,"]
    assert jobs.queue == []
    assert jobs.deferred == []


async def test_restart_outside_the_active_hours_waits_for_their_start(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """A new purchase that waited for the active hours before a restart of Home Assistant waits on,
    and goes out when they begin."""
    _store(hass_storage, queue=["30"], auto_added=["30"], auto_added_new=["30"])
    _pc_on(hass)
    entry = await setup_entry(_active_hours(open_now=False))
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == []
    assert jobs.deferred == ["30"]
    start = jobs.hours.next_start
    assert _updates_attributes(hass)["deferred_until"] == dt_util.as_local(start).isoformat()

    async_fire_time_changed(hass, start)
    await _settle(hass)
    assert _installs(publishes) == ["install:30,"]
    assert jobs.deferred == []


async def test_restart_inside_the_active_hours_starts_what_waited(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """Home Assistant starts inside the active hours with automatic work that waited for them: it
    goes out at once. A game that needs nothing any more (installed and up to date, or an update of
    a game uninstalled meanwhile) is dropped; a new purchase that is not installed yet goes."""
    _store(hass_storage, queue=["30", "10", "20"], auto_added=["30", "10", "20"], auto_added_new=["30"])
    _pc_on(hass)
    hass.states.async_set(LIBRARY, "1", {"games": [{"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}]})
    publishes = async_mock_service(hass, "mqtt", "publish")
    entry = await setup_entry(_active_hours(open_now=True))
    await _settle(hass)
    assert _installs(publishes) == ["install:30,"]
    assert entry.runtime_data.jobs.queue == []


async def test_end_of_the_active_hours_keeps_the_running_job(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """A job that runs when the active hours end goes on; automatic work queued behind it waits
    for the next start."""
    _store(hass_storage, job=["10"])
    _pc_on(hass)
    downloading = {"appid": "10", "position": 1, "count": 1, "percent": 20.0, "phase": "download"}
    hass.states.async_set(STATUS, "downloading", downloading)
    entry = await setup_entry(_active_hours(open_now=True))
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    assert jobs.job_running()
    assert (await jobs.async_install(["20"], automatic=True))["queued"]
    assert jobs.deferred == []  # behind the job, not waiting for the window

    async_fire_time_changed(hass, next_at(jobs.hours.end, dt_util.utcnow(), dt_util.get_default_time_zone()))
    await _settle(hass)
    assert not jobs.hours.is_open()
    assert jobs.job_running()
    assert jobs.deferred == ["20"]

    hass.states.async_set(STATUS, "downloading", {**downloading, "percent": 60.0})
    hass.states.async_set(STATUS, "done", _done(ok=["10"]))
    await _settle(hass)
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == []
    assert jobs.job == []
    assert jobs.queue == ["20"]

    async_fire_time_changed(hass, jobs.hours.next_start)
    await _settle(hass)
    assert _installs(publishes) == ["install:20,"]


async def test_held_work_stays_queued_when_the_wake_fails(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """The PC cannot be woken when the active hours begin (no wake entity): what waited stays in
    the queue, not in the job list, so a PC switched on after their end does not start it; it goes
    at the next start."""
    _store(hass_storage, queue=["30"], auto_added=["30"], auto_added_new=["30"])
    entry = await setup_entry(_active_hours(open_now=False))
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    start = jobs.hours.next_start

    async_fire_time_changed(hass, start)
    await _settle(hass)
    assert jobs.hours.is_open()
    assert jobs.job == []
    assert jobs.queue == ["30"]

    async_fire_time_changed(hass, next_at(jobs.hours.end, start, dt_util.get_default_time_zone()))
    await _settle(hass)
    assert not jobs.hours.is_open()
    assert jobs.deferred == ["30"]
    _pc_on(hass)
    await _settle(hass)
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == []
    assert jobs.queue == ["30"]

    async_fire_time_changed(hass, jobs.hours.next_start)
    await _settle(hass)
    assert _installs(publishes) == ["install:30,"]
    assert jobs.deferred == []


async def test_active_hours_follow_the_clock(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A timer that fires when the clock is already further on (late, or the clock corrected after
    the start) sets the state the clock shows, not the opposite of the last one."""
    entry = await setup_entry(_active_hours(open_now=True))
    hours = entry.runtime_data.jobs.hours
    tz = dt_util.get_default_time_zone()
    end = next_at(hours.end, dt_util.utcnow(), tz)
    later = end + timedelta(hours=22)  # inside the window of the next day
    with patch("homeassistant.util.dt.utcnow", return_value=later):
        async_fire_time_changed(hass, end)
    await _settle(hass)
    assert hours.is_open()
    assert hours.next_start is None
    # The next timer is the end after that clock time
    async_fire_time_changed(hass, next_at(hours.end, later, tz))
    await _settle(hass)
    assert not hours.is_open()


async def test_update_push_says_when_automatic_updates_start(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    setup_entry: Setup,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Outside the active hours the update push tells when the automatic updates start."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _store(hass_storage, auto_update=["10"])
    _pc_on(hass)
    alpha = {"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}
    beta = {"appid": "20", "name": "Beta", "buildid": "1", "size_mb": 100}
    hass.states.async_set(LIBRARY, "2", {"games": [alpha, beta]})
    _build_answer(aioclient_mock, "10", "2")
    _build_answer(aioclient_mock, "20", "2")
    entry = await setup_entry({**NOTIFY, **_active_hours(open_now=False)})
    async_mock_service(hass, "mqtt", "publish")

    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    updates = _update_pushes(pushes)
    assert len(updates) == 1
    start = dt_util.as_local(entry.runtime_data.jobs.hours.next_start).strftime("%H:%M")
    assert updates[0].data["message"] == f"Beta · 1 runs automatically at {start}"


async def test_refresh_reads_the_library_first(
    hass: HomeAssistant, setup_entry: Setup, aioclient_mock: AiohttpClientMocker
) -> None:
    """Sync now (refresh): asks the agent for its library, waits for it, then checks with it."""
    _pc_on(hass)
    alpha = {"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}
    hass.states.async_set(LIBRARY, "1", {"games": [alpha]})
    _build_answer(aioclient_mock, "10", "2")
    await setup_entry()
    publishes = async_mock_service(hass, "mqtt", "publish")

    # In the background, so the waits of the test do not wait for it
    task = hass.async_create_background_task(
        hass.services.async_call(DOMAIN, "refresh", {}, blocking=True, return_response=True), "refresh"
    )
    await _settle(hass)
    assert [c.data["payload"] for c in publishes] == ["library"]
    assert not task.done()

    # The agent answers: Alpha is already at the new build (Steam updated it by itself)
    hass.states.async_set(LIBRARY, "1", {"games": [{**alpha, "buildid": "2"}]})
    response = await task
    assert response["library_refreshed"] is True
    assert response["games"] == []
    assert response["skipped"] is False
    assert response["new_purchases"] == []
    assert response["owned_skipped"] is True  # no Steam key in the settings


async def test_refresh_with_an_unchanged_library(
    hass: HomeAssistant, setup_entry: Setup, aioclient_mock: AiohttpClientMocker, mqtt_mock: Any
) -> None:
    """The agent sends the same library again: the sensor does not change, the fresh message on its
    topic ends the wait (the retained copy the subscription gets first does not)."""
    _pc_on(hass)
    hass.states.async_set(LIBRARY, "1", {"games": [{"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}]})
    _build_answer(aioclient_mock, "10", "1")
    await setup_entry()
    await _settle(hass, 11)  # no depot list on the broker: the update check stops waiting for it

    task = hass.async_create_background_task(
        hass.services.async_call(DOMAIN, "refresh", {}, blocking=True, return_response=True), "refresh"
    )
    await _settle(hass)
    async_fire_mqtt_message(hass, "steam/library", "{}", retain=True)
    await _settle(hass)
    assert not task.done()
    async_fire_mqtt_message(hass, "steam/library", "{}")
    response = await task
    assert response["library_refreshed"] is True
    assert response["games"] == []



def _job_pushes(calls: list[ServiceCall]) -> list[ServiceCall]:
    """Pushes about a job (start, progress, result), not the clearing of one."""
    return [
        c
        for c in calls
        if c.data.get("message") != "clear_notification" and c.data.get("data", {}).get("tag") == TAG_PROGRESS
    ]


async def test_kinds_not_chosen_are_not_pushed(hass: HomeAssistant, setup_entry: Setup) -> None:
    """Without "progress" only the result comes; without "result" the progress bar is cleared at the end."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _pc_on(hass)
    kinds = [k for k in PUSH_KINDS if k != "progress"]
    entry = await setup_entry({SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: PHONE, CONF_PUSH_KINDS: kinds}})
    async_mock_service(hass, "mqtt", "publish")

    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    hass.states.async_set(STATUS, "downloading", {"appid": "10", "position": 1, "count": 1, "percent": 40.0})
    await _settle(hass)
    assert _job_pushes(pushes) == []
    hass.states.async_set(STATUS, "done", _done(ok=["10"]))
    await _settle(hass)
    assert len(_job_pushes(pushes)) == 1

    # Progress yes, result no
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: PHONE, CONF_PUSH_KINDS: ["progress"]}}
    )
    await _settle(hass)
    pushes.clear()
    hass.states.async_set(STATUS, "started", {"appids": ["20"], "count": 1})
    await _settle(hass)
    assert len(_job_pushes(pushes)) == 1
    hass.states.async_set(STATUS, "done", _done(ok=["20"]))
    await _settle(hass)
    assert len(_job_pushes(pushes)) == 1
    assert pushes[-1].data == {"message": "clear_notification", "data": {"tag": TAG_PROGRESS}}


async def test_no_pushes(hass: HomeAssistant, setup_entry: Setup, caplog: pytest.LogCaptureFixture) -> None:
    """No service chosen: nothing is sent, and nothing is logged as missing."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _pc_on(hass)
    await setup_entry({SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: ""}})
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    hass.states.async_set(STATUS, "done", _done(failed=["10"]))
    await _settle(hass)
    assert pushes == []
    assert "Notify service" not in caplog.text


async def test_quiet_automatic_updates(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    setup_entry: Setup,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """With "automatic updates quiet" a job of automatic updates only pushes when its last try
    failed; the update push leaves them out; a game asked for by hand is not quiet."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _store(hass_storage, auto_update=["10"])
    _pc_on(hass)
    alpha = {"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}
    beta = {"appid": "20", "name": "Beta", "buildid": "1", "size_mb": 100}
    hass.states.async_set(LIBRARY, "2", {"games": [alpha, beta]})
    _build_answer(aioclient_mock, "10", "2")
    _build_answer(aioclient_mock, "20", "2")
    entry = await setup_entry({SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: PHONE, CONF_QUIET_AUTO_UPDATES: True}})
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")

    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert _installs(publishes) == ["install:10,"]
    assert jobs.auto_quiet == {"10"}
    updates = _update_pushes(pushes)
    assert [u.data["message"] for u in updates] == ["Beta"]  # without "1 runs automatically"

    # First try fails: quiet, the second try follows with the next check
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    hass.states.async_set(STATUS, "downloading", {"appid": "10", "position": 1, "count": 1, "percent": 40.0})
    await _settle(hass)
    hass.states.async_set(STATUS, "done_with_errors", _done(failed=["10"]))
    await _settle(hass, 3)
    assert _job_pushes(pushes) == []
    assert jobs.auto_quiet == set()

    # Second try fails too: now the result is pushed
    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert _installs(publishes) == ["install:10,", "install:10,"]
    assert jobs.auto_quiet == {"10"}
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass)
    hass.states.async_set(STATUS, "done_with_errors", _done(failed=["10"]))
    await _settle(hass, 3)
    results = [c for c in _job_pushes(pushes) if c not in updates]
    assert len(results) == 1
    assert "Alpha" in results[0].data["message"]

    # Asked for by hand: no longer quiet
    await _settle(hass, QUEUE_DELAY + 1)
    await hass.services.async_call(DOMAIN, "install", {"appid": "10"}, blocking=True)
    await _settle(hass)
    assert jobs.auto_quiet == set()
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass)
    assert len([c for c in _job_pushes(pushes) if c not in updates]) == 2


async def test_muted_games(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    setup_entry: Setup,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Muted games: not in the update push (nor counted in it as automatic), not counted by the
    updates sensor, their automatic job is quiet without the option, Install all leaves them out;
    the service switches muting."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _store(hass_storage, auto_update=["10"], muted=["10", "30"])
    _pc_on(hass)
    games = [
        {"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100},
        {"appid": "20", "name": "Beta", "buildid": "1", "size_mb": 100},
        {"appid": "30", "name": "Gamma", "buildid": "1", "size_mb": 100},
    ]
    hass.states.async_set(LIBRARY, "3", {"games": games})
    for appid in ("10", "20", "30"):
        _build_answer(aioclient_mock, appid, "2")
    entry = await setup_entry(NOTIFY)
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")

    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert _installs(publishes) == ["install:10,"]  # the automatic update still runs
    updates = _update_pushes(pushes)
    assert [u.data["message"] for u in updates] == ["Beta"]
    sensor = hass.states.get(UPDATES_SENSOR_ENTITY_ID)
    assert sensor.state == "1"
    assert [g["appid"] for g in sensor.attributes["games"]] == ["10", "20", "30"]
    assert sensor.attributes["muted"] == ["10", "30"]

    # The automatic job of a muted game is quiet (the option is off)
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass)
    hass.states.async_set(STATUS, "done", _done(ok=["10"]))
    await _settle(hass, 3)
    assert _job_pushes(pushes) == []

    # Install all: Beta, not the muted Gamma
    await _settle(hass, QUEUE_DELAY + 1)
    hass.bus.async_fire("mobile_app_notification_action", {"action": ACTION_INSTALL_ALL})
    await _settle(hass)
    assert _job_pushes(pushes)[0].data["message"] == "1 update queued."
    sent = set(jobs.job) | set(jobs.queue)
    assert "20" in sent and "30" not in sent

    response = await hass.services.async_call(
        DOMAIN, "set_muted", {"appid": "30"}, blocking=True, return_response=True
    )
    assert response == {"appid": "30", "muted": False}
    await _settle(hass)
    assert hass.states.get(UPDATES_SENSOR_ENTITY_ID).attributes["muted"] == ["10"]
    assert jobs.muted == ["10"]


async def test_quiet_automatic_update_without_space_is_pushed(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    setup_entry: Setup,
) -> None:
    """Too little space is pushed at once (a second try would not find more): the push "not enough
    space", without a result push beside it."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _store(hass_storage, job=["10"], auto_update=["10"], auto_quiet=["10"])
    _pc_on(hass)
    hass.states.async_set(LIBRARY, "1", {"games": [{"appid": "10", "name": "Alpha", "buildid": "1", "size_mb": 100}]})
    await setup_entry({SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: PHONE, CONF_QUIET_AUTO_UPDATES: True}})
    async_mock_service(hass, "mqtt", "publish")

    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass)
    assert _job_pushes(pushes) == []
    no_space = [{"appid": "10", "needed_gb": 50, "free_gb": 10, "drive": "F:"}]
    hass.states.async_set(STATUS, "done_with_errors", _done(no_space=no_space))
    await _settle(hass)
    assert _job_pushes(pushes) == []
    assert [c.data.get("data", {}).get("tag") for c in pushes if c.data.get("message") != "clear_notification"] == [TAG_NO_SPACE]


async def test_target_drive(hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup) -> None:
    """A game asked for with a drive is sent as <appid>@<letter>, also when the job goes out again.
    Without a drive a stored target stays; a new drive wins until the PC accepted the game. The
    target goes once the game left the job, the queue and after_game."""
    _pc_on(hass)
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")

    await hass.services.async_call(DOMAIN, "install", {"appid": "10", "drive": "E:"}, blocking=True)
    await _settle(hass)
    assert _installs(publishes) == ["install:10@e,"]
    assert hass_storage[STORAGE_KEY]["data"]["targets"] == {"10": "e"}
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass, 3)

    # Queued behind the running job with a drive of its own
    assert await jobs.async_install(["20"], drive="c") == {"queued": True, "sent": False}
    assert jobs.targets == {"10": "e", "20": "c"}
    # The PC has 10 already: a new drive does not change it
    assert (await jobs.async_install(["10"], drive="f"))["reason"] == "already_queued"
    # 20 is only queued: the new drive wins
    assert await jobs.async_install(["20"], drive="f") == {"queued": True, "sent": False}
    # Without a drive (a button, a push, automatic work) nothing changes
    assert (await jobs.async_install(["20"]))["reason"] == "already_queued"
    assert (await jobs.async_install(["20"], drive="f"))["reason"] == "already_queued"
    assert jobs.targets == {"10": "e", "20": "f"}
    assert hass_storage[STORAGE_KEY]["data"]["targets"] == {"10": "e", "20": "f"}

    # Interrupted: the job goes out again with the queue, each game with its drive
    hass.states.async_set(STATUS, "aborted", {"reason": "The job was interrupted"})
    await _settle(hass)
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10@e,", "install:10@e,20@f,"]

    # Done: no game is open any more, the targets go
    hass.states.async_set(STATUS, "started", {"appids": ["10", "20"], "count": 2})
    await _settle(hass, 3)
    hass.states.async_set(STATUS, "done", _done(ok=["10", "20"]))
    await _settle(hass, 3)
    assert jobs.job == []
    assert jobs.targets == {}
    assert hass_storage[STORAGE_KEY]["data"]["targets"] == {}


async def test_target_drive_while_the_pc_waits(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A game in the job list that the PC has not accepted (the agent is offline, the job waits for
    it) takes a new drive, and the job goes out with it once the agent is back."""
    _pc_on(hass)
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    hass.states.async_set(AGENT_ONLINE, "off")
    hass.states.async_set(NETWORK, "unavailable")
    await _settle(hass)
    # No wake entity: the job waits for the agent
    assert (await jobs.async_install(["10"], drive="e"))["sent"] is False
    assert (await jobs.async_install(["10"], drive="d"))["sent"] is False
    assert jobs.job == ["10"]
    assert jobs.targets == {"10": "d"}
    assert _installs(publishes) == []

    _pc_on(hass)
    await _settle(hass)
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10@d,"]


async def test_target_drive_after_a_restart(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """Stored targets come back for the games still open; others and invalid ones are dropped."""
    _store(hass_storage, job=["10"], queue=["20", "30"], targets={"10": "e", "20": "E:", "40": "d"})
    _pc_on(hass)
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    assert jobs.targets == {"10": "e"}
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10@e,20,30,"]


async def test_target_drive_waits_for_the_game(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A game not started because a game runs keeps its drive while it waits for the game to close,
    and goes out with it then."""
    _pc_on(hass)
    hass.states.async_set(GAME_RUNNING, "on", {"appid": "99"})
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")

    await hass.services.async_call(DOMAIN, "install", {"appid": "10", "drive": "e"}, blocking=True)
    await _settle(hass)
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass, 3)
    hass.states.async_set(STATUS, "done", _done(game_running=["10"]))
    await _settle(hass, 3)
    assert jobs.job == []
    assert jobs.waiting == ["10"]
    assert jobs.targets == {"10": "e"}

    # The game is closed: what waited goes out, with its drive
    hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
    await _settle(hass)
    assert _installs(publishes) == ["install:10@e,", "install:10@e,"]
    assert jobs.waiting == []
    assert jobs.job == ["10"]
    assert jobs.targets == {"10": "e"}
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass, 3)
    hass.states.async_set(STATUS, "done", _done(ok=["10"]))
    await _settle(hass, 3)
    assert jobs.targets == {}


async def test_target_drive_when_the_game_closed_meanwhile(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A game not started because a game ran, with that game closed before the end arrives: it
    leaves the job for the queue in one step, keeps its drive and goes out with the queue."""
    _pc_on(hass)
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")

    await hass.services.async_call(DOMAIN, "install", {"appid": "10", "drive": "e"}, blocking=True)
    await _settle(hass)
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass, 3)
    hass.states.async_set(STATUS, "done", _done(game_running=["10"]))
    await _settle(hass, 3)
    assert jobs.job == []
    assert jobs.queue == ["10"]
    assert jobs.targets == {"10": "e"}
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10@e,", "install:10@e,"]


async def _game_started_during_the_job(hass: HomeAssistant, setup_entry: Setup) -> tuple[MockConfigEntry, list[ServiceCall]]:
    """A game started during the job ends it (Steam holds the download) before the agent's next round
    shows the game: the end names the game running (game_appid), the agent shows none yet."""
    _pc_on(hass)
    entry = await setup_entry()
    publishes = async_mock_service(hass, "mqtt", "publish")
    await hass.services.async_call(DOMAIN, "install", {"appid": "10"}, blocking=True)
    await _settle(hass)
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass, 3)
    hass.states.async_set(STATUS, "done_with_errors", {**_done(game_running=["10"]), "game_appid": "99"})
    await _settle(hass, 3)
    assert entry.runtime_data.jobs.waiting == ["10"]
    assert entry.runtime_data.jobs.queue == []
    return entry, publishes


async def test_game_started_during_the_job_waits_for_it(hass: HomeAssistant, setup_entry: Setup) -> None:
    """The game waits in after_game, not sent again at once; the agent shows the game with its next
    round, and the game goes out once that game is closed."""
    entry, publishes = await _game_started_during_the_job(hass, setup_entry)
    hass.states.async_set(GAME_RUNNING, "on", {"appid": "99"})
    await _settle(hass, QUEUE_DELAY + 1)
    await _settle(hass, GAME_SEEN_WAIT + 1)
    assert _installs(publishes) == ["install:10,"]
    assert entry.runtime_data.jobs.waiting == ["10"]

    hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
    await _settle(hass)
    assert _installs(publishes) == ["install:10,", "install:10,"]
    assert entry.runtime_data.jobs.waiting == []


async def test_game_closed_before_the_agent_showed_it(hass: HomeAssistant, setup_entry: Setup) -> None:
    """The game was closed before the agent's next round: the agent never shows it, so what waits
    goes out once that round is over (Steam busy until then), not left in after_game."""
    entry, publishes = await _game_started_during_the_job(hass, setup_entry)
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10,"]
    assert hass.states.get(ENTITY_STEAM_BUSY).state == "on"
    await _settle(hass, GAME_SEEN_WAIT + 1)
    assert _installs(publishes) == ["install:10,", "install:10,"]
    assert entry.runtime_data.jobs.waiting == []


async def test_game_waiting_during_a_drain_goes_too(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A game that joins after_game while the waiting games are being sent (and whose game is closed
    again meanwhile) is installed once that run ends, not only at the next game or agent change."""
    _pc_on(hass)
    hass.states.async_set(GAME_RUNNING, "on", {"appid": "99"})
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    await jobs.async_wait_for_game(["10"])

    release = asyncio.Event()
    install = jobs.async_install

    async def held_install(appids: list[str], *args: Any, **kwargs: Any) -> dict[str, Any]:
        await release.wait()
        return await install(appids, *args, **kwargs)

    with patch.object(jobs, "async_install", held_install):
        hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
        await _settle(hass)
        # While 10 is on its way a game runs, 20 is to wait for it, and the game is closed again
        hass.states.async_set(GAME_RUNNING, "on", {"appid": "99"})
        await jobs.async_wait_for_game(["20"])
        hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
        await _settle(hass)
        assert _installs(publishes) == []

        release.set()
        await _settle(hass)
        assert _installs(publishes) == ["install:10,"]
        assert jobs.waiting == ["20"]
        hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
        await _settle(hass, 3)
        # The first run ended: 20 follows the running job
        assert jobs.waiting == []
        assert jobs.queue == ["20"]


def _job_running_at_start(hass: HomeAssistant) -> None:
    """The PC is on and a job of game 10 downloads."""
    _pc_on(hass)
    hass.states.async_set(
        STATUS, "downloading", {"appid": "10", "position": 1, "count": 1, "percent": 20.0, "phase": "download"}
    )


async def test_target_drive_kept_through_a_job_end_meanwhile(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """A job end seen while an install writes the target drive to the store (before the game joined
    a list) does not take the target away."""
    _store(hass_storage, job=["10"])
    _job_running_at_start(hass)
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    assert jobs.job_running()
    save = jobs._save  # noqa: SLF001
    ended = False

    async def save_while_a_job_ends() -> None:
        nonlocal ended
        if not ended:
            ended = True
            hass.states.async_set(STATUS, "done", _done(ok=["10"]))
            for _ in range(10):
                await asyncio.sleep(0)  # the end is handled: it does not wait for the install
            assert jobs.job == []
        await save()

    with patch.object(jobs, "_save", save_while_a_job_ends):
        await hass.services.async_call(DOMAIN, "install", {"appid": "20", "drive": "E:"}, blocking=True)
        # The install runs as a background task: wait until the game joined a list (sent or queued
        # depends on when the end was seen)
        for _ in range(200):
            if "20" in jobs.job + jobs.queue:
                break
            await asyncio.sleep(0)
    assert ended
    assert "20" in jobs.job + jobs.queue
    assert jobs.targets == {"20": "e"}


async def test_uninstall_takes_the_game_out_of_open_installs(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """An uninstall takes the game out of the queue, and out of the job list while no job runs: the
    PC must not install it again. A job that runs keeps it (the PC refuses the uninstall then)."""
    _store(hass_storage, job=["10", "30"], queue=["20"])
    _job_running_at_start(hass)
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    publishes = async_mock_service(hass, "mqtt", "publish")
    assert jobs.job_running()

    assert await jobs.async_uninstall("20") == {"sent": True}
    assert await jobs.async_uninstall("10") == {"sent": True}
    assert jobs.queue == []
    assert jobs.job == ["10", "30"]

    hass.states.async_set(STATUS, "aborted", {"reason": "The job was interrupted"})
    await _settle(hass)
    assert await jobs.async_uninstall("30") == {"sent": True}
    assert jobs.job == ["10"]
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:10,"]


async def test_only_ascii_app_ids(hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup) -> None:
    """Only ASCII digits are app IDs (the agent refuses a command with others): other digits are
    dropped from the actions and from the lists in the store."""
    superscript, arabic = "\u00b2", "\u0663"
    assert parse_appids(f"730, {superscript},{arabic}, 10") == ["730", "10"]
    _store(hass_storage, job=[arabic, "10"], queue=[superscript])
    entry = await setup_entry()
    jobs = entry.runtime_data.jobs
    assert jobs.job == ["10"]
    assert jobs.queue == []
    assert await jobs.async_uninstall(superscript) == {"sent": False, "reason": "invalid_appid"}
    assert await jobs.async_launch(arabic) == {"sent": False, "reason": "invalid_appid"}


def test_install_command() -> None:
    assert protocol.install_command(["440", "570"], {}) == "install:440,570,"
    assert protocol.install_command(["440", "570"], {"440": "e", "999": "d"}) == "install:440@e,570,"
    # Only a single lower-case letter is ever sent
    assert protocol.install_command(["440"], {"440": "E:"}) == "install:440,"
    # The job ID goes first
    assert protocol.install_command(["440"], {"440": "e"}, "0a1b2c3d") == "install:0a1b2c3d:440@e,"
    assert protocol.job_id({"job": "0a1b2c3d"}) == "0a1b2c3d"
    assert protocol.job_id({}) == ""
