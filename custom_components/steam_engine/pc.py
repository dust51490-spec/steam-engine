"""The names of one PC: MQTT topics, the agent's unique IDs, the integration's own entity IDs, push
tags and actions, store, label and device name.

The PC without a name (the first and usual one) has frozen names; tests pin them (tests/test_pc.py),
because automations, dashboards, pushes on phones and the history hang on them. A named PC (several
PCs on one Home Assistant) gets names of its own, the same as the PC side makes them (Get-SteamEngineNames in mqtt_publish.ps1, see PROTOCOL.md):

- topics steam/pc/<id>/... instead of steam/...
- agent unique IDs and entity IDs steam_pc_<id>_<key> (steam_pc_<key> without a name)
- own entity IDs <domain>.steam_engine_<id>_<key>, device "Steam Engine <name>"
- push tags <tag>_<id>, push actions <action>@<id>, store steam_engine.lists_<id>, label pc_busy_<id>
- push titles "<name>: ..." (only while there is more than one PC)
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from homeassistant.config_entries import ConfigEntry, ConfigEntryState

# The agent's entities the integration reads: field -> (domain, key). Unique ID steam_pc_<key>, entity
# ID <domain>.steam_pc_<key> (steam_pc_<id>_<key> for a named PC), as the PC side makes them.
AGENT_ENTITIES: dict[str, tuple[str, str]] = {
    "library": ("sensor", "library"),
    "install_status": ("sensor", "install_status"),
    "history": ("sensor", "history"),
    "agent_online": ("binary_sensor", "agent_online"),
    "steam_downloading": ("binary_sensor", "steam_downloading"),
    "steam_running": ("binary_sensor", "steam_running"),
    "game_running": ("binary_sensor", "game_running"),
    "last_input": ("sensor", "last_input"),
    "network": ("sensor", "network"),
    "shutdown_button": ("button", "shutdown"),
    "restart_button": ("button", "restart"),
}

# Agent entities only the dashboard card reads (websocket.py), same form as AGENT_ENTITIES
CARD_AGENT_ENTITIES: dict[str, tuple[str, str]] = {
    "library_free": ("sensor", "library_free"),
    "lock_button": ("button", "lock"),
    "close_games_button": ("button", "close_games"),
}

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def pc_id_of(name: str) -> str:
    """The ID of a PC name as the PC side makes it: lower case, umlauts as two letters, a-z and 0-9
    only, at most 16 characters ("Kinder-PC" -> "kinderpc"); "" for no name. ValueError for a name
    without letters or digits."""
    if not name or not name.strip():
        return ""
    pc_id = re.sub(r"[^a-z0-9]", "", name.strip().lower().translate(_UMLAUTS))[:16]
    if not pc_id:
        raise ValueError(f"The PC name {name!r} has no letters or digits to make an ID from")
    return pc_id


@dataclass(frozen=True)
class PcNames:
    """All names of one PC; pc_id "" is the unnamed PC."""

    pc_id: str = ""
    pc_name: str = ""

    @property
    def named(self) -> bool:
        return bool(self.pc_id)

    # --- MQTT (the PC side's topics) -------------------------------------------------------

    def topic(self, path: str) -> str:
        """steam/<path>, or steam/pc/<id>/<path> for a named PC (path e.g. "agent/command")."""
        return f"steam/pc/{self.pc_id}/{path}" if self.named else f"steam/{path}"

    @property
    def agent_command_topic(self) -> str:
        return self.topic("agent/command")

    @property
    def uninstall_result_topic(self) -> str:
        return self.topic("uninstall")

    @property
    def move_topic(self) -> str:
        return self.topic("move")

    @property
    def depots_topic(self) -> str:
        return self.topic("depots")

    @property
    def library_topic(self) -> str:
        return self.topic("library")

    @property
    def launch_pending_topic(self) -> str:
        return self.topic("launch_pending")

    # --- the agent's entities and device ---------------------------------------------------

    def agent_entity(self, field: str) -> tuple[str, str]:
        """(unique ID, default entity ID) of one of the agent's entities (AGENT_ENTITIES)."""
        domain, key = AGENT_ENTITIES.get(field) or CARD_AGENT_ENTITIES[field]
        prefix = f"steam_pc_{self.pc_id}_" if self.named else "steam_pc_"
        return prefix + key, f"{domain}.{prefix}{key}"

    @property
    def agent_device(self) -> tuple[str, str]:
        """Identifier of the agent's MQTT device (identifiers in mqtt_publish.ps1)."""
        return ("mqtt", f"steam_pc_{self.pc_id}" if self.named else "steam_pc")

    # --- the integration's own entities and device -----------------------------------------

    def own_entity_id(self, domain: str, key: str) -> str:
        """<domain>.steam_engine_<key>, or steam_engine_<id>_<key> for a named PC."""
        return f"{domain}.steam_engine_{self.pc_id}_{key}" if self.named else f"{domain}.steam_engine_{key}"

    def own(self, entity_id: str) -> str:
        """This PC's entity ID for one of the unnamed PC's (the ENTITY_* constants)."""
        if not self.named:
            return entity_id
        domain, object_id = entity_id.split(".", 1)
        return f"{domain}.{object_id.replace('steam_engine_', f'steam_engine_{self.pc_id}_', 1)}"

    @property
    def device_name(self) -> str:
        return f"Steam Engine {self.pc_name}" if self.named else "Steam Engine"

    # --- pushes, store, label --------------------------------------------------------------

    def tag(self, tag: str) -> str:
        """A push tag (push.py): a named PC's pushes must not replace the unnamed PC's."""
        return f"{tag}_{self.pc_id}" if self.named else tag

    def action(self, action: str) -> str:
        """A push action (button of a push): the PC it belongs to after an @."""
        return f"{action}@{self.pc_id}" if self.named else action

    def title(self, title: str) -> str:
        """A push title with the PC's name before it (push.py adds it only while there is more
        than one PC)."""
        return f"{self.pc_name}: {title}" if self.named else title

    def action_of(self, received: str) -> str | None:
        """The action of a push button pressed (mobile_app_notification_action) if it is one of
        this PC's, else None: the unnamed PC's have no @, a named PC's end in @<id>."""
        action, at, pc_id = received.rpartition("@")
        if not at:
            return None if self.named else received
        return action if self.named and pc_id == self.pc_id else None

    def issue_id(self, issue: str) -> str:
        """The ID of a hint under Repairs (its translation stays the one of issue)."""
        return f"{issue}_{self.pc_id}" if self.named else issue

    @property
    def store_key(self) -> str:
        """The job lists of steam_jobs.py (.storage)."""
        return f"steam_engine.lists_{self.pc_id}" if self.named else "steam_engine.lists"

    @property
    def images_store_key(self) -> str:
        """The image URLs of steam_images.py (.storage)."""
        return f"steam_engine.images_{self.pc_id}" if self.named else "steam_engine.images"

    @property
    def busy_label(self) -> str:
        """Label that plugins give their entities while they need this PC ("PC busy")."""
        return f"pc_busy_{self.pc_id}" if self.named else "pc_busy"

    @property
    def busy_labels(self) -> tuple[str, ...]:
        """The labels that keep this PC busy: its own, and for a named PC also "PC busy" without a
        PC (a plugin that needs every PC)."""
        return (self.busy_label, "pc_busy") if self.named else (self.busy_label,)


# Key in a config entry's data: the PC's name as set on the PC ("" or missing = the unnamed PC)
CONF_PC_NAME = "pc_name"
# The optional field of the actions that picks the PC
ATTR_PC = "pc"


def pc_names(pc_name: str = "") -> PcNames:
    """The names of the PC called pc_name ("" = the unnamed PC)."""
    pc_id = pc_id_of(pc_name)
    return PcNames(pc_id, pc_name.strip() if pc_id else "")


# The PC without a name
UNNAMED = pc_names()
# Its name where one is shown (its device is "Steam PC"; the actions take it as "pc" too)
UNNAMED_PC = "Steam PC"
# The unique ID of its config entry when it was found by MQTT discovery
UNNAMED_UNIQUE_ID = "steampc"


def entry_pc_names(data: dict) -> PcNames:
    """The names of the PC of a config entry (its data)."""
    return pc_names(str(data.get(CONF_PC_NAME) or ""))


def pick_entry[E: ConfigEntry](entries: list[E], pc: str | None) -> tuple[E | None, str]:
    """The entry of the PC asked for by name; without a name the only one, else the one of the
    unnamed PC. entries: every PC set up, loaded or not (in the order they were set up), so the
    pick never falls to another PC while one is not loaded (reloading, disabled, failed to set
    up). (None, "not_loaded") when the PC picked is not loaded or none is set up, (None,
    "unknown_pc" / "pc_needed") when there is none."""
    if not entries:
        return None, "not_loaded"
    if pc and pc.strip():
        entry, problem = next((e for e in entries if matches(entry_pc_names(e.data), pc)), None), "unknown_pc"
    elif len(entries) == 1:
        entry, problem = entries[0], ""
    else:
        entry, problem = next((e for e in entries if not entry_pc_names(e.data).named), None), "pc_needed"
    if entry is None:
        return None, problem
    return (entry, "") if entry.state is ConfigEntryState.LOADED else (None, "not_loaded")


def matches(names: PcNames, pc: str) -> bool:
    """Whether the PC asked for by name (the action's field "pc") is this one. The unnamed PC is
    also "Steam PC", the name of its device."""
    try:
        wanted = pc_id_of(pc)
    except ValueError:
        return False
    return wanted == names.pc_id or (not names.named and wanted == "steampc")
