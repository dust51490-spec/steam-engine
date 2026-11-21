"""Moving a game to another drive (steam_jobs.async_move, steam_move.py): the action and its checks
before anything goes to the PC, Steam busy from the command on, the progress and the result as the
PC reports them on steam/move (pushes, the event, the card's attributes), an end read again
(retained) that is not told twice, the end of the pushed move read after a restart, an end sent late
that does not answer a new move, a move that goes silent, the install queue and game starts that
wait for a move."""

from __future__ import annotations

import json
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_mqtt_message,
    async_mock_service,
)

from custom_components.steam_engine import MOVE_SCHEMA
from custom_components.steam_engine.const import DOMAIN, ENTITY_DOWNLOAD_PROGRESS, ENTITY_LAUNCH_GAME, ENTITY_STEAM_BUSY
from custom_components.steam_engine.game_choice import PICK_INSTALLED
from custom_components.steam_engine.steam_events import EVENT_MOVED
from custom_components.steam_engine.steam_jobs import QUEUE_DELAY
from custom_components.steam_engine.steam_move import MOVE_SILENT

from .test_jobs_power import (  # noqa: F401 (setup_entry is a fixture)
    AGENT_ONLINE,
    GAME_RUNNING,
    LIBRARY,
    NOTIFY,
    PHONE,
    STATUS,
    Setup,
    _installs,
    _pc_on,
    _settle,
    _store,
    setup_entry,
)
from .test_space import DRIVES, LIBRARY_FREE, _game

TAG = "steam_engine_move"
DOWNLOADING = "binary_sensor.steam_pc_steam_downloading"
TOPIC = "steam/move"
STARTED = "2026-11-11T18:00:00+00:00"
# Game 20 (60 GB) is on F: (SSD); E: (HDD) has a library and room
GAMES = [{**_game("20", 60), "name": "Old F", "buildid": "1"}, {**_game("10", 200, drive="E:"), "buildid": "1"}]


def _moves(calls: list[ServiceCall]) -> list[str]:
    return [str(c.data["payload"]) for c in calls if str(c.data.get("payload", "")).startswith("move:")]


def _pushes(calls: list[ServiceCall]) -> list[dict[str, Any]]:
    """The move pushes, without the clearing of one."""
    return [
        dict(c.data) for c in calls if c.data.get("message") != "clear_notification" and c.data.get("data", {}).get("tag") == TAG
    ]


def _message(state: str, started: str = STARTED, **fields: Any) -> dict[str, Any]:
    return {
        "state": state,
        "started": started,
        "appid": "20",
        "name": "Old F",
        "from": "F:",
        "drive": "E:",
        "updated": "2026-11-11T18:05:00+00:00",
        **fields,
    }


def _send(hass: HomeAssistant, state: str, retain: bool = False, **fields: Any) -> None:
    async_fire_mqtt_message(hass, TOPIC, json.dumps(_message(state, **fields)), retain=retain)


def _running(hass: HomeAssistant, phase: str, percent: int, **fields: Any) -> None:
    _send(hass, "running", phase=phase, percent=percent, copied_gb=round(60 * percent / 100, 1), total_gb=60.0, **fields)


async def _setup(hass: HomeAssistant, setup_entry: Setup) -> tuple[MockConfigEntry, list[ServiceCall], list[ServiceCall]]:
    """PC on with the library and the drives; returns the entry, the pushes and the MQTT commands."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _pc_on(hass)
    hass.states.async_set(LIBRARY, str(len(GAMES)), {"games": GAMES})
    hass.states.async_set(LIBRARY_FREE, "812", {"drives": DRIVES})
    entry = await setup_entry(NOTIFY)
    await _settle(hass)  # the subscription to steam/move
    publishes = async_mock_service(hass, "mqtt", "publish")
    return entry, pushes, publishes


def _cleared(calls: list[ServiceCall]) -> int:
    """How often the move push was cleared."""
    return sum(1 for c in calls if c.data == {"message": "clear_notification", "data": {"tag": TAG}})


def _busy(hass: HomeAssistant) -> bool:
    return hass.states.get(ENTITY_STEAM_BUSY).state == "on"


def _attrs(hass: HomeAssistant) -> dict[str, Any]:
    return dict(hass.states.get(ENTITY_DOWNLOAD_PROGRESS).attributes)


def test_move_schema() -> None:
    """The field "drive" is required: one letter with or without the colon, sent in lower case."""
    for value, letter in (("E", "e"), ("e:", "e"), (" F: ", "f")):
        assert MOVE_SCHEMA({"appid": "440", "drive": value}) == {"appid": "440", "drive": letter}
    for value in ("", "  ", "EF", "1", "E:\\", "E:\\SteamLibrary", "Ä"):
        with pytest.raises(vol.Invalid):
            MOVE_SCHEMA({"appid": "440", "drive": value})
    with pytest.raises(vol.Invalid):
        MOVE_SCHEMA({"appid": "440"})
    with pytest.raises(vol.Invalid):
        MOVE_SCHEMA({"appid": "440,570", "drive": "E"})


async def test_checks_before_sending(hass: HomeAssistant, setup_entry: Setup) -> None:
    """What HA knows already is answered without a command: a game not installed or on a drive not
    there, the drive it is on, a drive the PC does not report, a running game or install job."""
    entry, _pushes_, publishes = await _setup(hass, setup_entry)
    jobs = entry.runtime_data.jobs
    assert await jobs.async_move("99", "e") == {"sent": False, "reason": "not_installed"}
    assert await jobs.async_move("20", "f") == {"sent": False, "reason": "same_drive"}
    assert await jobs.async_move("20", "z") == {"sent": False, "reason": "drive_unavailable"}
    assert await jobs.async_move("20", "1") == {"sent": False, "reason": "invalid_drive"}
    assert await jobs.async_move("x", "e") == {"sent": False, "reason": "invalid_appid"}
    hass.states.async_set(LIBRARY, "1", {"games": [{**GAMES[0], "unavailable": True}]})
    assert await jobs.async_move("20", "e") == {"sent": False, "reason": "drive_unavailable"}
    hass.states.async_set(LIBRARY, str(len(GAMES)), {"games": GAMES})

    hass.states.async_set(GAME_RUNNING, "on", {"appid": "10"})
    assert await jobs.async_move("20", "e") == {"sent": False, "reason": "game_running"}
    hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
    hass.states.async_set(STATUS, "started", {"appids": ["30"], "count": 1})
    await _settle(hass)
    assert await jobs.async_move("20", "e") == {"sent": False, "reason": "job_running"}
    assert _moves(publishes) == []
    assert not entry.runtime_data.move.active


# The move messages come over MQTT; its client keeps a periodic timer past the time jumps
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_move_progress_and_result(hass: HomeAssistant, setup_entry: Setup, mqtt_mock: Any) -> None:
    """Busy from the command on; a push at the start, on each 10 % step and each phase; the result
    with the event; the same end read again later (retained) is not told twice."""
    _entry, pushes, publishes = await _setup(hass, setup_entry)
    events = async_capture_events(hass, EVENT_MOVED)
    response = await hass.services.async_call(
        DOMAIN, "move", {"appid": "20", "drive": "E:"}, blocking=True, return_response=True
    )
    assert response == {"sent": True}
    assert _moves(publishes) == ["move:20@e"]
    await _settle(hass)
    assert _busy(hass)
    assert _attrs(hass)["move"]["phase"] == "requested"
    # Asked again while it is on its way: one move at a time
    response = await hass.services.async_call(
        DOMAIN, "move", {"appid": "20", "drive": "E"}, blocking=True, return_response=True
    )
    assert response == {"sent": False, "reason": "job_running"}

    _running(hass, "copy", 4)
    await _settle(hass)
    [start] = _pushes(pushes)
    assert start["title"] == "Steam: moving Old F"
    assert start["message"] == "From F: (SSD) to E: (HDD) (60 GB). Steam closes for a moment near the end."
    assert start["data"]["notification_icon"] == "mdi:folder-move-outline"
    move = _attrs(hass)["move"]
    assert (move["appid"], move["phase"], move["percent"], move["from"], move["drive"]) == ("20", "copy", 4, "F:", "E:")

    _running(hass, "copy", 12)
    _running(hass, "copy", 15)
    _running(hass, "sync", 99)
    _running(hass, "switch", 100)
    await _settle(hass)
    assert [p["message"] for p in _pushes(pushes)[1:]] == [
        "Copying – 12 %",
        "Steam closed, copying the rest – 99 %",
        "Steam closed, switching over – 100 %",
    ]
    assert _busy(hass)

    _send(hass, "ok", library="E:\\SteamLibrary", new_library=False, source_free_gb=102, target_free_gb=752, warning=None, reason=None)
    await _settle(hass)
    result = _pushes(pushes)[-1]
    assert result["title"] == "Steam: moved"
    assert result["message"] == "Old F is now on E: (HDD). Free: E: 752 GB, F: 102 GB."
    assert not _busy(hass)
    assert _attrs(hass)["move"] is None
    assert _attrs(hass)["move_result"]["result"] == "ok"
    [event] = events
    assert event.data == {
        "appid": "20",
        "name": "Old F",
        "success": True,
        "reason": None,
        "warning": None,
        "from": "F:",
        "drive": "E:",
        "new_library": False,
        "source_free_gb": 102,
        "target_free_gb": 752,
        "pc": "",
    }

    # The same end again (retained, after a reconnect): nothing new
    count = len(_pushes(pushes))
    _send(hass, "ok", retain=True, new_library=False, source_free_gb=102, target_free_gb=752)
    await _settle(hass)
    assert len(_pushes(pushes)) == count
    assert len(events) == 1


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_warning_and_failures(hass: HomeAssistant, setup_entry: Setup, mqtt_mock: Any) -> None:
    """A source folder left over, a new library, no space with the numbers, a move that could not be
    taken back."""
    _entry, pushes, _publishes = await _setup(hass, setup_entry)
    _send(hass, "ok", new_library=True, warning="source_not_deleted", started="2026-11-11T18:00:01+00:00")
    await _settle(hass)
    assert _pushes(pushes)[-1]["message"] == (
        "Old F is now on E: (HDD). A new Steam library was set up there. The old folder on F: (SSD) could not be "
        "deleted completely; the rest is deleted at the next start of the agent."
    )

    _send(hass, "error", reason_code="no_space", needed_gb=66, target_free_gb=40, started="2026-11-11T18:00:02+00:00")
    await _settle(hass)
    failed = _pushes(pushes)[-1]
    assert failed["title"] == "Steam: move failed"
    assert failed["message"] == "Old F was not moved: not enough space (66 GB needed, 40 GB free on E: (HDD))."
    assert failed["data"]["notification_icon"] == "mdi:folder-alert-outline"

    _send(hass, "error", reason_code="drive_unavailable", started="2026-11-11T18:00:03+00:00")
    await _settle(hass)
    assert _pushes(pushes)[-1]["message"] == (
        "Old F was not moved: the drive of the game or the target drive is not available."
    )

    _send(hass, "error", reason_code="rollback_failed", started="2026-11-11T18:00:04+00:00")
    await _settle(hass)
    assert _pushes(pushes)[-1]["message"].startswith("Old F: the move failed and could not be taken back yet.")

    # A program from the game's folder counts as running; a move cut off and uninstalled meanwhile
    # leaves its old folder (warning), the push names it
    _send(hass, "error", reason_code="game_running", started="2026-11-11T18:00:05+00:00")
    await _settle(hass)
    assert _pushes(pushes)[-1]["message"] == "Old F was not moved: a game or a program from the game's folder is running."
    _send(hass, "error", reason_code="not_installed", warning="source_not_deleted", started="2026-11-11T18:00:06+00:00")
    await _settle(hass)
    assert _pushes(pushes)[-1]["message"] == (
        "Old F was not moved: not installed. Its old folder stays on F: (SSD); delete it by hand if it is not needed any more."
    )


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_retained_end_after_a_restart(hass: HomeAssistant, setup_entry: Setup, mqtt_mock: Any) -> None:
    """An end the subscription gets first (retained) is the last result for the card, nothing more."""
    _entry, pushes, _publishes = await _setup(hass, setup_entry)
    events = async_capture_events(hass, EVENT_MOVED)
    _send(hass, "error", retain=True, reason_code="copy_failed")
    await _settle(hass)
    assert _pushes(pushes) == []
    assert events == []
    assert _attrs(hass)["move_result"]["reason_code"] == "copy_failed"
    assert not _busy(hass)


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_installs_and_game_starts_wait_for_a_move(
    hass: HomeAssistant, setup_entry: Setup, mqtt_mock: Any
) -> None:
    """While a move runs an install waits in the queue (the PC would fail it after 10 minutes) and a
    game is not started (the button says so and keeps the pick); once the move ended the queue
    moves on."""
    entry, _pushes_, publishes = await _setup(hass, setup_entry)
    _running(hass, "copy", 30)
    await _settle(hass)
    response = await hass.services.async_call(DOMAIN, "install", {"appid": "30"}, blocking=True, return_response=True)
    assert response == {"queued": True, "sent": False}
    assert await entry.runtime_data.jobs.async_launch("10") == {"sent": False, "reason": "moving"}
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "launch", {"appid": "10"}, blocking=True)
    assert err.value.translation_key == "moving"
    entry.runtime_data.choice.set(PICK_INSTALLED, "10")
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: ENTITY_LAUNCH_GAME}, blocking=True)
    assert err.value.translation_key == "moving"
    assert entry.runtime_data.choice.get(PICK_INSTALLED) == "10"
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == []

    _send(hass, "ok", new_library=False)
    await _settle(hass)
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:30,"]


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_silent_move_counts_as_ended(hass: HomeAssistant, setup_entry: Setup, mqtt_mock: Any) -> None:
    """No running message for 5 minutes (PC off, job killed): no longer busy, and the progress push
    (sticky) is cleared."""
    entry, pushes, _publishes = await _setup(hass, setup_entry)
    _running(hass, "copy", 30)
    await _settle(hass)
    assert _busy(hass)
    assert len(_pushes(pushes)) == 1
    await _settle(hass, MOVE_SILENT.total_seconds() + 1)
    assert not _busy(hass)
    assert not entry.runtime_data.move.active
    assert _cleared(pushes) == 1
    assert len(_pushes(pushes)) == 1


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_retained_end_of_the_pushed_move_is_told(
    hass: HomeAssistant, setup_entry: Setup, mqtt_mock: Any
) -> None:
    """The end of the move whose progress is on the phone came while the connection was down
    (retained after the reconnect): the result push replaces the progress push, with the event."""
    _entry, pushes, _publishes = await _setup(hass, setup_entry)
    events = async_capture_events(hass, EVENT_MOVED)
    _running(hass, "copy", 40)
    await _settle(hass)
    _send(hass, "ok", retain=True, new_library=False)
    await _settle(hass)
    assert _pushes(pushes)[-1]["title"] == "Steam: moved"
    assert len(events) == 1
    assert not _busy(hass)


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_end_of_the_pushed_move_after_a_restart(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    """The end of the move whose progress is on the phone came while HA was down: read retained
    after the restart, it still replaces the progress push, with the event, once."""
    _store(hass_storage, move_pushed=STARTED)
    _entry, pushes, _publishes = await _setup(hass, setup_entry)
    events = async_capture_events(hass, EVENT_MOVED)
    _send(hass, "ok", retain=True, new_library=False)
    await _settle(hass)
    assert _pushes(pushes)[-1]["title"] == "Steam: moved"
    assert len(events) == 1
    _send(hass, "ok", retain=True, new_library=False)
    await _settle(hass)
    assert len(events) == 1


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_refusal_allows_another_try_at_once(
    hass: HomeAssistant, setup_entry: Setup, mqtt_mock: Any
) -> None:
    """The PC refused the move (no space on E:): another drive can be asked for right away. A
    refusal that came retained (the connection dropped in between) ends the move sent as well."""
    entry, _pushes_, publishes = await _setup(hass, setup_entry)
    jobs = entry.runtime_data.jobs
    assert await jobs.async_move("20", "e") == {"sent": True}
    _send(hass, "error", reason_code="no_space", needed_gb=66, target_free_gb=40)
    await _settle(hass)
    assert not entry.runtime_data.move.active
    assert await jobs.async_move("20", "g") == {"sent": True}
    assert _moves(publishes) == ["move:20@e", "move:20@g"]
    _send(hass, "error", retain=True, reason_code="steam_busy", drive="G:", started="2026-11-11T18:01:00+00:00")
    await _settle(hass)
    assert not entry.runtime_data.move.active
    assert not _busy(hass)
    assert _attrs(hass)["move"] is None
    assert _attrs(hass)["move_result"]["reason_code"] == "steam_busy"


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_late_end_does_not_answer_a_new_move(
    hass: HomeAssistant, setup_entry: Setup, mqtt_mock: Any
) -> None:
    """The end of an earlier move of the same game that the PC could not send then (late, sent by the
    next move before its own messages) is told, but it does not answer the move just sent: that one
    still counts as running (Steam busy, no second command) until its own messages come."""
    entry, _pushes_, publishes = await _setup(hass, setup_entry)
    events = async_capture_events(hass, EVENT_MOVED)
    jobs = entry.runtime_data.jobs
    assert await jobs.async_move("20", "e") == {"sent": True}
    _send(hass, "error", reason_code="copy_failed", late=True, started="2026-11-11T17:00:00+00:00")
    # Nor does one read retained (after a reconnect)
    _send(hass, "error", retain=True, reason_code="copy_failed", late=True, started="2026-11-11T16:00:00+00:00")
    await _settle(hass)
    assert [e.data["reason"] for e in events] == ["copy_failed"]
    assert entry.runtime_data.move.active
    assert _busy(hass)
    assert _attrs(hass)["move"]["phase"] == "requested"
    assert await jobs.async_move("20", "e") == {"sent": False, "reason": "job_running"}
    assert _moves(publishes) == ["move:20@e"]

    _running(hass, "copy", 5)
    _send(hass, "ok", new_library=False)
    await _settle(hass)
    assert not entry.runtime_data.move.active
    assert [e.data["success"] for e in events] == [False, True]


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_later_end_of_the_same_move_is_told(
    hass: HomeAssistant, setup_entry: Setup, mqtt_mock: Any
) -> None:
    """A move that could not be taken back is finished at the next start of the agent (-Recover):
    its end has the same started but is news; the same end read again is not."""
    _entry, pushes, _publishes = await _setup(hass, setup_entry)
    events = async_capture_events(hass, EVENT_MOVED)
    _send(hass, "error", reason_code="rollback_failed")
    await _settle(hass)
    _send(hass, "ok", new_library=False, warning="finished after an interruption", updated="2026-11-11T19:00:00+00:00")
    await _settle(hass)
    assert _pushes(pushes)[-1]["title"] == "Steam: moved"
    assert [e.data["success"] for e in events] == [False, True]
    assert _attrs(hass)["move_result"]["result"] == "ok"
    _send(hass, "ok", retain=True, new_library=False, updated="2026-11-11T19:00:00+00:00")
    await _settle(hass)
    assert len(events) == 2


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_the_agent_reports_a_move(hass: HomeAssistant, setup_entry: Setup, mqtt_mock: Any) -> None:
    """After a restart of HA the agent's steam_moving is all HA knows of a move until its next
    running message: Steam busy, installs wait, no other move; not while the agent is offline."""
    entry, _pushes_, publishes = await _setup(hass, setup_entry)
    hass.states.async_set(DOWNLOADING, "off", {"appids": [], "moving": "ON"})
    await _settle(hass)
    assert entry.runtime_data.move.active
    assert _busy(hass)
    response = await hass.services.async_call(DOMAIN, "install", {"appid": "30"}, blocking=True, return_response=True)
    assert response == {"queued": True, "sent": False}
    assert await entry.runtime_data.jobs.async_move("20", "e") == {"sent": False, "reason": "job_running"}
    hass.states.async_set(AGENT_ONLINE, "off")
    await _settle(hass)
    assert not entry.runtime_data.move.active
    hass.states.async_set(AGENT_ONLINE, "on")
    hass.states.async_set(DOWNLOADING, "off", {"appids": [], "moving": "OFF"})
    await _settle(hass)
    assert not entry.runtime_data.move.active
    await _settle(hass, QUEUE_DELAY + 1)
    assert _installs(publishes) == ["install:30,"]


async def test_no_move_while_an_install_is_handed_over(hass: HomeAssistant, setup_entry: Setup) -> None:
    """An install on its way to the PC (waking it, waiting for the agent to accept) holds the job
    lock: a move would compete with it for the PC's job lock."""
    entry, _pushes_, publishes = await _setup(hass, setup_entry)
    jobs = entry.runtime_data.jobs
    async with jobs._lock:
        assert await jobs.async_move("20", "e") == {"sent": False, "reason": "job_running"}
    assert _moves(publishes) == []
