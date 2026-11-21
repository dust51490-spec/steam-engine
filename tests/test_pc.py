"""The names of a PC (pc.py). The unnamed PC's names are written out here letter by letter: they are
what Home Assistant, automations, dashboards and pushes on phones know, and must never change. A
named PC gets the names the PC side gives it (Get-SteamEngineNames in mqtt_publish.ps1)."""

from __future__ import annotations

import pytest

from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.steam_engine import agent_entities, const, no_space, push, steam_images, steam_notify
from custom_components.steam_engine.push import for_pc, send_push
from custom_components.steam_engine.pc import AGENT_ENTITIES, UNNAMED, pc_id_of, pc_names

OWN_ENTITY_IDS = {
    "ENTITY_PC": "switch.steam_engine_pc",
    "ENTITY_PC_STATE": "sensor.steam_engine_pc_state",
    "ENTITY_AUTO_OFF_ENABLED": "switch.steam_engine_auto_off",
    "ENTITY_WOKEN_BY_AUTOMATION": "switch.steam_engine_woken_by_automation",
    "ENTITY_AUTO_INSTALL_PURCHASES": "switch.steam_engine_install_new_purchases",
    "SAFE_SENSOR_ENTITY_ID": "binary_sensor.steam_engine_safe_to_shut_down",
    "ENTITY_STEAM_BUSY": "binary_sensor.steam_engine_steam_busy",
    "ENTITY_PLUGIN_BUSY": "binary_sensor.steam_engine_busy_by_plugin",
    "UPDATES_SENSOR_ENTITY_ID": "sensor.steam_engine_updates",
    "LAST_ANSWER_SENSOR_ENTITY_ID": "sensor.steam_engine_last_answer",
    "QUERY_SENSOR_ENTITY_ID": "sensor.steam_engine_query",
    "NOT_INSTALLED_SENSOR_ENTITY_ID": "sensor.steam_engine_not_installed",
    "ENTITY_DOWNLOAD_PROGRESS": "sensor.steam_engine_download_progress",
    "ENTITY_CURRENT_GAME": "sensor.steam_engine_current_game",
    "ENTITY_GAME_TO_INSTALL": "select.steam_engine_game_to_install",
    "ENTITY_INSTALLED_GAME": "select.steam_engine_installed_game",
    "ENTITY_INSTALL_GAME": "button.steam_engine_install_game",
    "ENTITY_UNINSTALL_GAME": "button.steam_engine_uninstall_game",
    "ENTITY_LAUNCH_GAME": "button.steam_engine_launch_game",
    "ENTITY_AUTO_UPDATE_PICKED": "switch.steam_engine_auto_update_picked_game",
}

AGENT_UNIQUE_IDS = {
    "library": ("steam_pc_library", "sensor.steam_pc_library"),
    "install_status": ("steam_pc_install_status", "sensor.steam_pc_install_status"),
    "history": ("steam_pc_history", "sensor.steam_pc_history"),
    "agent_online": ("steam_pc_agent_online", "binary_sensor.steam_pc_agent_online"),
    "steam_downloading": ("steam_pc_steam_downloading", "binary_sensor.steam_pc_steam_downloading"),
    "steam_running": ("steam_pc_steam_running", "binary_sensor.steam_pc_steam_running"),
    "game_running": ("steam_pc_game_running", "binary_sensor.steam_pc_game_running"),
    "last_input": ("steam_pc_last_input", "sensor.steam_pc_last_input"),
    "network": ("steam_pc_network", "sensor.steam_pc_network"),
    "shutdown_button": ("steam_pc_shutdown", "button.steam_pc_shutdown"),
    "restart_button": ("steam_pc_restart", "button.steam_pc_restart"),
}

TAGS = {
    "TAG_PC_IDLE": "steam_engine_pc_idle",
    "TAG_AUTO_OFF_REMINDER": "steam_engine_auto_off_reminder",
    "TAG_PC_LOCK": "steam_engine_pc_lock",
    "TAG_PROGRESS": "steam_engine_progress",
    "TAG_UPDATES": "steam_engine_updates",
    "TAG_STALLED": "steam_engine_stalled",
    "TAG_AGENT": "steam_engine_agent",
    "TAG_UNINSTALL": "steam_engine_uninstall",
    "TAG_UNINSTALL_ASK": "steam_engine_uninstall_ask",
    "TAG_MOVE": "steam_engine_move",
    "TAG_NO_SPACE": "steam_engine_no_space",
    "TAG_LAUNCH": "steam_engine_launch",
    "TAG_PURCHASE": "steam_engine_new_purchase",
}


def test_unnamed_pc_keeps_every_name() -> None:
    for name, entity_id in OWN_ENTITY_IDS.items():
        assert getattr(const, name) == entity_id, name
    # Every entity ID constant of const.py is in the list above
    assert {n for n in vars(const) if n.startswith("ENTITY_") or n.endswith("_ENTITY_ID")} == set(OWN_ENTITY_IDS)
    assert agent_entities.UNIQUE_IDS == AGENT_UNIQUE_IDS
    for name, tag in TAGS.items():
        assert getattr(push, name) == tag, name
    assert {n for n in vars(push) if n.startswith("TAG_")} == set(TAGS)
    assert const.AGENT_COMMAND_TOPIC == "steam/agent/command"
    assert const.UNINSTALL_RESULT_TOPIC == "steam/uninstall"
    assert const.MOVE_TOPIC == "steam/move"
    assert const.DEPOTS_TOPIC == "steam/depots"
    assert const.LIBRARY_TOPIC == "steam/library"
    assert const.LAUNCH_PENDING_TOPIC == "steam/launch_pending"
    assert const.AGENT_DEVICE == ("mqtt", "steam_pc")
    assert const.LABEL_PC_BUSY == "pc_busy"
    assert const.STORAGE_KEY == "steam_engine.lists"
    assert steam_images.STORAGE_KEY == "steam_engine.images"
    assert const.ACTION_PC_SHUTDOWN == "STEAM_ENGINE_PC_SHUTDOWN"
    assert const.ACTION_AUTO_OFF_ON == "STEAM_ENGINE_AUTO_OFF_ON"
    assert no_space.ACTION_UNINSTALL == "STEAM_ENGINE_UNINSTALL_"
    assert steam_notify.ACTION_INSTALL_ALL == "STEAM_ENGINE_INSTALL_ALL"
    assert UNNAMED.device_name == "Steam Engine"
    assert UNNAMED.title("Steam: 2 updates") == "Steam: 2 updates"
    assert UNNAMED.action("STEAM_ENGINE_UNINSTALL_ASK_440") == "STEAM_ENGINE_UNINSTALL_ASK_440"
    assert pc_names("") == pc_names("   ") == UNNAMED


def test_named_pc() -> None:
    kids = pc_names(" Kinder-PC ")
    assert (kids.pc_id, kids.pc_name, kids.named) == ("kinderpc", "Kinder-PC", True)
    assert kids.agent_command_topic == "steam/pc/kinderpc/agent/command"
    assert kids.uninstall_result_topic == "steam/pc/kinderpc/uninstall"
    assert kids.move_topic == "steam/pc/kinderpc/move"
    assert kids.depots_topic == "steam/pc/kinderpc/depots"
    assert kids.library_topic == "steam/pc/kinderpc/library"
    assert kids.launch_pending_topic == "steam/pc/kinderpc/launch_pending"
    # As the PC side announces them (seen in the sandbox test of 2026-11-09)
    assert kids.agent_entity("library") == ("steam_pc_kinderpc_library", "sensor.steam_pc_kinderpc_library")
    assert kids.agent_entity("shutdown_button") == ("steam_pc_kinderpc_shutdown", "button.steam_pc_kinderpc_shutdown")
    assert kids.agent_entity("agent_online") == (
        "steam_pc_kinderpc_agent_online",
        "binary_sensor.steam_pc_kinderpc_agent_online",
    )
    assert kids.agent_device == ("mqtt", "steam_pc_kinderpc")
    assert kids.own_entity_id("switch", "pc") == "switch.steam_engine_kinderpc_pc"
    assert kids.device_name == "Steam Engine Kinder-PC"
    assert kids.tag("steam_engine_progress") == "steam_engine_progress_kinderpc"
    assert kids.action("STEAM_ENGINE_PC_SHUTDOWN") == "STEAM_ENGINE_PC_SHUTDOWN@kinderpc"
    assert kids.title("Steam: 2 updates") == "Kinder-PC: Steam: 2 updates"
    assert kids.store_key == "steam_engine.lists_kinderpc"
    assert kids.busy_label == "pc_busy_kinderpc"
    # Every agent entity of a named PC has a unique ID and entity ID of its own
    assert len({kids.agent_entity(f)[0] for f in AGENT_ENTITIES}) == len(AGENT_ENTITIES)


@pytest.mark.parametrize(
    ("name", "pc_id"),
    [
        ("", ""),
        ("Kinder-PC", "kinderpc"),
        ("Kinder-PC Über", "kinderpcueber"),
        ("Büro Größe", "buerogroesse"),
        ("Sehr langer Name fuer einen PC", "sehrlangernamefu"),
    ],
)
def test_pc_id_as_the_pc_side_makes_it(name: str, pc_id: str) -> None:
    assert pc_id_of(name) == pc_id


def test_pc_name_without_letters() -> None:
    with pytest.raises(ValueError):
        pc_id_of("!!!")


PUSH = {
    "title": "Steam: 2 updates",
    "message": "Alpha, Beta",
    "data": {
        "tag": "steam_engine_progress",
        "actions": [
            {"action": "STEAM_ENGINE_INSTALL_ALL", "title": "Install all"},
            {"action": "URI", "title": "Open", "uri": "/lovelace"},
        ],
    },
}


def test_pushes_per_pc() -> None:
    # The unnamed PC's pushes stay exactly as they are
    assert for_pc(PUSH, UNNAMED) is PUSH
    kids = pc_names("Kinder-PC")
    assert for_pc(PUSH, kids) == {
        "title": "Kinder-PC: Steam: 2 updates",
        "message": "Alpha, Beta",
        "data": {
            "tag": "steam_engine_progress_kinderpc",
            "actions": [
                {"action": "STEAM_ENGINE_INSTALL_ALL@kinderpc", "title": "Install all"},
                {"action": "URI", "title": "Open", "uri": "/lovelace"},
            ],
        },
    }
    clear = {"message": "clear_notification", "data": {"tag": "steam_engine_pc_idle"}}
    assert for_pc(clear, kids) == {"message": "clear_notification", "data": {"tag": "steam_engine_pc_idle_kinderpc"}}
    assert PUSH["data"]["tag"] == "steam_engine_progress"  # not changed in place


def test_push_buttons_reach_only_their_pc() -> None:
    kids = pc_names("Kinder-PC")
    office = pc_names("Office")
    assert UNNAMED.action_of("STEAM_ENGINE_PC_SHUTDOWN") == "STEAM_ENGINE_PC_SHUTDOWN"
    assert UNNAMED.action_of("STEAM_ENGINE_PC_SHUTDOWN@kinderpc") is None
    assert kids.action_of("STEAM_ENGINE_PC_SHUTDOWN@kinderpc") == "STEAM_ENGINE_PC_SHUTDOWN"
    assert kids.action_of("STEAM_ENGINE_PC_SHUTDOWN") is None
    assert office.action_of("STEAM_ENGINE_PC_SHUTDOWN@kinderpc") is None
    assert kids.action_of("STEAM_ENGINE_UNINSTALL_ASK_440@kinderpc") == "STEAM_ENGINE_UNINSTALL_ASK_440"


def test_hints_store_and_labels_per_pc() -> None:
    kids = pc_names("Kinder-PC")
    assert UNNAMED.issue_id("agent_not_running") == "agent_not_running"
    assert kids.issue_id("agent_not_running") == "agent_not_running_kinderpc"
    assert UNNAMED.busy_labels == ("pc_busy",)
    assert kids.busy_labels == ("pc_busy_kinderpc", "pc_busy")


async def test_pc_name_in_titles_only_with_several_pcs(hass: HomeAssistant) -> None:
    """With one PC its name would only take room in the push; the tag and buttons stay its own."""
    pushes = async_mock_service(hass, "notify", "phone")
    kids = pc_names("Kinder-PC")
    # A found PC that was ignored is none
    MockConfigEntry(domain=const.DOMAIN, source=SOURCE_IGNORE, unique_id="office", data={}).add_to_hass(hass)
    MockConfigEntry(domain=const.DOMAIN, data={"pc_name": "Kinder-PC"}).add_to_hass(hass)
    send_push(hass, {const.CONF_NOTIFY_SERVICE: "phone"}, PUSH, kids)
    await hass.async_block_till_done()
    assert pushes[-1].data["title"] == "Steam: 2 updates"
    assert pushes[-1].data["data"]["tag"] == "steam_engine_progress_kinderpc"

    MockConfigEntry(domain=const.DOMAIN, data={}).add_to_hass(hass)
    send_push(hass, {const.CONF_NOTIFY_SERVICE: "phone"}, PUSH, kids)
    send_push(hass, {const.CONF_NOTIFY_SERVICE: "phone"}, PUSH, UNNAMED)
    await hass.async_block_till_done()
    assert pushes[-2].data["title"] == "Kinder-PC: Steam: 2 updates"
    assert pushes[-1].data["title"] == "Steam: 2 updates"


def test_buttons_of_another_pc_keep_their_mark() -> None:
    push = {"title": "t", "data": {"actions": [{"action": "STEAM_ENGINE_INSTALL_PURCHASES_50@office", "title": "x"}]}}
    out = for_pc(push, pc_names("Kinder-PC"))
    assert out["data"]["actions"][0]["action"] == "STEAM_ENGINE_INSTALL_PURCHASES_50@office"

