"""The push "not enough space" (no_space.py): its content and buttons for a first install and for an
update, the pushes its buttons lead to (deletion candidates, the confirmation naming the game that
waits, games to move off the drive and the drives they fit on, other drives) with at most two
choices and "Back" to the first push, the game sent again after a successful uninstall or move, the
short push when the uninstall or the move fails or the space is still short,
several games without space in one job, no result push for a job that ended only with games without
space, the push with result pushes not chosen, automatic updates that stop after no space until
room is made, and a named PC's buttons."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_mock_service,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.steam_engine.const import (
    CONF_NOTIFY_SERVICE,
    CONF_PUSH_KINDS,
    CONF_STEAM_LINK,
    DOMAIN,
    PUSH_KINDS,
    SECTION_NOTIFY,
    SECTION_STEAM,
    merged_options,
)
from custom_components.steam_engine.game_size import STORAGE_KEY as SIZES_KEY, async_game_sizes
from custom_components.steam_engine import no_space as no_space_module
from custom_components.steam_engine.no_space import AFTER_KEEP
from custom_components.steam_engine.push import TAG_PROGRESS
from custom_components.steam_engine.space import GB
from custom_components.steam_engine.steam_move import REQUEST_WAIT
from custom_components.steam_engine.steam_updates import BUILD_URL

from .test_jobs_power import (  # noqa: F401 (setup_entry is a fixture)
    GAME_RUNNING,
    LIBRARY,
    PHONE,
    STATUS,
    Setup,
    _active_hours,
    _done,
    _installs,
    _pc_on,
    _settle,
    _store,
    setup_entry,
)
from .test_space import DRIVES, LIBRARY_FREE, _game

LINK = "/dashboard-steam"
OPTIONS = {SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: PHONE}, SECTION_STEAM: {CONF_STEAM_LINK: LINK}}
TAG = "steam_engine_no_space"
VIEW = [{"action": "URI", "title": "View", "uri": LINK}]
BACK_40 = {"action": "STEAM_ENGINE_BACK_40", "title": "Back"}
ROOM_40 = {"action": "STEAM_ENGINE_ROOM_40", "title": "Free up space"}
MOVE_40 = {"action": "STEAM_ENGINE_MOVE_40", "title": "Move a game"}
OTHER_40 = {"action": "STEAM_ENGINE_OTHER_40", "title": "Other drive"}
MOVE_TAG = "steam_engine_move"
# Game 40 (not installed, 60 GB to install: 66 GB with the reserve) did not fit on F: (42 GB free)
NO_SPACE_40 = {"appid": "40", "needed_gb": 66, "free_gb": 42, "drive": "F:"}


def _library_games() -> list[dict[str, Any]]:
    return [
        {**_game("10", 200, drive="E:"), "buildid": "1"},
        {**_game("20", 60), "buildid": "1", "name": "Old F"},
        {**_game("30", 90), "buildid": "1"},  # updated in the job that ends: free by then
        {**_game("50", 10), "buildid": "1"},  # too small to help
    ]


def _sizes(hass_storage: dict[str, Any], **sizes: float) -> None:
    """Install sizes game_size.py knows already (bytes per app ID), so nothing is fetched."""
    now = dt_util.utcnow().isoformat()
    hass_storage[SIZES_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": SIZES_KEY,
        "data": {"apps": {appid: {"size": size, "download": None, "fetched": now} for appid, size in sizes.items()}},
    }


def _pushes(calls: list[ServiceCall], tag: str = TAG) -> list[dict[str, Any]]:
    """The pushes with this tag, without the clearing of one."""
    return [
        dict(c.data) for c in calls if c.data.get("message") != "clear_notification" and c.data.get("data", {}).get("tag") == tag
    ]


def _cleared(calls: list[ServiceCall], tag: str = TAG) -> bool:
    return any(c.data == {"message": "clear_notification", "data": {"tag": tag}} for c in calls)


def _results(calls: list[ServiceCall]) -> list[dict[str, Any]]:
    """The result pushes (TAG_PROGRESS with a View button; not the start or progress of a job)."""
    return [p for p in _pushes(calls, TAG_PROGRESS) if p["data"].get("actions")]


def _uninstalls(calls: list[ServiceCall]) -> list[str]:
    return [str(c.data["payload"]) for c in calls if str(c.data.get("payload", "")).startswith("uninstall:")]


def _moves(calls: list[ServiceCall]) -> list[str]:
    return [str(c.data["payload"]) for c in calls if str(c.data.get("payload", "")).startswith("move:")]


async def _run(hass: HomeAssistant, times: int = 4) -> None:
    """Let the background tasks of the pushes and buttons run on (they are not tracked by
    async_block_till_done; a job sent waits for its acceptance, so not waited for either)."""
    for _ in range(times):
        await _settle(hass)


async def _press(hass: HomeAssistant, action: str) -> None:
    hass.bus.async_fire("mobile_app_notification_action", {"action": action})
    await _run(hass)


async def _job_without_space(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    setup_entry: Setup,
    no_space: list[dict[str, Any]],
    *,
    games: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    sizes: dict[str, float] | None = None,
    lists: dict[str, Any] | None = None,
) -> tuple[MockConfigEntry, list[ServiceCall], list[ServiceCall]]:
    """A job of 30 (updated) and the games of no_space ends; returns the entry, the pushes and the
    MQTT commands."""
    pushes = async_mock_service(hass, "notify", PHONE)
    job = ["30"] + [str(k["appid"]) for k in no_space]
    _store(hass_storage, job=job, **(lists or {}))
    _sizes(hass_storage, **(sizes if sizes is not None else {"40": 60 * GB}))
    await async_game_sizes(hass)
    _pc_on(hass)
    library = games if games is not None else _library_games()
    hass.states.async_set(LIBRARY, str(len(library)), {"games": library})
    hass.states.async_set(LIBRARY_FREE, "812", {"drives": DRIVES})
    entry = await setup_entry(options or OPTIONS)
    publishes = async_mock_service(hass, "mqtt", "publish")
    hass.states.async_set(STATUS, "started", {"appids": job, "count": len(job)})
    await _settle(hass)
    hass.states.async_set(STATUS, "done_with_errors", _done(ok=["30"], no_space=no_space))
    await _run(hass)
    return entry, pushes, publishes


def _uninstall_result(hass: HomeAssistant, appid: str, name: str, ok: bool, free_gb: float | None, reason: str = "") -> None:
    payload = {"appid": appid, "name": name, "result": "ok" if ok else "error", "reason": reason or None, "free_gb": free_gb}
    async_fire_mqtt_message(hass, "steam/uninstall", json.dumps(payload))


def _move_result(hass: HomeAssistant, ok: bool, **fields: Any) -> None:
    """The end of a move of Old F (20) from F: to E: as move.ps1 sends it (live)."""
    payload = {
        "state": "ok" if ok else "error",
        "started": "2026-11-11T18:00:00+00:00",
        "appid": "20",
        "name": "Old F",
        "from": "F:",
        "drive": "E:",
        "updated": "2026-11-11T18:05:00+00:00",
        **fields,
    }
    async_fire_mqtt_message(hass, "steam/move", json.dumps(payload))


async def test_first_install_push_and_result_push(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """A first install without space: a push of its own with "Free up space", "Move a game" and
    "Other drive"; the result push only names the game (no buttons to delete). Game 30, updated in
    the job that ends, is a candidate already (the job list still holds it then)."""
    _entry, pushes, _publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40])
    [push] = _pushes(pushes)
    assert push["title"] == "Steam: not enough space on F: (SSD)"
    assert push["message"] == (
        "Game 40 needs 66 GB (with reserve), 42 GB are free. "
        "Removing one of these games on F: (SSD) frees enough for Game 40: G30 (90 GB, never played), "
        "Old F (60 GB, never played). "
        "Moving one of these games to another drive frees enough as well: G30, Old F. "
        "Fits on: E: (HDD) with 812 GB free, G: (SSD, USB) with 230 GB free, H: (USB) with 90 GB free."
    )
    assert push["data"]["actions"] == [ROOM_40, MOVE_40, OTHER_40]
    assert push["data"]["notification_icon"] == "mdi:harddisk-remove"
    [result] = _pushes(pushes, TAG_PROGRESS)[-1:]
    assert "Not enough space: Game 40 (needs 66 GB, 42 GB free on F: (SSD))." in result["message"]
    assert result["data"]["actions"] == VIEW


# The uninstall result comes over MQTT; its client keeps a periodic timer past the time jumps
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_room_ask_yes_and_install_again(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    """Free up space -> the candidates -> asks, naming the game that waits -> uninstalls -> the
    game goes out again once the uninstall went well, as asked for by hand."""
    _entry, pushes, publishes = await _job_without_space(
        hass, hass_storage, setup_entry, [NO_SPACE_40], lists={"targets": {"40": "f"}}
    )

    await _press(hass, "STEAM_ENGINE_ROOM_40")
    room = _pushes(pushes)[-1]
    assert room["title"] == "Steam: free up space on F: (SSD)"
    # The same candidates as in the first push, the largest first
    assert room["data"]["actions"] == [
        {"action": "STEAM_ENGINE_UNINSTALL_ASK_30_40", "title": "G30"},
        {"action": "STEAM_ENGINE_UNINSTALL_ASK_20_40", "title": "Old F"},
        BACK_40,
    ]
    assert room["message"].endswith("Tap a game to delete it (asks first).")

    await _press(hass, "STEAM_ENGINE_UNINSTALL_ASK_20_40")
    ask = _pushes(pushes)[-1]
    assert ask["title"] == "Really uninstall?"
    assert ask["message"] == "Old F (60 GB) will be deleted from the PC. Then Game 40 will be installed on F: (SSD)."
    assert ask["data"]["actions"] == [
        {"action": "STEAM_ENGINE_UNINSTALL_YES_20_40", "title": "Yes, delete"},
        {"action": "STEAM_ENGINE_UNINSTALL_NO_0_40", "title": "Cancel"},
    ]
    assert _uninstalls(publishes) == []

    await _press(hass, "STEAM_ENGINE_UNINSTALL_YES_20_40")
    assert _cleared(pushes)
    assert _uninstalls(publishes) == ["uninstall:20"]
    assert _installs(publishes) == []

    _uninstall_result(hass, "20", "Old F", True, 102)
    await _run(hass)
    # Sent again to the drive it was meant for
    assert _installs(publishes) == ["install:40@f,"]
    done = _pushes(pushes, "steam_engine_uninstall")[-1]
    assert done["message"] == "Old F was removed. Free on the drive: 102 GB. Game 40 is being installed now."

    # Another uninstall result later does not send it again
    _uninstall_result(hass, "20", "Old F", True, 102)
    await _run(hass)
    assert _installs(publishes) == ["install:40@f,"]


# The uninstall result comes over MQTT; its client keeps a periodic timer past the time jumps
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_cancel_and_failed_uninstall(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    """"Cancel" of the question brings the first push again (its other way stays open); a failed
    uninstall says so."""
    _entry, pushes, publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40])
    first = _pushes(pushes)[0]
    await _press(hass, "STEAM_ENGINE_UNINSTALL_ASK_20_40")
    await _press(hass, "STEAM_ENGINE_UNINSTALL_NO_0_40")
    assert _pushes(pushes)[-1] == first
    assert first["data"]["actions"] == [ROOM_40, MOVE_40, OTHER_40]
    assert _uninstalls(publishes) == []

    await _press(hass, "STEAM_ENGINE_UNINSTALL_YES_20_40")
    _uninstall_result(hass, "20", "Old F", False, None, "steam_busy")
    await _run(hass)
    failed = _pushes(pushes)[-1]
    assert failed["title"] == "Steam: uninstall failed"
    assert failed["message"] == "Game 40 is not installed: removing Old F failed (Steam is working on the game right now)."
    assert _installs(publishes) == []


# The uninstall result comes over MQTT; its client keeps a periodic timer past the time jumps
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_still_not_enough_space(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    _entry, pushes, publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40])
    await _press(hass, "STEAM_ENGINE_UNINSTALL_YES_20_40")
    _uninstall_result(hass, "20", "Old F", True, 50)
    await _run(hass)
    short = _pushes(pushes)[-1]
    assert short["title"] == "Steam: still not enough space"
    assert short["message"] == "16 GB are still missing on F: (SSD) for Game 40."
    assert _installs(publishes) == []
    done = _pushes(pushes, "steam_engine_uninstall")[-1]
    assert done["message"] == "Old F was removed. Free on the drive: 50 GB."


# The move result comes over MQTT; its client keeps a periodic timer past the time jumps
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_move_a_game_and_install_again(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    """Move a game -> the games that free enough and fit on another drive (in the order of "Free up
    space") -> the drives one fits on -> moved at once, without a question (it deletes nothing) ->
    the game without space goes out again once the move went well, the move's result push says so."""
    _entry, pushes, publishes = await _job_without_space(
        hass, hass_storage, setup_entry, [NO_SPACE_40], lists={"targets": {"40": "f"}}
    )

    await _press(hass, "STEAM_ENGINE_MOVE_40")
    games = _pushes(pushes)[-1]
    assert games["title"] == "Steam: move a game off F: (SSD)"
    assert games["message"] == (
        "Moving one of these games to another drive frees enough on F: (SSD) for Game 40: "
        "G30 (90 GB, never played), Old F (60 GB, never played). Tap a game to choose the drive."
    )
    assert games["data"]["actions"] == [
        {"action": "STEAM_ENGINE_MOVEGAME_30_40", "title": "G30"},
        {"action": "STEAM_ENGINE_MOVEGAME_20_40", "title": "Old F"},
        BACK_40,
    ]
    assert games["data"]["notification_icon"] == "mdi:folder-move-outline"

    await _press(hass, "STEAM_ENGINE_MOVEGAME_20_40")
    drives = _pushes(pushes)[-1]
    assert drives["title"] == "Steam: move Old F"
    # 60 GB plus the reserve of an install (6 GB); libraries first, then an SSD; C: is too small
    assert drives["message"] == (
        "Old F needs 66 GB with reserve and fits on: E: (HDD) with 812 GB free, G: (SSD, USB) with 230 GB "
        "free. Tap a drive to move it there (nothing is deleted). Then Game 40 will be installed on F: (SSD)."
    )
    assert drives["data"]["actions"] == [
        {"action": "STEAM_ENGINE_MOVETO_20_40_e", "title": "E: HDD"},
        {"action": "STEAM_ENGINE_MOVETO_20_40_g", "title": "G: SSD USB"},
        BACK_40,
    ]
    assert _moves(publishes) == []

    await _press(hass, "STEAM_ENGINE_MOVETO_20_40_e")
    assert _cleared(pushes)
    assert _moves(publishes) == ["move:20@e"]
    assert _installs(publishes) == []

    _move_result(hass, True, library="E:\\SteamLibrary", new_library=False, source_free_gb=102, target_free_gb=752)
    await _run(hass)
    # Sent again to the drive it was meant for
    assert _installs(publishes) == ["install:40@f,"]
    moved = _pushes(pushes, MOVE_TAG)[-1]
    assert moved["title"] == "Steam: moved"
    assert moved["message"] == "Old F is now on E: (HDD). Free: E: 752 GB, F: 102 GB. Game 40 is being installed now."

    # Another move result later does not send it again
    _move_result(hass, True, source_free_gb=102, target_free_gb=752, updated="2026-11-11T19:05:00+00:00")
    await _run(hass)
    assert _installs(publishes) == ["install:40@f,"]


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_failed_move_says_so_also_with_result_pushes_off(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    """A move the integration refuses (a game runs) and one the PC reports as failed: a short push
    each, the second as the move's result push, which comes although result pushes are not chosen
    (it answers a button). Nothing is installed."""
    kinds = [k for k in PUSH_KINDS if k != "result"]
    options = {SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: PHONE, CONF_PUSH_KINDS: kinds}, SECTION_STEAM: {CONF_STEAM_LINK: LINK}}
    _entry, pushes, publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40], options=options)

    hass.states.async_set(GAME_RUNNING, "on", {"appid": "10"})
    await _press(hass, "STEAM_ENGINE_MOVETO_20_40_e")
    refused = _pushes(pushes)[-1]
    assert refused["title"] == "Steam: move failed"
    assert refused["message"] == (
        "Game 40 is not installed: moving Old F failed (a game or a program from the game's folder is running)."
    )
    assert _moves(publishes) == []

    # The game without space is still known: the next try goes out
    hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
    await _press(hass, "STEAM_ENGINE_MOVETO_20_40_e")
    assert _moves(publishes) == ["move:20@e"]
    _move_result(hass, False, reason_code="copy_failed", reason="robocopy exit code 8")
    await _run(hass)
    failed = _pushes(pushes, MOVE_TAG)[-1]
    assert failed["title"] == "Steam: move failed"
    assert failed["message"] == "Old F was not moved: copying failed. So Game 40 is not installed."
    assert _installs(publishes) == []


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_moved_but_still_not_enough_space(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    """The move went well, but the drive it left has too little free space still: the short push,
    nothing is installed; the move's result push says nothing more."""
    _entry, pushes, publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40])
    await _press(hass, "STEAM_ENGINE_MOVETO_20_40_e")
    _move_result(hass, True, source_free_gb=50, target_free_gb=752)
    await _run(hass)
    short = _pushes(pushes)[-1]
    assert short["title"] == "Steam: still not enough space"
    assert short["message"] == "16 GB are still missing on F: (SSD) for Game 40."
    assert _installs(publishes) == []
    assert _pushes(pushes, MOVE_TAG)[-1]["message"] == "Old F is now on E: (HDD). Free: E: 752 GB, F: 50 GB."


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_move_buttons_of_an_old_push(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    """Buttons of an old copy of the push: a game not on the full drive, a move back onto it and a
    second tap while the move runs move nothing, and the running move still sends the game again."""
    _entry, pushes, publishes = await _job_without_space(
        hass, hass_storage, setup_entry, [NO_SPACE_40], lists={"targets": {"40": "f"}}
    )
    await _press(hass, "STEAM_ENGINE_MOVETO_10_40_g")  # game 10 is on E:
    await _press(hass, "STEAM_ENGINE_MOVETO_20_40_f")  # onto the full drive itself
    assert _moves(publishes) == []

    await _press(hass, "STEAM_ENGINE_MOVETO_20_40_e")
    assert _moves(publishes) == ["move:20@e"]
    count = len(_pushes(pushes))
    await _press(hass, "STEAM_ENGINE_MOVETO_20_40_g")  # another phone, while the move runs
    assert _moves(publishes) == ["move:20@e"]
    assert len(_pushes(pushes)) == count  # no "move failed" push

    _move_result(hass, True, source_free_gb=102, target_free_gb=752)
    await _run(hass)
    assert _installs(publishes) == ["install:40@f,"]


async def test_other_drive(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """Other drive -> up to two drives it fits on (libraries first, an SSD before others) -> the
    game is installed on the drive tapped, at once also outside the active hours."""
    entry, pushes, publishes = await _job_without_space(
        hass, hass_storage, setup_entry, [NO_SPACE_40], options={**OPTIONS, **_active_hours(False)}
    )
    assert not entry.runtime_data.jobs.hours.is_open()

    await _press(hass, "STEAM_ENGINE_OTHER_40")
    other = _pushes(pushes)[-1]
    assert other["title"] == "Steam: other drive for Game 40"
    assert other["message"] == (
        "Game 40 needs 66 GB with reserve and fits on: E: (HDD) with 812 GB free, G: (SSD, USB) with "
        "230 GB free. Tap a drive to install it there."
    )
    assert other["data"]["actions"] == [
        {"action": "STEAM_ENGINE_DRIVE_40_e", "title": "E: HDD"},
        {"action": "STEAM_ENGINE_DRIVE_40_g", "title": "G: SSD USB"},
        BACK_40,
    ]

    await _press(hass, "STEAM_ENGINE_DRIVE_40_g")
    assert _cleared(pushes)
    assert _installs(publishes) == ["install:40@g,"]
    assert entry.runtime_data.jobs.targets == {"40": "g"}


async def test_neither_room_nor_other_drive(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """900 GB fit nowhere, and no single game frees enough: only "View"; the pushes its buttons
    would lead to (a button of an older push) have only "Back"."""
    no_space = [{"appid": "40", "needed_gb": 990, "free_gb": 42, "drive": "F:"}]
    _entry, pushes, _publishes = await _job_without_space(
        hass, hass_storage, setup_entry, no_space, sizes={"40": 900 * GB}
    )
    [push] = _pushes(pushes)
    assert push["message"] == (
        "Game 40 needs 990 GB (with reserve), 42 GB are free. "
        "Removing one game on F: (SSD) does not free enough for Game 40 (948 GB missing)."
    )
    assert push["data"]["actions"] == VIEW

    await _press(hass, "STEAM_ENGINE_ROOM_40")
    assert _pushes(pushes)[-1]["data"]["actions"] == [BACK_40]
    await _press(hass, "STEAM_ENGINE_MOVE_40")
    move = _pushes(pushes)[-1]
    assert move["message"] == "No game on F: (SSD) can be moved to another drive right now."
    assert move["data"]["actions"] == [BACK_40]
    await _press(hass, "STEAM_ENGINE_OTHER_40")
    other = _pushes(pushes)[-1]
    assert other["title"] == "Steam: other drive for Game 40"
    assert other["data"]["actions"] == [BACK_40]


async def test_back_brings_the_first_push_again(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """"Back" from "Free up space", "Move a game" (and the drives of a game to move) and "Other
    drive" sends the first push again, all ways open; nothing is uninstalled, moved or installed."""
    _entry, pushes, publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40])
    [first] = _pushes(pushes)
    for way in ("STEAM_ENGINE_ROOM_40", "STEAM_ENGINE_MOVE_40", "STEAM_ENGINE_MOVEGAME_20_40", "STEAM_ENGINE_OTHER_40"):
        await _press(hass, way)
        assert _pushes(pushes)[-1]["data"]["actions"][-1] == BACK_40
        await _press(hass, "STEAM_ENGINE_BACK_40")
        assert _pushes(pushes)[-1] == first
    assert len(_pushes(pushes)) == 9
    assert _installs(publishes) == [] and _uninstalls(publishes) == [] and _moves(publishes) == []


async def test_only_games_without_space_send_no_result_push(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """A job with games installed and one without space has its result push (beside the push "not
    enough space"); a job that ended only with games without space has the push "not enough space"
    alone, and its progress bar goes."""
    _entry, pushes, _publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40])
    assert len(_results(pushes)) == 1
    assert len(_pushes(pushes)) == 1

    hass.states.async_set(STATUS, "started", {"appids": ["40"], "count": 1})
    await _settle(hass)
    before = len(pushes)
    hass.states.async_set(STATUS, "done_with_errors", _done(no_space=[NO_SPACE_40]))
    await _run(hass)
    assert len(_results(pushes)) == 1
    assert len(_pushes(pushes)) == 2
    assert _cleared(pushes[before:], TAG_PROGRESS)


async def test_size_unknown_offers_no_other_drive(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    setup_entry: Setup,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The size is asked for (api.steamcmd.net gives none): nothing can be said about the other
    drives, so no "Other drive"; moving a game off the drive does not need it."""
    aioclient_mock.get(BUILD_URL.format(appid="40"), json={"data": {"40": {"common": {"name": "Game 40"}}}})
    _entry, pushes, _publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40], sizes={})
    await _run(hass, 10)
    assert aioclient_mock.call_count == 1
    [push] = _pushes(pushes)
    assert push["data"]["actions"] == [ROOM_40, MOVE_40]
    assert "Fits on" not in push["message"]


async def test_several_games_without_space(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """Buttons only for the first game, the others are named; none of them is a candidate."""
    no_space = [NO_SPACE_40, {"appid": "20", "needed_gb": 20, "free_gb": 42, "drive": "F:"}, {"appid": "41", "needed_gb": 30, "free_gb": 10, "drive": "F:"}]
    _entry, pushes, _publishes = await _job_without_space(hass, hass_storage, setup_entry, no_space)
    [push] = _pushes(pushes)
    assert push["title"] == "Steam: not enough space on F: (SSD)"
    assert push["message"].endswith("Not enough space for these either: Old F, Game 41.")
    # Old F did not fit itself (an update): it is no candidate, G30 (updated in the job) is
    assert (
        "frees enough for Game 40: G30 (90 GB, never played). "
        "Moving one of these games to another drive frees enough as well: G30. Fits on:"
    ) in push["message"]
    assert push["data"]["actions"] == [ROOM_40, MOVE_40, OTHER_40]


async def test_earlier_game_without_space_is_a_candidate_again(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """Only the games without space of the same result are no candidates: an update that found no
    space in an earlier job (push left alone) may be suggested for another game."""
    old_f = {"appid": "20", "needed_gb": 20, "free_gb": 5, "drive": "F:"}
    _entry, pushes, _publishes = await _job_without_space(hass, hass_storage, setup_entry, [old_f])
    hass.states.async_set(STATUS, "started", {"appids": ["40"], "count": 1})
    await _settle(hass)
    hass.states.async_set(STATUS, "done_with_errors", _done(no_space=[NO_SPACE_40]))
    await _run(hass)
    push = _pushes(pushes)[-1]
    assert "frees enough for Game 40: G30 (90 GB, never played), Old F (60 GB, never played)." in push["message"]


# The uninstall result comes over MQTT; its client keeps a periodic timer past the time jumps
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_update_without_space_with_result_pushes_off(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    """An automatic update without space: the push comes without result pushes chosen, has no
    "Other drive" (moving another game off the drive is offered); the automatic updates stop for the game until room is made through the push,
    then the update goes out again and they may try it again."""
    kinds = [k for k in PUSH_KINDS if k != "result"]
    options = {SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: PHONE, CONF_PUSH_KINDS: kinds}, SECTION_STEAM: {CONF_STEAM_LINK: LINK}}
    games = [{**_game("10", 50), "buildid": "1", "name": "Alpha"}, *_library_games()[1:]]
    no_space = [{"appid": "10", "needed_gb": 14, "free_gb": 5, "drive": "F:"}]
    entry, pushes, publishes = await _job_without_space(
        hass, hass_storage, setup_entry, no_space, games=games, options=options, lists={"auto_update": ["10"]}
    )
    jobs = entry.runtime_data.jobs
    assert not any("Not enough space" in p["message"] for p in _pushes(pushes, TAG_PROGRESS))  # no result push
    [push] = _pushes(pushes)
    assert push["title"] == "Steam: no space for the update on F: (SSD)"
    assert push["message"] == (
        "Alpha: 9 GB missing for the update (needs 14 GB with reserve, 5 GB free). The game stays installed. "
        "Removing one of these games on F: (SSD) frees enough for Alpha: G30 (90 GB, never played), "
        "Old F (60 GB, never played), G50 (10 GB, never played). "
        "Moving one of these games to another drive frees enough as well: G30, Old F."
    )
    assert push["data"]["actions"] == [
        {"action": "STEAM_ENGINE_ROOM_10", "title": "Free up space"},
        {"action": "STEAM_ENGINE_MOVE_10", "title": "Move a game"},
    ]
    assert "10" in jobs.auto_given_up()

    await _press(hass, "STEAM_ENGINE_UNINSTALL_ASK_20_10")
    assert _pushes(pushes)[-1]["message"] == "Old F (60 GB) will be deleted from the PC. Then Alpha will be updated."
    await _press(hass, "STEAM_ENGINE_UNINSTALL_YES_20_10")
    _uninstall_result(hass, "20", "Old F", True, 65)
    await _run(hass)
    assert _installs(publishes) == ["install:10,"]
    assert "10" not in jobs.auto_given_up()
    assert jobs.auto_quiet == set()  # asked for by hand


async def test_button_after_a_restart(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """What a button refers to is kept in memory only: after a restart a push says so, and "Yes,
    delete" deletes nothing (the game it promised to send again is not known any more)."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _pc_on(hass)
    await setup_entry(OPTIONS)
    publishes = async_mock_service(hass, "mqtt", "publish")
    await _press(hass, "STEAM_ENGINE_ROOM_40")
    gone = _pushes(pushes)[-1]
    assert gone["message"].startswith("This push about Game 40 is out of date")
    assert gone["data"]["actions"] == VIEW
    await _press(hass, "STEAM_ENGINE_BACK_40")
    assert _pushes(pushes)[-1] == gone
    assert _installs(publishes) == [] and _uninstalls(publishes) == []

    await _press(hass, "STEAM_ENGINE_UNINSTALL_YES_20_40")
    assert _pushes(pushes)[-1]["message"] == (
        "This push about Game 40 is out of date (installed or sent again meanwhile, or Home Assistant "
        "was restarted). See the card. Game 20 was not deleted."
    )
    assert _installs(publishes) == [] and _uninstalls(publishes) == []

    # Nothing is moved for a game that is not known any more either
    await _press(hass, "STEAM_ENGINE_MOVETO_20_40_e")
    assert _pushes(pushes)[-1]["message"].startswith("This push about Game 40 is out of date")
    assert _moves(publishes) == []

    # A game of an old "Free up space" push: no question (its "Yes, delete" would delete without
    # the game it was for), and an old "Other drive" button installs nothing
    await _press(hass, "STEAM_ENGINE_UNINSTALL_ASK_20_40")
    assert _pushes(pushes)[-1]["message"].startswith("This push about Game 40 is out of date")
    assert _pushes(pushes, "steam_engine_uninstall_ask") == []
    await _press(hass, "STEAM_ENGINE_DRIVE_40_e")
    assert _pushes(pushes)[-1]["message"].startswith("This push about Game 40 is out of date")
    assert _installs(publishes) == [] and _uninstalls(publishes) == []


async def test_buttons_after_the_game_was_sent_again(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """The game without space was sent again by hand meanwhile (another drive on the card): the
    buttons of its old push (another phone) say so and neither delete nor install anything."""
    entry, pushes, publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40])
    jobs = entry.runtime_data.jobs
    hass.async_create_task(jobs.async_install(["40"], drive="e"))
    await _settle(hass)
    hass.states.async_set(STATUS, "started", {"appids": ["40"], "count": 1})  # the PC took it
    await _run(hass)
    # Sent (until the PC took it): only to E:
    assert set(_installs(publishes)) == {"install:40@e,"}
    assert "40" in jobs.job

    for action in ("STEAM_ENGINE_ROOM_40", "STEAM_ENGINE_UNINSTALL_ASK_20_40", "STEAM_ENGINE_UNINSTALL_YES_20_40"):
        await _press(hass, action)
        assert _pushes(pushes)[-1]["message"].startswith("This push about Game 40 is out of date"), action
    await _press(hass, "STEAM_ENGINE_DRIVE_40_g")
    assert set(_installs(publishes)) == {"install:40@e,"}
    assert _uninstalls(publishes) == []
    assert jobs.targets == {"40": "e"}


async def test_other_drive_only_for_a_first_install(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """An update has no "Other drive": a forged or old button for it installs nothing."""
    no_space = [{"appid": "20", "needed_gb": 30, "free_gb": 20, "drive": "F:"}]
    _entry, pushes, publishes = await _job_without_space(hass, hass_storage, setup_entry, no_space)
    assert all(a["action"] != "STEAM_ENGINE_OTHER_20" for a in _pushes(pushes)[0]["data"]["actions"])
    await _press(hass, "STEAM_ENGINE_DRIVE_20_e")
    assert _installs(publishes) == []


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_silent_move_brings_the_push_again(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    """A move from the push that ends without an end (the PC did not answer): the first push comes
    again, and moving from it works once more."""
    _entry, pushes, publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40])
    first = _pushes(pushes)[0]
    await _press(hass, "STEAM_ENGINE_MOVETO_20_40_e")
    assert _moves(publishes) == ["move:20@e"]
    assert _cleared(pushes)

    await _settle(hass, REQUEST_WAIT.total_seconds() + 1)
    await _run(hass)
    assert _pushes(pushes)[-1] == first

    await _press(hass, "STEAM_ENGINE_MOVETO_20_40_g")
    assert _moves(publishes) == ["move:20@e", "move:20@g"]


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_an_old_uninstall_does_not_send_the_game(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    """The result of an uninstall from the push never came; when the game is uninstalled again much
    later (AFTER_KEEP), the game without space is not sent without being asked."""
    _entry, pushes, publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40])
    await _press(hass, "STEAM_ENGINE_UNINSTALL_YES_20_40")
    assert _uninstalls(publishes) == ["uninstall:20"]

    # The result comes long after: only no_space.py sees the later time
    later = dt_util.utcnow() + AFTER_KEEP + timedelta(minutes=1)
    with patch.object(no_space_module, "dt_util", SimpleNamespace(utcnow=lambda: later, now=dt_util.now)):
        _uninstall_result(hass, "20", "Old F", True, 102)
        await _run(hass)
    assert _installs(publishes) == []
    assert _pushes(pushes, "steam_engine_uninstall")[-1]["message"] == "Old F was removed. Free on the drive: 102 GB."


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_uninstall_answer_with_uninstall_pushes_off(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup, mqtt_mock: Any
) -> None:
    """The uninstall from "Yes, delete" answers a button: its result push comes also when uninstall
    pushes are not chosen; one asked for elsewhere does not."""
    kinds = [k for k in PUSH_KINDS if k != "uninstall"]
    options = {SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: PHONE, CONF_PUSH_KINDS: kinds}, SECTION_STEAM: {CONF_STEAM_LINK: LINK}}
    _entry, pushes, publishes = await _job_without_space(hass, hass_storage, setup_entry, [NO_SPACE_40], options=options)
    await _press(hass, "STEAM_ENGINE_UNINSTALL_YES_20_40")
    _uninstall_result(hass, "20", "Old F", True, 102)
    await _run(hass)
    assert _installs(publishes) == ["install:40,"]
    [done] = _pushes(pushes, "steam_engine_uninstall")
    assert done["message"] == "Old F was removed. Free on the drive: 102 GB. Game 40 is being installed now."

    _uninstall_result(hass, "50", "G50", True, 112)
    await _run(hass)
    assert len(_pushes(pushes, "steam_engine_uninstall")) == 1


async def test_named_pc(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """A named PC's buttons carry its ID; only its own are handled, and the install goes to it."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _sizes(hass_storage, **{"40": 60 * GB})
    await async_game_sizes(hass)
    prefix = "steam_pc_kinderpc_"
    hass.states.async_set(f"binary_sensor.{prefix}agent_online", "on")
    hass.states.async_set(f"binary_sensor.{prefix}game_running", "off", {"appid": 0})
    hass.states.async_set(f"sensor.{prefix}network", "100", {"unit_of_measurement": "B/s"})
    hass.states.async_set(f"button.{prefix}shutdown", "unknown")
    hass.states.async_set(f"sensor.{prefix}last_input", dt_util.utcnow().isoformat())
    hass.states.async_set(f"sensor.{prefix}library", "1", {"games": _library_games()})
    hass.states.async_set(f"sensor.{prefix}library_free", "812", {"drives": DRIVES})
    kids = MockConfigEntry(domain=DOMAIN, data={"pc_name": "Kinder-PC"}, options=merged_options(OPTIONS), unique_id="kinderpc")
    kids.add_to_hass(hass)
    assert await hass.config_entries.async_setup(kids.entry_id)
    await hass.async_block_till_done()
    publishes = async_mock_service(hass, "mqtt", "publish")

    status = f"sensor.{prefix}install_status"
    hass.states.async_set(status, "started", {"appids": ["40"], "count": 1})
    await _settle(hass)
    hass.states.async_set(status, "done_with_errors", _done(no_space=[NO_SPACE_40]))
    await _run(hass)
    [push] = _pushes(pushes, f"{TAG}_kinderpc")
    assert push["title"] == "Steam: not enough space on F: (SSD)"  # one PC: no name before it
    assert push["data"]["actions"] == [
        {"action": "STEAM_ENGINE_ROOM_40@kinderpc", "title": "Free up space"},
        {"action": "STEAM_ENGINE_MOVE_40@kinderpc", "title": "Move a game"},
        {"action": "STEAM_ENGINE_OTHER_40@kinderpc", "title": "Other drive"},
    ]

    # A button without the PC's ID is another PC's
    await _press(hass, "STEAM_ENGINE_DRIVE_40_e")
    assert _installs(publishes) == [] and _uninstalls(publishes) == []
    await _press(hass, "STEAM_ENGINE_OTHER_40@kinderpc")
    other = _pushes(pushes, f"{TAG}_kinderpc")[-1]
    assert other["data"]["actions"][0] == {"action": "STEAM_ENGINE_DRIVE_40_e@kinderpc", "title": "E: HDD"}
    await _press(hass, "STEAM_ENGINE_DRIVE_40_e@kinderpc")
    assert [c.data["topic"] for c in publishes if str(c.data.get("payload", "")).startswith("install:")] == [
        "steam/pc/kinderpc/agent/command"
    ]
    assert _installs(publishes) == ["install:40@e,"]

    assert await hass.config_entries.async_unload(kids.entry_id)
    await hass.async_block_till_done()
