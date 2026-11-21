"""The set-up integration: its entities on the device, picking a game, the buttons, auto update of
the picked game, the start time of the current game over a restart, the hint under Repairs while the
PC side has not reported. Nothing here waits for the PC (no job is started)."""

from __future__ import annotations

from datetime import timedelta
import re

import pytest
import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.steam_engine import INSTALL_SCHEMA, const
from custom_components.steam_engine.steam_api import OWNED_URL
from custom_components.steam_engine.steam_events import EVENT_PURCHASED
from custom_components.steam_engine.const import (
    DOMAIN,
    ENTITY_AUTO_INSTALL_PURCHASES,
    ENTITY_AUTO_UPDATE_PICKED,
    ENTITY_CURRENT_GAME,
    ENTITY_GAME_TO_INSTALL,
    ENTITY_INSTALLED_GAME,
    ENTITY_INSTALL_GAME,
    ENTITY_LAUNCH_GAME,
    ENTITY_UNINSTALL_GAME,
    NOT_INSTALLED_SENSOR_ENTITY_ID,
    merged_options,
)

# Default entity IDs of the agent (used while the agent has not reported)
LIBRARY = "sensor.steam_pc_library"
GAME_RUNNING = "binary_sensor.steam_pc_game_running"
GAMES = [
    {"appid": "10", "name": "Beta"},
    {"appid": "20", "name": "Alpha"},
    {"appid": "30", "name": "Alpha"},
    {"appid": "40", "name": "Gamma", "unavailable": True},  # drive not there
]


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.states.async_set(LIBRARY, str(len(GAMES)), {"games": GAMES})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = await _setup(hass)
    yield entry
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _pick(hass: HomeAssistant, entity_id: str, option: str) -> None:
    await hass.services.async_call(
        "select", "select_option", {ATTR_ENTITY_ID: entity_id, "option": option}, blocking=True
    )


async def test_entities_on_the_device(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    # Every entity ID in const.py exists, belongs to this entry and sits on the one device
    entity_ids = [
        value
        for name, value in vars(const).items()
        if isinstance(value, str) and (name.startswith("ENTITY_") or name.endswith("_ENTITY_ID"))
    ]
    assert len(entity_ids) == 20
    registry = er.async_get(hass)
    devices = set()
    for entity_id in entity_ids:
        assert hass.states.get(entity_id) is not None, entity_id
        entity = registry.async_get(entity_id)
        assert entity is not None and entity.config_entry_id == entry.entry_id, entity_id
        devices.add(entity.device_id)
    assert len(devices) == 1 and None not in devices


async def test_installed_game_options(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    state = hass.states.get(ENTITY_INSTALLED_GAME)
    # Sorted by name, a shared name gets the app ID, a game on a missing drive is left out
    assert state.attributes["options"] == ["Alpha (20)", "Alpha (30)", "Beta"]
    assert state.state == "unknown"


async def test_buttons_need_a_pick(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    for entity_id in (ENTITY_INSTALL_GAME, ENTITY_UNINSTALL_GAME, ENTITY_LAUNCH_GAME):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: entity_id}, blocking=True)


async def test_launch_refused_while_a_game_runs(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    hass.states.async_set(GAME_RUNNING, "on", {"appid": "20"})
    await _pick(hass, ENTITY_INSTALLED_GAME, "Beta")
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: ENTITY_LAUNCH_GAME}, blocking=True)
    assert err.value.translation_key == "game_running"
    # The pick stays, so it can be started once the other game ends
    assert hass.states.get(ENTITY_INSTALLED_GAME).state == "Beta"
    response = await hass.services.async_call(
        DOMAIN, "launch", {"appid": "10"}, blocking=True, return_response=True
    )
    assert response == {"sent": False, "reason": "game_running"}


async def test_auto_update_of_the_picked_game(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    jobs = entry.runtime_data.jobs
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_AUTO_UPDATE_PICKED}, blocking=True)

    await _pick(hass, ENTITY_INSTALLED_GAME, "Alpha (30)")
    assert hass.states.get(ENTITY_AUTO_UPDATE_PICKED).state == "off"
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_AUTO_UPDATE_PICKED}, blocking=True)
    assert jobs.auto_update == ["30"]
    assert hass.states.get(ENTITY_AUTO_UPDATE_PICKED).state == "on"

    # Another pick shows that game's setting
    await _pick(hass, ENTITY_INSTALLED_GAME, "Beta")
    assert hass.states.get(ENTITY_AUTO_UPDATE_PICKED).state == "off"

    await _pick(hass, ENTITY_INSTALLED_GAME, "Alpha (30)")
    await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: ENTITY_AUTO_UPDATE_PICKED}, blocking=True)
    assert jobs.auto_update == []


async def test_pick_ends_after_ten_minutes(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _pick(hass, ENTITY_INSTALLED_GAME, "Beta")
    assert hass.states.get(ENTITY_INSTALLED_GAME).state == "Beta"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=9))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_INSTALLED_GAME).state == "Beta"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_INSTALLED_GAME).state == "unknown"


async def test_pick_ends_when_the_game_leaves_the_list(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _pick(hass, ENTITY_INSTALLED_GAME, "Beta")
    hass.states.async_set(LIBRARY, "1", {"games": [{"appid": "20", "name": "Alpha"}]})
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_INSTALLED_GAME)
    assert state.state == "unknown"
    assert state.attributes["options"] == ["Alpha"]


async def test_hint_while_the_pc_side_has_not_reported(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    # No agent entity in the entity registry: the PC side has not reported yet
    assert ir.async_get(hass).async_get_issue(DOMAIN, "agent_missing") is not None


def _started(hass: HomeAssistant) -> object:
    return dt_util.parse_datetime(hass.states.get(ENTITY_CURRENT_GAME).attributes["started"])


async def test_current_game_keeps_its_start_over_a_restart(hass: HomeAssistant) -> None:
    started = dt_util.utcnow() - timedelta(hours=2)
    mock_restore_cache(hass, [State(ENTITY_CURRENT_GAME, "Beta", {"appid": "10", "started": started.isoformat()})])
    entry = await _setup(hass)
    # The agent reports after the sensor is back: the same game still runs
    hass.states.async_set(GAME_RUNNING, "on", {"appid": "10"})
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_CURRENT_GAME).state == "Beta"
    assert _started(hass) == started
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_current_game_forgets_the_start_once_the_game_ended(hass: HomeAssistant) -> None:
    started = dt_util.utcnow() - timedelta(days=3)
    mock_restore_cache(hass, [State(ENTITY_CURRENT_GAME, "Beta", {"appid": "10", "started": started.isoformat()})])
    entry = await _setup(hass)
    hass.states.async_set(GAME_RUNNING, "off")
    await hass.async_block_till_done()
    # Days later the same game starts again: a new start time, not the one from before the restart
    hass.states.async_set(GAME_RUNNING, "on", {"appid": "10"})
    await hass.async_block_till_done()
    assert _started(hass) > dt_util.utcnow() - timedelta(minutes=1)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_services_without_a_loaded_entry(hass: HomeAssistant) -> None:
    """The services stay registered while the entry is not loaded (a reload) and say so."""
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "check_updates")
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True)
    assert err.value.translation_key == "not_loaded"


async def test_agent_down_hint_after_the_wait(hass: HomeAssistant) -> None:
    """PC reachable, agent off: the hint comes once both have been so for AGENT_DOWN_AFTER (a timer,
    no polling) and goes as soon as the agent reports."""
    reachable = "binary_sensor.pc_ping"
    agent_online = "binary_sensor.steam_pc_agent_online"
    hass.states.async_set(reachable, "on")
    hass.states.async_set(agent_online, "off")
    hass.states.async_set(LIBRARY, str(len(GAMES)), {"games": GAMES})
    options = merged_options({const.SECTION_PC: {const.CONF_REACHABLE_ENTITY: reachable}})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, "agent_not_running") is None

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=29))
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, "agent_not_running") is None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=31))
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, "agent_not_running") is not None

    hass.states.async_set(agent_online, "on")
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, "agent_not_running") is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_every_key_is_an_entity(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Each KEY_* of const.py is the unique ID key of one entity (own_entities.py relies on them)."""
    keys = [value for name, value in vars(const).items() if name.startswith("KEY_")]
    assert len(keys) == 20
    registry = er.async_get(hass)
    unique_ids = {e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert {const.unique_id(entry.entry_id, key) for key in keys} == unique_ids


async def test_actions_pick_the_pc(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The field "pc": empty or "Steam PC" is the PC without a name; an unknown PC says which there are."""
    await hass.services.async_call(DOMAIN, "set_auto_update", {"appid": "10", "enabled": True}, blocking=True)
    await hass.services.async_call(
        DOMAIN, "set_auto_update", {"appid": "10", "enabled": False, "pc": "Steam PC"}, blocking=True
    )
    await hass.services.async_call(DOMAIN, "set_auto_update", {"appid": "10", "enabled": True, "pc": ""}, blocking=True)
    assert entry.runtime_data.jobs.auto_update == ["10"]
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "set_auto_update", {"appid": "10", "enabled": False, "pc": "Kinder-PC"}, blocking=True
        )
    assert err.value.translation_key == "unknown_pc"
    assert err.value.translation_placeholders == {"pc": "Kinder-PC", "pcs": "Steam PC"}
    assert entry.runtime_data.jobs.auto_update == ["10"]


async def test_two_pcs(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A second PC gets entities and a device of its own; the first keeps all of its names, and
    actions without a PC still go to it."""
    kids = MockConfigEntry(domain=DOMAIN, data={"pc_name": "Kinder-PC"}, options=merged_options({}), unique_id="kinderpc")
    kids.add_to_hass(hass)
    assert await hass.config_entries.async_setup(kids.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("switch.steam_engine_pc") is not None
    assert hass.states.get("switch.steam_engine_kinderpc_pc") is not None
    assert hass.states.get("sensor.steam_engine_kinderpc_current_game") is not None
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    first = registry.async_get("switch.steam_engine_pc")
    second = registry.async_get("switch.steam_engine_kinderpc_pc")
    assert devices.async_get(first.device_id).name == "Steam Engine"
    assert devices.async_get(second.device_id).name == "Steam Engine Kinder-PC"
    assert kids.runtime_data.agent.library == "sensor.steam_pc_kinderpc_library"
    assert entry.runtime_data.agent.library == LIBRARY

    await hass.services.async_call(DOMAIN, "set_auto_update", {"appid": "10", "enabled": True}, blocking=True)
    assert entry.runtime_data.jobs.auto_update == ["10"]
    assert kids.runtime_data.jobs.auto_update == []
    await hass.services.async_call(
        DOMAIN, "set_auto_update", {"appid": "20", "enabled": True, "pc": "Kinder-PC"}, blocking=True
    )
    assert kids.runtime_data.jobs.auto_update == ["20"]
    assert entry.runtime_data.jobs.auto_update == ["10"]

    # A target drive belongs to the PC the game goes to (no agent online: the job waits for it)
    await hass.services.async_call(
        DOMAIN, "install", {"appid": "30", "drive": "E:", "pc": "Kinder-PC"}, blocking=True, return_response=True
    )
    assert kids.runtime_data.jobs.job == ["30"]
    assert kids.runtime_data.jobs.targets == {"30": "e"}
    assert entry.runtime_data.jobs.targets == {}

    assert await hass.config_entries.async_unload(kids.entry_id)
    await hass.async_block_till_done()


async def test_one_steam_account_on_two_pcs(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A new purchase on an account two PCs share: one push (from the PC without a name) with a
    button per PC that lacks the game; the Kinder-PC's button installs on the Kinder-PC only."""
    pushes = async_mock_service(hass, "notify", "phone")
    publishes = async_mock_service(hass, "mqtt", "publish")
    owned = lambda *ids: {"response": {"game_count": len(ids), "games": [{"appid": int(i), "name": f"Game {i}"} for i in ids]}}  # noqa: E731
    aioclient_mock.get(OWNED_URL, json=owned("10"))
    hass.states.async_set(LIBRARY, str(len(GAMES)), {"games": GAMES})
    hass.states.async_set("sensor.steam_pc_kinderpc_library", "0", {"games": []})
    # The Kinder-PC is on and its agent reports (as _pc_on in test_jobs_power.py)
    hass.states.async_set("binary_sensor.steam_pc_kinderpc_agent_online", "on")
    hass.states.async_set("binary_sensor.steam_pc_kinderpc_game_running", "off", {"appid": 0})
    hass.states.async_set("sensor.steam_pc_kinderpc_network", "100", {"unit_of_measurement": "B/s"})
    hass.states.async_set("button.steam_pc_kinderpc_shutdown", "unknown")
    hass.states.async_set("sensor.steam_pc_kinderpc_last_input", (dt_util.utcnow() - timedelta(hours=2)).isoformat())
    options = merged_options(
        {
            const.SECTION_NOTIFY: {const.CONF_NOTIFY_SERVICE: "phone"},
            const.SECTION_STEAM: {const.CONF_API_KEY: "KEY", const.CONF_STEAM_ID: "76561198000000001"},
        }
    )
    first = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    kids = MockConfigEntry(domain=DOMAIN, data={"pc_name": "Kinder-PC"}, options=options, unique_id="kinderpc")
    for item in (first, kids):
        item.add_to_hass(hass)
        assert await hass.config_entries.async_setup(item.entry_id)
        await hass.async_block_till_done()

    # Game 50 bought: both PCs find it, only the PC without a name reports it
    aioclient_mock.clear_requests()
    aioclient_mock.get(OWNED_URL, json=owned("10", "50"))
    await hass.services.async_call(DOMAIN, "check_owned", {"pc": "Kinder-PC"}, blocking=True, return_response=True)
    await hass.services.async_call(DOMAIN, "check_owned", {}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    purchases = [c for c in pushes if c.data.get("data", {}).get("tag", "").startswith("steam_engine_new_purchase")]
    assert len(purchases) == 1
    push = purchases[0].data
    assert push["data"]["tag"] == "steam_engine_new_purchase"
    assert push["data"]["actions"] == [
        {"action": "STEAM_ENGINE_INSTALL_PURCHASES_50", "title": "Install on Steam PC"},
        {"action": "STEAM_ENGINE_INSTALL_PURCHASES_50@kinderpc", "title": "Install on Kinder-PC"},
    ]
    assert first.runtime_data.owned.new == ["50"]
    assert kids.runtime_data.owned.new == ["50"]

    # The Kinder-PC's button: its agent gets the job, not the other PC's
    hass.bus.async_fire("mobile_app_notification_action", {"action": "STEAM_ENGINE_INSTALL_PURCHASES_50@kinderpc"})
    for _ in range(3):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
        await hass.async_block_till_done()
    installs = [c.data for c in publishes if str(c.data.get("payload", "")).startswith("install:")]
    assert [c["topic"] for c in installs] == ["steam/pc/kinderpc/agent/command"]
    assert re.fullmatch(r"install:[0-9a-f]{8}:50,", installs[0]["payload"])  # with the job ID
    # A new purchase: it may go to the Kinder-PC's overflow drive (steam_jobs.py)
    assert kids.runtime_data.jobs.purchased == ["50"]
    assert first.runtime_data.jobs.purchased == []

    for item in (kids, first):
        assert await hass.config_entries.async_unload(item.entry_id)
    await hass.async_block_till_done()


async def test_action_without_pc_never_goes_to_another_pc(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """While the PC without a name is not loaded (a reload, disabled, failed to set up), an action
    without "pc" says so; it does not go to the other PC, the only one loaded then."""
    kids = MockConfigEntry(domain=DOMAIN, data={"pc_name": "Kinder-PC"}, options=merged_options({}), unique_id="kinderpc")
    kids.add_to_hass(hass)
    assert await hass.config_entries.async_setup(kids.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "set_auto_update", {"appid": "10", "enabled": True}, blocking=True)
    assert err.value.translation_key == "not_loaded"
    assert kids.runtime_data.jobs.auto_update == []
    # Asked for by its name, the other PC still answers
    await hass.services.async_call(
        DOMAIN, "set_auto_update", {"appid": "20", "enabled": True, "pc": "Kinder-PC"}, blocking=True
    )
    assert kids.runtime_data.jobs.auto_update == ["20"]

    # Loaded again: the action without "pc" goes to it
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "set_auto_update", {"appid": "10", "enabled": True}, blocking=True)
    assert entry.runtime_data.jobs.auto_update == ["10"]
    assert kids.runtime_data.jobs.auto_update == ["20"]
    assert await hass.config_entries.async_unload(kids.entry_id)
    await hass.async_block_till_done()


async def test_hints_name_the_pc_only_with_several_pcs(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A hint under Repairs is one per PC; while there is more than one PC its title starts with the
    PC's name ({pc}), as push titles do, the PC without a name as "Steam PC"."""
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, "agent_missing").translation_placeholders == {"pc": ""}
    kids = MockConfigEntry(domain=DOMAIN, data={"pc_name": "Kinder-PC"}, options=merged_options({}), unique_id="kinderpc")
    kids.add_to_hass(hass)
    assert await hass.config_entries.async_setup(kids.entry_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, "agent_missing_kinderpc").translation_placeholders == {"pc": "Kinder-PC: "}
    # The first PC's hint names it once it is set again (here by a reload)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, "agent_missing").translation_placeholders == {"pc": "Steam PC: "}
    assert await hass.config_entries.async_unload(kids.entry_id)
    await hass.async_block_till_done()


STEAM_ID = "76561198000000001"
OTHER_STEAM_ID = "76561198000000002"


def _owned(*appids: str) -> dict:
    """The Steam Web API's answer with these owned games."""
    return {"response": {"game_count": len(appids), "games": [{"appid": int(a), "name": f"Game {a}"} for a in appids]}}


def _account(steam_id: str) -> dict[str, dict]:
    return merged_options(
        {
            const.SECTION_NOTIFY: {const.CONF_NOTIFY_SERVICE: "phone"},
            const.SECTION_STEAM: {const.CONF_API_KEY: "KEY", const.CONF_STEAM_ID: steam_id},
        }
    )


def _purchase_pushes(pushes: list) -> list:
    return [c for c in pushes if c.data.get("data", {}).get("tag", "").startswith("steam_engine_new_purchase")]


async def test_another_steam_account_brings_no_purchases(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A Steam ID changed on the settings page (the integration reloads): the owned games of the
    account before are not taken over, so the first check of the new account stays quiet like a
    first check: no event, no push, no install, also with "Install new purchases" on."""
    pushes = async_mock_service(hass, "notify", "phone")
    purchased = async_capture_events(hass, EVENT_PURCHASED)
    aioclient_mock.get(OWNED_URL, json=_owned("10", "60", "70"))
    hass.states.async_set(LIBRARY, str(len(GAMES)), {"games": GAMES})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=_account(STEAM_ID))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.owned.owned == ["10", "60", "70"]
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: ENTITY_AUTO_INSTALL_PURCHASES}, blocking=True)

    aioclient_mock.clear_requests()
    aioclient_mock.get(OWNED_URL, json=_owned("10", "80", "90"))
    hass.config_entries.async_update_entry(entry, options=_account(OTHER_STEAM_ID))
    await hass.async_block_till_done(wait_background_tasks=True)
    owned = entry.runtime_data.owned
    assert owned.steam_id == OTHER_STEAM_ID
    assert owned.owned == ["10", "80", "90"]
    assert [g["appid"] for g in owned.missing] == ["80", "90"]
    assert owned.new == []
    assert purchased == []
    assert _purchase_pushes(pushes) == []
    assert entry.runtime_data.jobs.job == []
    assert entry.runtime_data.jobs.purchased == []
    assert hass.states.get(ENTITY_AUTO_INSTALL_PURCHASES).state == "on"

    # A purchase of the new account is one (pushed only: the switch is off now)
    await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: ENTITY_AUTO_INSTALL_PURCHASES}, blocking=True)
    aioclient_mock.clear_requests()
    aioclient_mock.get(OWNED_URL, json=_owned("10", "80", "90", "95"))
    await hass.services.async_call(DOMAIN, "check_owned", {}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    assert [e.data["games"] for e in purchased] == [[{"appid": "95", "name": "Game 95"}]]
    assert len(_purchase_pushes(pushes)) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("stored_id", "new"),
    [(STEAM_ID, ["70"]), (OTHER_STEAM_ID, []), (None, [])],
    ids=["same account", "another account", "account not stored"],
)
async def test_owned_games_restored_only_for_their_account(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, stored_id: str | None, new: list[str]
) -> None:
    """After a restart the owned games of the last check are taken over only when they were checked
    for the account set now (kept with them): a game bought meanwhile is a new purchase. Those of
    another account, or of an account not known, are dropped: the first check stays quiet."""
    last = State(
        NOT_INSTALLED_SENSOR_ENTITY_ID,
        "1",
        {"games": [{"appid": "60", "name": "Game 60"}], "owned": ["10", "60"], "new": []},
    )
    if stored_id is None:
        mock_restore_cache(hass, [last])
    else:
        mock_restore_cache_with_extra_data(hass, [(last, {"steam_id": stored_id})])
    async_mock_service(hass, "notify", "phone")
    purchased = async_capture_events(hass, EVENT_PURCHASED)
    aioclient_mock.get(OWNED_URL, json=_owned("10", "60", "70"))
    hass.states.async_set(LIBRARY, str(len(GAMES)), {"games": GAMES})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=_account(STEAM_ID))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.owned.owned == ["10", "60", "70"]
    assert entry.runtime_data.owned.new == new
    assert len(purchased) == len(new)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_install_schema_drive() -> None:
    """The field "drive": a drive letter with or without the colon, in any case, sent as one lower-case
    letter; empty is no drive; anything else (a path, two letters, a digit) is refused."""
    assert INSTALL_SCHEMA({"appid": "440"}) == {"appid": "440"}
    for value, letter in (("E", "e"), ("e", "e"), ("F:", "f"), (" f: ", "f"), ("", None), ("  ", None)):
        assert INSTALL_SCHEMA({"appid": "440", "drive": value})["drive"] == letter
    for value in ("EF", "1", "E:\\", "E:\\SteamLibrary", "Ä", ":"):
        with pytest.raises(vol.Invalid):
            INSTALL_SCHEMA({"appid": "440", "drive": value})


async def test_install_passes_the_drive(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The action hands the normalized letter to the jobs; without the field a stored target stays."""
    jobs = entry.runtime_data.jobs
    await hass.services.async_call(
        DOMAIN, "install", {"appid": "20", "drive": "D"}, blocking=True, return_response=True
    )
    assert jobs.targets == {"20": "d"}
    await hass.services.async_call(DOMAIN, "install", {"appid": "20"}, blocking=True, return_response=True)
    assert jobs.targets == {"20": "d"}
    # An empty field (a template with default('')) is no drive either
    await hass.services.async_call(
        DOMAIN, "install", {"appid": "20", "drive": ""}, blocking=True, return_response=True
    )
    assert jobs.targets == {"20": "d"}
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "install", {"appid": "20", "drive": "D:\\Games"}, blocking=True)
