"""Set-up dialog and settings page: the Steam account is checked only when key or Steam ID changed."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_IGNORE, SOURCE_MQTT, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.steam_engine.const import (
    CONF_ACTIVE_ENABLED,
    CONF_ACTIVE_END,
    CONF_ACTIVE_START,
    CONF_API_KEY,
    CONF_LAUNCH_UNLOCKED,
    CONF_NOTIFY_SERVICE,
    CONF_OVERFLOW_DRIVE,
    CONF_PUSH_KINDS,
    CONF_QUIET_AUTO_UPDATES,
    CONF_REACHABLE_ENTITY,
    CONF_STEAM_ID,
    CONF_WAKE_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
    SECTION_ACTIVE_HOURS,
    SECTION_NOTIFY,
    SECTION_PC,
    SECTION_STEAM,
    merged_options,
)
from custom_components.steam_engine.config_flow import _overflow_drives
from custom_components.steam_engine.pc import UNNAMED, pc_names
from custom_components.steam_engine.steam_api import OWNED_URL

STEAM_ID = "76561198000000001"
OWNED_OK = {"response": {"game_count": 1, "games": [{"appid": 10}]}}


def _form_input(api_key: str = "", steam_id: str = "", options: dict | None = None) -> dict[str, Any]:
    """What the form sends: every section, without the empty entity fields."""
    data = merged_options(options or DEFAULT_OPTIONS)
    data[SECTION_PC] = {k: v for k, v in data[SECTION_PC].items() if v}
    data[SECTION_STEAM][CONF_API_KEY] = api_key
    data[SECTION_STEAM][CONF_STEAM_ID] = steam_id
    data[SECTION_STEAM][CONF_OVERFLOW_DRIVE] = data[SECTION_STEAM][CONF_OVERFLOW_DRIVE] or "none"
    return data


def _shown(result: dict[str, Any], section: str) -> dict[str, Any]:
    """What a form shows in one section: the suggested values and the defaults."""
    shown: dict[str, Any] = {}
    for key, value in result["data_schema"].schema.items():
        if key == section:
            for field in value.schema.schema:
                if field.description and "suggested_value" in field.description:
                    shown[str(field)] = field.description["suggested_value"]
                elif callable(field.default):
                    shown[str(field)] = field.default()
    return shown


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return result["flow_id"]


async def test_setup_without_steam_account(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    flow_id = await _start(hass)
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id, _form_input())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert aioclient_mock.call_count == 0


async def test_setup_stores_steam_id64_of_profile_url(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(OWNED_URL, json=OWNED_OK)
    flow_id = await _start(hass)
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            flow_id, _form_input("KEY", f"https://steamcommunity.com/profiles/{STEAM_ID}/")
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][SECTION_STEAM][CONF_STEAM_ID] == STEAM_ID


async def test_setup_shows_rejected_key(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(OWNED_URL, status=403)
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, _form_input("BAD", STEAM_ID))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_api_key"}


async def test_options_unchanged_account_is_not_checked(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    stored = merged_options({SECTION_STEAM: {CONF_API_KEY: "KEY", CONF_STEAM_ID: STEAM_ID}})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=stored)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _form_input("KEY", STEAM_ID, stored)
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert aioclient_mock.call_count == 0


async def test_options_private_profile_is_shown(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(OWNED_URL, json={"response": {}})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], _form_input("KEY", STEAM_ID))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "profile_private"}


async def test_options_cleared_fields_are_saved_empty(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The form leaves out a field the user emptied; it must be saved empty, not as before."""
    stored = merged_options({SECTION_STEAM: {CONF_API_KEY: "KEY", CONF_STEAM_ID: STEAM_ID}})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=stored)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    form = _form_input("", "", stored)
    del form[SECTION_STEAM][CONF_API_KEY], form[SECTION_STEAM][CONF_STEAM_ID]
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][SECTION_STEAM][CONF_API_KEY] == ""
    assert result["data"][SECTION_STEAM][CONF_STEAM_ID] == ""
    assert aioclient_mock.call_count == 0


async def test_options_notify_service(hass: HomeAssistant) -> None:
    """notify. in front is cut off; a changed service that does not exist is refused."""
    hass.services.async_register("notify", "mobile_app_phone", lambda call: None)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    form = _form_input()
    form[SECTION_NOTIFY][CONF_NOTIFY_SERVICE] = "notify.mobile_app_nothere"
    result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["errors"] == {"base": "notify_not_found"}
    form[SECTION_NOTIFY][CONF_NOTIFY_SERVICE] = "notify.mobile_app_phone"
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][SECTION_NOTIFY][CONF_NOTIFY_SERVICE] == "mobile_app_phone"


async def test_options_no_pushes_and_kinds(hass: HomeAssistant) -> None:
    """"No pushes" is stored as an empty service; the kinds keep their order, unknown ones go."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    form = _form_input()
    form[SECTION_NOTIFY][CONF_NOTIFY_SERVICE] = "none"
    form[SECTION_NOTIFY][CONF_PUSH_KINDS] = ["problems", "result"]
    form[SECTION_NOTIFY][CONF_QUIET_AUTO_UPDATES] = True
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    notify = result["data"][SECTION_NOTIFY]
    assert notify[CONF_NOTIFY_SERVICE] == ""
    assert notify[CONF_PUSH_KINDS] == ["result", "problems"]
    assert notify[CONF_QUIET_AUTO_UPDATES] is True


async def test_options_active_hours(hass: HomeAssistant) -> None:
    """Off by default (08:00 to 20:00 preset); a window across midnight is saved as entered."""
    assert DEFAULT_OPTIONS[SECTION_ACTIVE_HOURS] == {
        CONF_ACTIVE_ENABLED: False,
        CONF_ACTIVE_START: "08:00:00",
        CONF_ACTIVE_END: "20:00:00",
    }
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    form = _form_input()
    form[SECTION_ACTIVE_HOURS] = {CONF_ACTIVE_ENABLED: True, CONF_ACTIVE_START: "22:00:00", CONF_ACTIVE_END: "06:00:00"}
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][SECTION_ACTIVE_HOURS] == {
        CONF_ACTIVE_ENABLED: True,
        CONF_ACTIVE_START: "22:00:00",
        CONF_ACTIVE_END: "06:00:00",
    }


async def test_options_launch_unlocked(hass: HomeAssistant) -> None:
    """Off by default (also when the form leaves it out); switched on it is saved in the PC section."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    form = _form_input()
    assert CONF_LAUNCH_UNLOCKED not in form[SECTION_PC]
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][SECTION_PC][CONF_LAUNCH_UNLOCKED] is False

    result = await hass.config_entries.options.async_init(entry.entry_id)
    form = _form_input()
    form[SECTION_PC][CONF_LAUNCH_UNLOCKED] = True
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][SECTION_PC][CONF_LAUNCH_UNLOCKED] is True


async def test_options_overflow_drive(hass: HomeAssistant) -> None:
    """None by default; the choice lists the drives the PC reports (with what is known of them, the
    default library's drive marked as without effect) and the stored drive also when it is not
    reported now; stored as a lower-case letter, "none" as ""."""
    assert DEFAULT_OPTIONS[SECTION_STEAM][CONF_OVERFLOW_DRIVE] == ""
    drives = [
        {"drive": "F:", "free_gb": 42, "library": "F:\\SteamLibrary", "default": True, "bus": "nvme", "media": "ssd"},
        {"drive": "E:", "free_gb": 812, "library": "E:\\SteamLibrary", "default": False, "bus": "sata", "media": "hdd"},
        {"drive": "G:", "free_gb": 230, "library": None, "default": False, "bus": "usb", "media": "ssd"},
    ]
    hass.states.async_set("sensor.steam_pc_library_free", "42", {"drives": drives})
    assert _overflow_drives(hass, UNNAMED, "z") == [
        {"value": "none", "label": "none"},
        {"value": "f", "label": "F: · SSD · 42 GB free · default library, no effect"},
        {"value": "e", "label": "E: · HDD · 812 GB free"},
        {"value": "g", "label": "G: · SSD · USB · 230 GB free · new library"},
        {"value": "z", "label": "Z: · not reported now"},
    ]
    # Another PC's drives come from its own sensor
    assert _overflow_drives(hass, pc_names("Kinder-PC"), "") == [{"value": "none", "label": "none"}]

    entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options({}))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    form = _form_input()
    form[SECTION_STEAM][CONF_OVERFLOW_DRIVE] = "e"
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][SECTION_STEAM][CONF_OVERFLOW_DRIVE] == "e"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    form = _form_input()
    form[SECTION_STEAM][CONF_OVERFLOW_DRIVE] = "none"
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], form)
    assert result["data"][SECTION_STEAM][CONF_OVERFLOW_DRIVE] == ""


async def test_second_pc_asks_its_name(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """With one PC set up, a further one is asked for its name (not one in use, not without letters),
    then for its settings, filled in with the first PC's."""
    first_options = merged_options({SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: "mobile_app_phone"}})
    MockConfigEntry(domain=DOMAIN, data={}, options=first_options).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pc"
    flow_id = result["flow_id"]

    result = await hass.config_entries.flow.async_configure(flow_id, {"pc_name": "!!!"})
    assert result["errors"] == {"base": "pc_name_invalid"}
    result = await hass.config_entries.flow.async_configure(flow_id, {"pc_name": "Steam PC"})
    assert result["errors"] == {"base": "pc_exists"}
    result = await hass.config_entries.flow.async_configure(flow_id, {"pc_name": " Kinder-PC "})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    assert result["description_placeholders"] == {"pc": "Kinder-PC"}

    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id, _form_input(options=first_options))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Steam Engine Kinder-PC"
    assert result["data"] == {"pc_name": "Kinder-PC"}
    assert result["options"][SECTION_NOTIFY][CONF_NOTIFY_SERVICE] == "mobile_app_phone"
    assert result["result"].unique_id == "kinderpc"
    assert aioclient_mock.call_count == 0

    # The same PC once more: refused by its name
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pc_name": "kinder pc"})
    assert result["errors"] == {"base": "pc_exists"}


async def test_further_pc_keeps_no_pc_fields_of_the_first(hass: HomeAssistant) -> None:
    """A further PC's settings are the first PC's, but not its own fields: PC reachable and Turn on
    with are the first PC's entities (the new PC would count as on while that one is), Start games
    without the lock is decided for each PC, and the overflow drive is one of the first PC's drives."""
    first_options = merged_options(
        {
            SECTION_PC: {
                CONF_REACHABLE_ENTITY: "binary_sensor.pc_ping",
                CONF_WAKE_ENTITY: "input_button.wake_pc",
                CONF_LAUNCH_UNLOCKED: True,
            },
            SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: "mobile_app_phone"},
            SECTION_STEAM: {CONF_OVERFLOW_DRIVE: "e"},
        }
    )
    MockConfigEntry(domain=DOMAIN, data={}, options=first_options).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pc_name": "Kinder-PC"})
    assert result["step_id"] == "settings"
    assert _shown(result, SECTION_PC) == {CONF_LAUNCH_UNLOCKED: False}
    assert _shown(result, SECTION_NOTIFY)[CONF_NOTIFY_SERVICE] == "mobile_app_phone"
    assert _shown(result, SECTION_STEAM)[CONF_OVERFLOW_DRIVE] == "none"


def _info(topic: str, payload: str) -> MqttServiceInfo:
    return MqttServiceInfo(topic=topic, payload=payload, qos=0, retain=True, subscribed_topic=topic, timestamp=0)


async def test_found_pc(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A named PC's agent/info shows the PC as found; confirmed, its settings follow (the first
    PC's), the entry gets the PC's name. The PC without a name is set up already: no flow."""
    first_options = merged_options({SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: "mobile_app_phone"}})
    MockConfigEntry(domain=DOMAIN, data={}, options=first_options).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_MQTT}, data=_info("steam/agent/info", '{"computer": "A", "pc_id": "", "pc_name": ""}')
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    payload = '{"computer": "KIDS-1", "pc_id": "kinderpc", "pc_name": "Kinder-PC"}'
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_MQTT}, data=_info("steam/pc/kinderpc/agent/info", payload)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "found"
    assert result["description_placeholders"] == {"pc": "Kinder-PC", "computer": "KIDS-1"}
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["step_id"] == "settings"
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id, _form_input(options=first_options))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Steam Engine Kinder-PC"
    assert result["data"] == {"pc_name": "Kinder-PC"}
    assert result["options"][SECTION_NOTIFY][CONF_NOTIFY_SERVICE] == "mobile_app_phone"
    assert result["result"].unique_id == "kinderpc"
    assert aioclient_mock.call_count == 0

    # Found again (each connect of its agent): set up already
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_MQTT}, data=_info("steam/pc/kinderpc/agent/info", payload)
    )
    assert result["reason"] == "already_configured"


async def test_found_pc_refused(hass: HomeAssistant) -> None:
    """No flow for a cleared agent/info (uninstall), another payload, or a name not matching its topic."""
    for topic, payload in (
        ("steam/pc/kinderpc/agent/info", ""),
        ("steam/pc/kinderpc/agent/info", "online"),
        ("steam/pc/kinderpc/agent/info", '{"pc_name": "Büro"}'),
        ("steam/pc/kinderpc/agent/info", '{"pc_name": "!!!"}'),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_MQTT}, data=_info(topic, payload))
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "not_supported"


async def test_found_first_pc(hass: HomeAssistant) -> None:
    """With nothing set up, the PC without a name is found: confirmed, the full settings follow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_MQTT}, data=_info("steam/agent/info", '{"computer": "PC", "pc_id": "", "pc_name": ""}')
    )
    assert result["step_id"] == "found"
    assert result["description_placeholders"] == {"pc": "Steam PC", "computer": "PC"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "user"
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], _form_input())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Steam Engine"
    assert result["data"] == {}
    assert result["result"].unique_id == "steampc"


async def test_ignored_pc_is_no_pc(hass: HomeAssistant) -> None:
    """A found PC the user ignored is an entry without data, so it would look like the PC without a
    name; it is no PC. The PC without a name is still found and set up as the first PC, a further
    PC's settings are filled in with its (not the ignored entry's), and the ignored PC stays ignored."""
    hass.services.async_register("notify", "mobile_app_phone", lambda call: None)
    MockConfigEntry(domain=DOMAIN, source=SOURCE_IGNORE, unique_id="kinderpc", data={}).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_MQTT}, data=_info("steam/agent/info", '{"computer": "PC", "pc_id": "", "pc_name": ""}')
    )
    assert result["step_id"] == "found"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "user"
    first_options = merged_options({SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: "mobile_app_phone"}})
    with patch("custom_components.steam_engine.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], _form_input(options=first_options))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "steampc"

    payload = '{"computer": "OFFICE", "pc_id": "buero", "pc_name": "Büro"}'
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_MQTT}, data=_info("steam/pc/buero/agent/info", payload)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "settings"
    assert _shown(result, SECTION_NOTIFY)[CONF_NOTIFY_SERVICE] == "mobile_app_phone"
    hass.config_entries.flow.async_abort(result["flow_id"])

    payload = '{"computer": "KIDS-1", "pc_id": "kinderpc", "pc_name": "Kinder-PC"}'
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_MQTT}, data=_info("steam/pc/kinderpc/agent/info", payload)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
