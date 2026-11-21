"""Constants for the Steam Engine integration."""

from __future__ import annotations

from datetime import timedelta

from .pc import UNNAMED

DOMAIN = "steam_engine"

# Entity IDs this integration creates its entities with (of the unnamed PC, pc.py). An entity that
# already exists keeps the entity ID it has in the registry (e.g. renamed by the user), so the logic
# looks its own entities up by unique ID (own_entities.py). The agent's entities are found by unique
# ID too (agent_entities.py).
ENTITY_PC = UNNAMED.own_entity_id("switch", "pc")
ENTITY_PC_STATE = UNNAMED.own_entity_id("sensor", "pc_state")
ENTITY_AUTO_OFF_ENABLED = UNNAMED.own_entity_id("switch", "auto_off")
ENTITY_WOKEN_BY_AUTOMATION = UNNAMED.own_entity_id("switch", "woken_by_automation")
ENTITY_AUTO_INSTALL_PURCHASES = UNNAMED.own_entity_id("switch", "install_new_purchases")
SAFE_SENSOR_ENTITY_ID = UNNAMED.own_entity_id("binary_sensor", "safe_to_shut_down")
ENTITY_STEAM_BUSY = UNNAMED.own_entity_id("binary_sensor", "steam_busy")
ENTITY_PLUGIN_BUSY = UNNAMED.own_entity_id("binary_sensor", "busy_by_plugin")
UPDATES_SENSOR_ENTITY_ID = UNNAMED.own_entity_id("sensor", "updates")
LAST_ANSWER_SENSOR_ENTITY_ID = UNNAMED.own_entity_id("sensor", "last_answer")
QUERY_SENSOR_ENTITY_ID = UNNAMED.own_entity_id("sensor", "query")
NOT_INSTALLED_SENSOR_ENTITY_ID = UNNAMED.own_entity_id("sensor", "not_installed")
ENTITY_DOWNLOAD_PROGRESS = UNNAMED.own_entity_id("sensor", "download_progress")
ENTITY_CURRENT_GAME = UNNAMED.own_entity_id("sensor", "current_game")
ENTITY_GAME_TO_INSTALL = UNNAMED.own_entity_id("select", "game_to_install")
ENTITY_INSTALLED_GAME = UNNAMED.own_entity_id("select", "installed_game")
ENTITY_INSTALL_GAME = UNNAMED.own_entity_id("button", "install_game")
ENTITY_UNINSTALL_GAME = UNNAMED.own_entity_id("button", "uninstall_game")
ENTITY_LAUNCH_GAME = UNNAMED.own_entity_id("button", "launch_game")
ENTITY_AUTO_UPDATE_PICKED = UNNAMED.own_entity_id("switch", "auto_update_picked_game")
# Keys of the entities: unique ID "<entry ID>_<key>" (unique_id()) and translation key
KEY_PC = "pc"
KEY_PC_STATE = "pc_state"
KEY_AUTO_OFF_ENABLED = "auto_off_enabled"
KEY_WOKEN_BY_AUTOMATION = "woken_by_automation"
KEY_AUTO_INSTALL_PURCHASES = "auto_install_purchases"
KEY_AUTO_UPDATE_PICKED = "auto_update_picked_game"
KEY_SAFE_TO_SHUT_DOWN = "safe_to_shut_down"
KEY_STEAM_BUSY = "steam_busy"
KEY_PLUGIN_BUSY = "plugin_busy"
KEY_UPDATES = "updates"
KEY_LAST_ANSWER = "last_answer"
KEY_QUERY = "query"
KEY_NOT_INSTALLED = "not_installed"
KEY_DOWNLOAD_PROGRESS = "download_progress"
KEY_CURRENT_GAME = "current_game"
KEY_GAME_TO_INSTALL = "game_to_install"
KEY_INSTALLED_GAME = "installed_game"
KEY_INSTALL_GAME = "install_game"
KEY_UNINSTALL_GAME = "uninstall_game"
KEY_LAUNCH_GAME = "launch_game"


def unique_id(entry_id: str, key: str) -> str:
    return f"{entry_id}_{key}"


# The agent's MQTT device "Steam PC" (identifiers in steam-agent.ps1), parent of the device "Steam Engine"
AGENT_DEVICE = UNNAMED.agent_device

# Label that plugins give their entities while they need the PC: "PC busy" (label ID pc_busy)
LABEL_PC_BUSY = UNNAMED.busy_label
# Buttons of the PC pushes (mobile_app_notification_action)
ACTION_PC_SHUTDOWN = UNNAMED.action("STEAM_ENGINE_PC_SHUTDOWN")
ACTION_AUTO_OFF_ON = UNNAMED.action("STEAM_ENGINE_AUTO_OFF_ON")

# Steam jobs and the integration's store
STORAGE_KEY = UNNAMED.store_key
STORAGE_VERSION = 1
AGENT_COMMAND_TOPIC = UNNAMED.agent_command_topic
UNINSTALL_RESULT_TOPIC = UNNAMED.uninstall_result_topic
# Progress and result of moving a game to another drive (move.ps1; end states retained, steam_move.py)
MOVE_TOPIC = UNNAMED.move_topic
# Installed depots per game with their manifest IDs (report.ps1, retained): {"<appid>": {"<depot>": "<id>"}}.
# Not in the library sensor, so its attributes stay small for the recorder.
DEPOTS_TOPIC = UNNAMED.depots_topic
# The installed games (report.ps1, retained); the agent's library sensor reads it
LIBRARY_TOPIC = UNNAMED.library_topic
# Note for the lock script on the PC (retained, QoS 1): a game start from Home Assistant woke the PC,
# so the lock at logon is left out once. {"appid": "<appid>", "until": "<UTC ISO 8601>"}; empty clears.
LAUNCH_PENDING_TOPIC = UNNAMED.launch_pending_topic
# How long the note is valid (its "until"); the integration clears it after this time at the latest
LAUNCH_PENDING_VALID = timedelta(minutes=10)
# A second uninstall of the same game within this time is ignored (the list in the dashboard lags behind);
# the same for a second move of a game
UNINSTALL_GUARD = timedelta(minutes=2)
# A Steam job without progress this long no longer keeps the PC on (and the stall push says so)
BUSY_STALL_LIMIT = timedelta(hours=6)

# Options. Sections group the settings page; values inside are stored per section.
SECTION_NOTIFY = "notify"
CONF_NOTIFY_SERVICE = "notify_service"  # empty: no pushes at all
CONF_LINK = "link"
CONF_PUSH_KINDS = "push_kinds"  # the kinds of pushes that are sent (push.py)
CONF_QUIET_AUTO_UPDATES = "quiet_auto_updates"  # jobs of automatic updates only push on a failure
# The value of "no pushes" in the form (stored as an empty service)
NOTIFY_NONE = "none"
# Kinds of pushes, in the order of the settings page. Pushes that answer a button of a push are sent
# whatever is chosen.
PUSH_PROGRESS = "progress"  # a job started, its progress (also the start of a move)
PUSH_RESULT = "result"  # the end of a job (also of a move)
PUSH_UPDATES = "updates"  # new updates found
PUSH_PURCHASES = "purchases"  # new purchases
PUSH_UNINSTALL = "uninstall"  # result of an uninstall
PUSH_IDLE = "idle"  # idle PC: shut it down?, and the automatic shut down
PUSH_REMINDER = "reminder"  # auto off has been switched off for long
PUSH_PROBLEMS = "problems"  # not enough space, stalled or aborted job, silent or unreachable agent, shut down refused
PUSH_KINDS = (
    PUSH_PROGRESS,
    PUSH_RESULT,
    PUSH_UPDATES,
    PUSH_PURCHASES,
    PUSH_UNINSTALL,
    PUSH_IDLE,
    PUSH_REMINDER,
    PUSH_PROBLEMS,
)

SECTION_AUTO_OFF = "auto_off"
CONF_IDLE_MINUTES = "idle_minutes"
CONF_QUIET_MINUTES = "quiet_minutes"
CONF_MIN_AFTER_WAKE_MINUTES = "min_after_wake_minutes"
CONF_NETWORK_LIMIT_KBS = "network_limit_kbs"

SECTION_IDLE_PROMPT = "idle_prompt"
CONF_PROMPT_AFTER_MINUTES = "prompt_after_minutes"
CONF_AFTER_DOWNLOAD_MINUTES = "after_download_minutes"
CONF_AFTER_GAME_MINUTES = "after_game_minutes"

SECTION_REMINDER = "reminder"
CONF_REMINDER_AFTER_HOURS = "after_hours"
CONF_REMINDER_EVERY_HOURS = "every_hours"

SECTION_PC = "pc"
CONF_REACHABLE_ENTITY = "reachable_entity"
CONF_WAKE_ENTITY = "wake_entity"
# Start a game on a switched-off PC without the lock at logon (security trade-off, off by default)
CONF_LAUNCH_UNLOCKED = "launch_unlocked"
# Entity domains that can wake the PC, and how they are triggered
WAKE_ACTIONS = {"button": "press", "input_button": "press", "switch": "turn_on", "script": "turn_on"}

SECTION_STEAM = "steam"
CONF_CHECK_EVERY_HOURS = "check_every_hours"
CONF_STEAM_LINK = "link"
CONF_STALLED_MINUTES = "stalled_minutes"
CONF_API_KEY = "api_key"
CONF_STEAM_ID = "steam_id"
# Overflow drive (steam_jobs.py): a lower-case drive letter ("e"), "" for none. A new purchase
# installed by itself (or by the button of the purchase push) that finds no space on the default
# drive, or finds it not there, goes there once instead. NOTIFY_NONE stands for "" in the form.
CONF_OVERFLOW_DRIVE = "overflow_drive"

# Active hours (active_hours.py): automatic work starts only inside this window of the day.
# Times as HH:MM:SS (what the time selector stores); start == end means all day.
SECTION_ACTIVE_HOURS = "active_hours"
CONF_ACTIVE_ENABLED = "enabled"
CONF_ACTIVE_START = "start"
CONF_ACTIVE_END = "end"

DEFAULT_OPTIONS: dict[str, dict] = {
    SECTION_PC: {
        CONF_REACHABLE_ENTITY: "",
        CONF_WAKE_ENTITY: "",
        CONF_LAUNCH_UNLOCKED: False,
    },
    SECTION_NOTIFY: {
        CONF_NOTIFY_SERVICE: "",
        CONF_LINK: "",
        CONF_PUSH_KINDS: list(PUSH_KINDS),
        CONF_QUIET_AUTO_UPDATES: False,
    },
    SECTION_AUTO_OFF: {
        CONF_IDLE_MINUTES: 30,
        CONF_QUIET_MINUTES: 15,
        CONF_MIN_AFTER_WAKE_MINUTES: 30,
        CONF_NETWORK_LIMIT_KBS: 100,
    },
    SECTION_IDLE_PROMPT: {
        CONF_PROMPT_AFTER_MINUTES: 60,
        CONF_AFTER_DOWNLOAD_MINUTES: 30,
        CONF_AFTER_GAME_MINUTES: 30,
    },
    SECTION_REMINDER: {
        CONF_REMINDER_AFTER_HOURS: 10,
        CONF_REMINDER_EVERY_HOURS: 3,
    },
    SECTION_STEAM: {
        CONF_CHECK_EVERY_HOURS: 1,
        CONF_STEAM_LINK: "",
        CONF_STALLED_MINUTES: 20,
        CONF_API_KEY: "",
        CONF_STEAM_ID: "",
        CONF_OVERFLOW_DRIVE: "",
    },
    SECTION_ACTIVE_HOURS: {
        CONF_ACTIVE_ENABLED: False,
        CONF_ACTIVE_START: "08:00:00",
        CONF_ACTIVE_END: "20:00:00",
    },
}


def merged_options(options: dict) -> dict[str, dict]:
    """Return the stored options on top of the defaults, section by section."""
    return {
        section: {**defaults, **(options.get(section) or {})}
        for section, defaults in DEFAULT_OPTIONS.items()
    }
