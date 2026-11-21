"""Config and options flow for Steam Engine: one settings page for everything."""

from __future__ import annotations

import json
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)

from .const import (
    CONF_ACTIVE_ENABLED,
    CONF_ACTIVE_END,
    CONF_ACTIVE_START,
    CONF_AFTER_DOWNLOAD_MINUTES,
    CONF_API_KEY,
    CONF_AFTER_GAME_MINUTES,
    CONF_CHECK_EVERY_HOURS,
    CONF_IDLE_MINUTES,
    CONF_LAUNCH_UNLOCKED,
    CONF_LINK,
    CONF_MIN_AFTER_WAKE_MINUTES,
    CONF_NETWORK_LIMIT_KBS,
    CONF_NOTIFY_SERVICE,
    CONF_OVERFLOW_DRIVE,
    CONF_PROMPT_AFTER_MINUTES,
    CONF_PUSH_KINDS,
    CONF_QUIET_AUTO_UPDATES,
    CONF_QUIET_MINUTES,
    CONF_REMINDER_AFTER_HOURS,
    CONF_REMINDER_EVERY_HOURS,
    CONF_STALLED_MINUTES,
    CONF_STEAM_ID,
    CONF_STEAM_LINK,
    CONF_REACHABLE_ENTITY,
    CONF_WAKE_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
    NOTIFY_NONE,
    PUSH_KINDS,
    SECTION_ACTIVE_HOURS,
    SECTION_AUTO_OFF,
    SECTION_IDLE_PROMPT,
    SECTION_NOTIFY,
    SECTION_PC,
    SECTION_REMINDER,
    SECTION_STEAM,
    WAKE_ACTIONS,
    merged_options,
)
from . import protocol
from .pc import CONF_PC_NAME, UNNAMED, UNNAMED_PC, UNNAMED_UNIQUE_ID, PcNames, entry_pc_names, matches, pc_id_of, pc_names
from .space import drive_letter, drives_of, kinds_of
from .steam_account import async_check_account
from .texts import Texts


def _number(minimum: int, maximum: int, unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum, max=maximum, step=1, unit_of_measurement=unit, mode=NumberSelectorMode.BOX
        )
    )


def _notify_services(hass: HomeAssistant, current: str) -> list[str]:
    """"No pushes", the mobile app notify services, and the current value if it is something else."""
    services = sorted(s for s in hass.services.async_services_for_domain("notify") if s.startswith("mobile_app_"))
    if current and current not in services:
        services.insert(0, current)
    return [NOTIFY_NONE, *services]


def _overflow_drives(hass: HomeAssistant, names: PcNames, current: str) -> list[dict[str, str]]:
    """"None" and the drives the PC reports, with what is known of them ("E: · HDD · 812 GB free",
    "G: · SSD · USB · 230 GB free · new library", the default library's drive marked as without
    effect), and the stored drive also when it is not
    reported now. Values: lower-case letters as stored (NOTIFY_NONE for none, its label is
    translated)."""
    tx = Texts(hass)
    found = [{"value": NOTIFY_NONE, "label": NOTIFY_NONE}]
    for drive in drives_of(hass, names):
        parts = [drive["drive"], *kinds_of(drive)]
        try:
            parts.append(tx("drive_option_free", free=int(float(drive.get("free_gb")))))
        except (TypeError, ValueError):
            pass  # free space not known
        if not drive.get("library"):
            parts.append(tx("drive_option_new"))
        if drive.get("default"):
            parts.append(tx("drive_option_default"))  # steam_jobs.py never falls back to the star
        found.append({"value": drive["drive"][0].lower(), "label": " · ".join(parts)})
    if protocol.valid_drive(current) and current not in [d["value"] for d in found]:
        found.append({"value": current, "label": f"{drive_letter(current)} · {tx('drive_option_missing')}"})
    return found


def _entity(key: str, current: str, domains: list[str]) -> tuple[vol.Optional, EntitySelector]:
    """Optional entity field; an empty value is shown as empty, not as an invalid default."""
    marker = vol.Optional(key, description={"suggested_value": current}) if current else vol.Optional(key)
    return marker, EntitySelector(EntitySelectorConfig(domain=domains))


def _text(key: str, current: str, password: bool = False) -> tuple[vol.Optional, TextSelector]:
    """Optional text field. Shown with its value but without a default: a field the user empties
    is left out of the form data and saved as empty (a default would put the old value back)."""
    marker = vol.Optional(key, description={"suggested_value": current}) if current else vol.Optional(key)
    config = TextSelectorConfig(type=TextSelectorType.PASSWORD) if password else TextSelectorConfig()
    return marker, TextSelector(config)


def _schema(hass: HomeAssistant, opts: dict[str, dict], names: PcNames = UNNAMED) -> vol.Schema:
    pc = opts[SECTION_PC]
    notify = opts[SECTION_NOTIFY]
    auto_off = opts[SECTION_AUTO_OFF]
    prompt = opts[SECTION_IDLE_PROMPT]
    reminder = opts[SECTION_REMINDER]
    steam = opts[SECTION_STEAM]
    active = opts[SECTION_ACTIVE_HOURS]
    minutes = "min"
    return vol.Schema(
        {
            vol.Required(SECTION_PC): section(
                vol.Schema(
                    {
                        **dict(
                            [
                                _entity(CONF_REACHABLE_ENTITY, pc[CONF_REACHABLE_ENTITY], ["binary_sensor"]),
                                _entity(CONF_WAKE_ENTITY, pc[CONF_WAKE_ENTITY], sorted(WAKE_ACTIONS)),
                            ]
                        ),
                        vol.Required(
                            CONF_LAUNCH_UNLOCKED, default=bool(pc[CONF_LAUNCH_UNLOCKED])
                        ): BooleanSelector(),
                    }
                ),
                {"collapsed": False},
            ),
            vol.Required(SECTION_NOTIFY): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_NOTIFY_SERVICE, default=notify[CONF_NOTIFY_SERVICE] or NOTIFY_NONE
                        ): SelectSelector(
                            SelectSelectorConfig(
                                options=_notify_services(hass, notify[CONF_NOTIFY_SERVICE]),
                                mode=SelectSelectorMode.DROPDOWN,
                                custom_value=True,
                                translation_key=CONF_NOTIFY_SERVICE,
                            )
                        ),
                        vol.Required(
                            CONF_PUSH_KINDS, default=[k for k in PUSH_KINDS if k in notify[CONF_PUSH_KINDS]]
                        ): SelectSelector(
                            SelectSelectorConfig(
                                options=list(PUSH_KINDS),
                                multiple=True,
                                mode=SelectSelectorMode.LIST,
                                translation_key=CONF_PUSH_KINDS,
                            )
                        ),
                        vol.Required(
                            CONF_QUIET_AUTO_UPDATES, default=bool(notify[CONF_QUIET_AUTO_UPDATES])
                        ): BooleanSelector(),
                        **dict([_text(CONF_LINK, notify[CONF_LINK])]),
                    }
                ),
                {"collapsed": False},
            ),
            vol.Required(SECTION_AUTO_OFF): section(
                vol.Schema(
                    {
                        vol.Required(CONF_IDLE_MINUTES, default=auto_off[CONF_IDLE_MINUTES]): _number(1, 600, minutes),
                        vol.Required(CONF_QUIET_MINUTES, default=auto_off[CONF_QUIET_MINUTES]): _number(0, 600, minutes),
                        vol.Required(
                            CONF_MIN_AFTER_WAKE_MINUTES, default=auto_off[CONF_MIN_AFTER_WAKE_MINUTES]
                        ): _number(0, 600, minutes),
                        vol.Required(
                            CONF_NETWORK_LIMIT_KBS, default=auto_off[CONF_NETWORK_LIMIT_KBS]
                        ): _number(1, 100000, "kB/s"),
                    }
                ),
                {"collapsed": False},
            ),
            vol.Required(SECTION_IDLE_PROMPT): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_PROMPT_AFTER_MINUTES, default=prompt[CONF_PROMPT_AFTER_MINUTES]
                        ): _number(1, 1440, minutes),
                        vol.Required(
                            CONF_AFTER_DOWNLOAD_MINUTES, default=prompt[CONF_AFTER_DOWNLOAD_MINUTES]
                        ): _number(0, 600, minutes),
                        vol.Required(
                            CONF_AFTER_GAME_MINUTES, default=prompt[CONF_AFTER_GAME_MINUTES]
                        ): _number(0, 600, minutes),
                    }
                ),
                {"collapsed": False},
            ),
            vol.Required(SECTION_REMINDER): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_REMINDER_AFTER_HOURS, default=reminder[CONF_REMINDER_AFTER_HOURS]
                        ): _number(1, 168, "h"),
                        vol.Required(
                            CONF_REMINDER_EVERY_HOURS, default=reminder[CONF_REMINDER_EVERY_HOURS]
                        ): _number(1, 24, "h"),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required(SECTION_STEAM): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_CHECK_EVERY_HOURS, default=steam[CONF_CHECK_EVERY_HOURS]
                        ): _number(1, 24, "h"),
                        vol.Required(
                            CONF_STALLED_MINUTES, default=steam[CONF_STALLED_MINUTES]
                        ): _number(5, 180, minutes),
                        **dict(
                            [
                                _text(CONF_STEAM_LINK, steam[CONF_STEAM_LINK]),
                                _text(CONF_API_KEY, steam[CONF_API_KEY], password=True),
                                _text(CONF_STEAM_ID, steam[CONF_STEAM_ID]),
                            ]
                        ),
                        vol.Required(
                            CONF_OVERFLOW_DRIVE, default=steam[CONF_OVERFLOW_DRIVE] or NOTIFY_NONE
                        ): SelectSelector(
                            SelectSelectorConfig(
                                options=_overflow_drives(hass, names, steam[CONF_OVERFLOW_DRIVE]),
                                mode=SelectSelectorMode.DROPDOWN,
                                translation_key=CONF_OVERFLOW_DRIVE,
                            )
                        ),
                    }
                ),
                {"collapsed": False},
            ),
            vol.Required(SECTION_ACTIVE_HOURS): section(
                vol.Schema(
                    {
                        vol.Required(CONF_ACTIVE_ENABLED, default=active[CONF_ACTIVE_ENABLED]): BooleanSelector(),
                        vol.Required(CONF_ACTIVE_START, default=active[CONF_ACTIVE_START]): TimeSelector(),
                        vol.Required(CONF_ACTIVE_END, default=active[CONF_ACTIVE_END]): TimeSelector(),
                    }
                ),
                {"collapsed": not active[CONF_ACTIVE_ENABLED]},
            ),
        }
    )


def _clean(user_input: dict[str, Any]) -> dict[str, dict]:
    """Number selectors return floats; store whole numbers."""
    result: dict[str, dict] = {}
    for sec, values in user_input.items():
        result[sec] = {
            key: int(value) if isinstance(value, float) else value for key, value in (values or {}).items()
        }
    return merged_options(result)


async def _checked(hass: HomeAssistant, user_input: dict[str, Any], before: dict[str, dict]) -> tuple[dict, dict]:
    """The cleaned options and the form errors. Key and Steam ID are checked with Steam when they
    changed; a profile URL or name is stored as the SteamID64."""
    options = _clean(user_input)
    # The service name without "notify." (the help text shows it with it, push.py calls notify.<name>);
    # a changed one must exist, or every push would be dropped
    notify = options[SECTION_NOTIFY]
    service = (notify.get(CONF_NOTIFY_SERVICE) or "").strip().removeprefix("notify.")
    if service == NOTIFY_NONE:
        service = ""  # no pushes
    notify[CONF_NOTIFY_SERVICE] = service
    # Only known kinds, in their order
    notify[CONF_PUSH_KINDS] = [k for k in PUSH_KINDS if k in (notify.get(CONF_PUSH_KINDS) or [])]
    if (
        service
        and service != before[SECTION_NOTIFY].get(CONF_NOTIFY_SERVICE)
        and not hass.services.has_service("notify", service)
    ):
        return options, {"base": "notify_not_found"}
    steam = options[SECTION_STEAM]
    # A drive letter as stored ("e"), "" for none (NOTIFY_NONE)
    overflow = str(steam.get(CONF_OVERFLOW_DRIVE) or "").strip().lower()
    steam[CONF_OVERFLOW_DRIVE] = overflow if protocol.valid_drive(overflow) else ""
    steam[CONF_API_KEY] = (steam.get(CONF_API_KEY) or "").strip()
    steam[CONF_STEAM_ID] = (steam.get(CONF_STEAM_ID) or "").strip()
    old = before[SECTION_STEAM]
    unchanged = ((old.get(CONF_API_KEY) or "").strip(), (old.get(CONF_STEAM_ID) or "").strip())
    if (steam[CONF_API_KEY], steam[CONF_STEAM_ID]) == unchanged:
        return options, {}
    steam[CONF_STEAM_ID], error = await async_check_account(hass, steam[CONF_API_KEY], steam[CONF_STEAM_ID])
    return options, ({"base": error} if error else {})


class SteamEngineConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Steam Engine, one entry per PC. The first PC has no name; every further one is asked
    for the name it was given in its setup (PcName in settings.psd1), then for its settings, filled
    in with those of the first PC except the PC's own fields. Settings live in the options
    (Configure)."""

    VERSION = 1

    def __init__(self) -> None:
        self._pc_name = ""
        self._computer = ""

    def _pcs(self) -> list[ConfigEntry]:
        """The PCs set up. A found PC the user ignored is an entry too, but none: its data is
        empty, so it would look like the PC without a name."""
        return self._async_current_entries(include_ignore=False)

    async def async_step_mqtt(self, discovery_info: MqttServiceInfo) -> ConfigFlowResult:
        """A PC's agent reported itself (retained steam/agent/info, steam/pc/<id>/agent/info for a
        named PC): Home Assistant shows it as found until it is set up or ignored."""
        try:
            info = json.loads(discovery_info.payload or "")
        except ValueError:
            info = None
        if not isinstance(info, dict):
            return self.async_abort(reason="not_supported")  # cleared by the uninstall
        name = str(info.get("pc_name") or "").strip()
        try:
            pc_id = pc_id_of(name)
        except ValueError:
            return self.async_abort(reason="not_supported")
        if discovery_info.topic != pc_names(name).topic("agent/info"):
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(pc_id or UNNAMED_UNIQUE_ID)
        self._abort_if_unique_id_configured()  # also when this PC was ignored
        if any(entry_pc_names(e.data).pc_id == pc_id for e in self._pcs()):
            return self.async_abort(reason="already_configured")  # e.g. a first PC set up by hand
        self._pc_name = name
        self._computer = str(info.get("computer") or "")
        self.context["title_placeholders"] = {"name": name or UNNAMED_PC}
        return await self.async_step_found()

    async def async_step_found(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm the found PC, then its settings."""
        if user_input is not None:
            if not self._pcs() and not self._pc_name:
                return await self.async_step_user()
            return await self.async_step_settings()
        return self.async_show_form(
            step_id="found",
            description_placeholders={"pc": self._pc_name or UNNAMED_PC, "computer": self._computer or "?"},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._pcs():
            return await self.async_step_pc()
        options, errors = merged_options(DEFAULT_OPTIONS), {}
        if user_input is not None:
            options, errors = await _checked(self.hass, user_input, merged_options(DEFAULT_OPTIONS))
            if not errors:
                # the first PC is the one without a name: a found one (MQTT) is then set up already
                await self.async_set_unique_id(UNNAMED_UNIQUE_ID, raise_on_progress=False)
                return self.async_create_entry(title="Steam Engine", data={}, options=options)
        return self.async_show_form(step_id="user", data_schema=_schema(self.hass, options), errors=errors)

    async def async_step_pc(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A further PC: its name, as set on that PC."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_PC_NAME) or "").strip()
            try:
                pc_id = pc_id_of(name)
            except ValueError:
                pc_id = ""
            taken = [e for e in self._pcs() if matches(entry_pc_names(e.data), name)]
            if not pc_id:
                errors["base"] = "pc_name_invalid"
            elif taken:
                errors["base"] = "pc_exists"
            else:
                await self.async_set_unique_id(pc_id)
                self._abort_if_unique_id_configured()
                self._pc_name = name
                return await self.async_step_settings()
        return self.async_show_form(
            step_id="pc", data_schema=vol.Schema({vol.Required(CONF_PC_NAME): TextSelector()}), errors=errors
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The settings of a further PC, filled in with those of the first one (if there is one)
        except the PC's own fields."""
        entries = self._pcs()
        before = merged_options(dict(entries[0].options) if entries else DEFAULT_OPTIONS)
        # Not taken over: PC reachable and Turn on with are the first PC's entities (this PC would
        # count as on while that one is), starting games without the lock is decided for each PC,
        # and the overflow drive is one of the first PC's drives
        before[SECTION_PC] = dict(DEFAULT_OPTIONS[SECTION_PC])
        before[SECTION_STEAM][CONF_OVERFLOW_DRIVE] = ""
        options, errors = before, {}
        if user_input is not None:
            options, errors = await _checked(self.hass, user_input, before)
            if not errors:
                return self.async_create_entry(
                    title=f"Steam Engine {self._pc_name}".strip(), data={CONF_PC_NAME: self._pc_name}, options=options
                )
        return self.async_show_form(
            step_id="settings",
            data_schema=_schema(self.hass, options, pc_names(self._pc_name)),
            errors=errors,
            description_placeholders={"pc": self._pc_name},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SteamEngineOptionsFlow()


class SteamEngineOptionsFlow(OptionsFlow):
    """The settings page."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options, errors = merged_options(dict(self.config_entry.options)), {}
        if user_input is not None:
            options, errors = await _checked(self.hass, user_input, options)
            if not errors:
                return self.async_create_entry(data=options)
        names = entry_pc_names(dict(self.config_entry.data))
        return self.async_show_form(step_id="init", data_schema=_schema(self.hass, options, names), errors=errors)
