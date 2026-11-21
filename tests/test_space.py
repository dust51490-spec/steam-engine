"""Space on the PC's drives (space.py): drive letters and labels for pushes, the fit rule of the
install job's space check, the drives from the agent's free space sensor, the deletion candidates
for a game without space and the drives a game can be moved to (the pushes that use them:
test_no_space.py)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.steam_engine.pc import PcNames
from custom_components.steam_engine.space import (
    GB,
    deletion_candidates,
    drive_label,
    drive_letter,
    drive_short,
    drives_of,
    fits,
    frees_enough_alone,
    missing_gb,
    missing_whole_gb,
    move_needed_gb,
    move_targets,
    needed_gb,
)

LIBRARY_FREE = "sensor.steam_pc_library_free"
DRIVES = [
    {"drive": "F:", "free_gb": 42, "library": "F:\\SteamLibrary", "default": True, "bus": "nvme", "media": "ssd"},
    {"drive": "E:", "free_gb": 812, "library": "E:\\SteamLibrary", "default": False, "bus": "sata", "media": "hdd"},
    {"drive": "G:", "free_gb": 230, "library": None, "default": False, "bus": "usb", "media": "ssd"},
    {"drive": "H:", "free_gb": 90, "library": None, "default": False, "bus": "usb", "media": None},
    {"drive": "C:", "free_gb": 60, "library": None, "default": False, "bus": None, "media": None},
]


def _game(appid: str, size_gb: float, drive: str = "F:", played: str = "", **extra: Any) -> dict[str, Any]:
    return {"appid": appid, "name": f"G{appid}", "size_mb": size_gb * 1024, "drive": drive, "last_played": played, **extra}


def test_drive_letter() -> None:
    assert drive_letter("F:") == "F:"
    assert drive_letter("f") == "F:"
    assert drive_letter("e:\\SteamLibrary") == "E:"
    assert drive_letter("") == ""
    assert drive_letter(None) == ""
    assert drive_letter("\\\\server\\share") == ""
    assert drive_letter("Fx") == ""


def test_drive_label_and_short() -> None:
    assert drive_label("F:", DRIVES) == "F: (SSD)"  # NVMe is not named
    assert drive_label("e", DRIVES) == "E: (HDD)"
    assert drive_label("G:", DRIVES) == "G: (SSD, USB)"
    assert drive_label("H:", DRIVES) == "H: (USB)"  # media unknown: only the bus
    assert drive_label("C:", DRIVES) == "C:"  # nothing known
    assert drive_label("Z:", DRIVES) == "Z:"  # not reported
    assert drive_label("F:") == "F:"
    assert drive_short("E:", DRIVES) == "E: HDD"
    assert drive_short("G:", DRIVES) == "G: SSD USB"
    assert drive_short("C:", DRIVES) == "C:"


def test_fit_rule_like_install_ps1() -> None:
    """needed = size + max(5 GB, 10 % of the size); the install job refuses when free < needed."""
    assert needed_gb(20 * GB) == 25  # the minimum reserve
    assert needed_gb(100 * GB) == 110  # 10 %
    assert fits(100 * GB, 110) is True
    assert fits(100 * GB, 109.9) is False
    assert fits(20 * GB, "25") is True
    assert fits(None, 500) is None  # size unknown
    assert fits(20 * GB, None) is None
    assert fits(20 * GB, "x") is None


def test_missing() -> None:
    assert round(missing_gb({"needed_gb": 73.4, "free_gb": 42}), 6) == 31.4
    assert missing_whole_gb({"needed_gb": 73.4, "free_gb": 42}) == 32  # rounded up
    assert missing_whole_gb({"needed_gb": 73, "free_gb": 42}) == 31
    assert missing_gb({"needed_gb": 10, "free_gb": 20}) == 0
    assert missing_gb({}) == 0


def test_candidates_only_single_games_on_the_full_drive() -> None:
    no_space = {"appid": "1", "needed_gb": 73, "free_gb": 42, "drive": "f:"}  # 31 GB missing
    library = [
        _game("10", 200, drive="E:"),  # another drive
        _game("11", 30),  # too small alone
        _game("12", 31),  # just enough
        _game("13", 120),
        _game("14", 60, drive="f:"),  # the letter in lower case counts too
        _game("15", 500, unavailable=True),  # its drive is not there
        _game("16", 0),  # size unknown
    ]
    found = deletion_candidates(library, no_space)
    assert [g["appid"] for g in found] == ["13", "14", "12"]  # largest first


def test_candidates_order_and_exclusions() -> None:
    no_space = {"appid": "1", "needed_gb": 30, "free_gb": 10, "drive": "F:"}  # 20 GB missing
    library = [
        _game("1", 300),  # the game without space itself
        _game("20", 100, played="2026-11-05"),  # played recently
        _game("21", 40, played="2026-01-01"),
        _game("22", 200),  # automatic update pending
        _game("23", 80),
        _game("24", 150),  # running
        _game("25", 250),  # in the job / queue / after_game
        _game("26", 25),
    ]
    found = deletion_candidates(
        library, no_space, exclude={"24", "25"}, recent_since="2026-10-28", auto_pending={"22"}
    )
    assert [g["appid"] for g in found] == ["23", "21", "26"]  # at most three
    # Fewer old games: the recent one comes next, the one with a pending update last
    found = deletion_candidates(
        [library[i] for i in (1, 2, 3)], no_space, recent_since="2026-10-28", auto_pending={"22"}
    )
    assert [g["appid"] for g in found] == ["21", "20", "22"]


def test_no_candidates() -> None:
    library = [_game("10", 30), _game("11", 25)]
    # No single game frees enough (no combinations)
    assert deletion_candidates(library, {"appid": "1", "needed_gb": 80, "free_gb": 20, "drive": "F:"}) == []
    # No drive in the entry
    assert deletion_candidates(library, {"appid": "1", "needed_gb": 30, "free_gb": 20}) == []


def test_frees_enough_alone_ignores_the_exclusions() -> None:
    no_space = {"appid": "1", "needed_gb": 73, "free_gb": 42, "drive": "F:"}  # 31 GB missing
    # The only fitting game is running or held: no candidate, but one game alone would free enough
    library = [_game("1", 300), _game("10", 30), _game("11", 40), _game("12", 500, drive="E:")]
    assert deletion_candidates(library, no_space, exclude={"11"}) == []
    assert frees_enough_alone(library, no_space) is True
    # Certain: every other game on the drive is known and too small
    assert frees_enough_alone([_game("1", 300), _game("10", 30), _game("13", 99, unavailable=True)], no_space) is False
    # Unknown: no library, a game of unknown size on the drive, or no drive in the entry
    assert frees_enough_alone([], no_space) is None
    assert frees_enough_alone([_game("10", 30), _game("14", 0)], no_space) is None
    assert frees_enough_alone(library, {"appid": "1", "needed_gb": 73, "free_gb": 42}) is None


def test_move_needed_and_targets() -> None:
    """A move needs the game and its workshop content plus the reserve of an install (of the game
    alone), as move.ps1 counts it; the drives it fits on are the others, those with a library first,
    then an SSD, otherwise in the PC's order."""
    assert move_needed_gb(_game("20", 60)) == 66
    assert move_needed_gb(_game("20", 60, workshop_mb=2048)) == 68
    assert move_needed_gb(_game("20", 200)) == 220
    assert move_needed_gb(_game("20", 0)) is None
    assert [d["drive"] for d in move_targets(_game("20", 60), DRIVES)] == ["E:", "G:", "H:"]
    # 99 GB: H: (90 GB free) is too small; C: is never big enough here
    assert [d["drive"] for d in move_targets(_game("30", 90), DRIVES)] == ["E:", "G:"]
    # From E: the F: library (42 GB free) is too small for 66 GB
    assert [d["drive"] for d in move_targets(_game("20", 60, drive="E:"), DRIVES)] == ["G:", "H:"]
    # Its drive unknown or not there, its size unknown: nowhere
    assert move_targets(_game("20", 60, drive=""), DRIVES) == []
    assert move_targets(_game("20", 60, unavailable=True), DRIVES) == []
    assert move_targets(_game("20", 0), DRIVES) == []
    assert move_targets(_game("20", 60), [{"drive": "E:", "free_gb": None}, {"drive": "G:", "free_gb": "x"}]) == []


def test_all_candidates_without_the_limit() -> None:
    library = [_game(str(i), 100 + i) for i in range(5)]
    no_space = {"appid": "40", "needed_gb": 66, "free_gb": 42, "drive": "F:"}
    assert len(deletion_candidates(library, no_space)) == 3
    assert [g["appid"] for g in deletion_candidates(library, no_space, limit=None)] == ["4", "3", "2", "1", "0"]


async def test_drives_of(hass: HomeAssistant) -> None:
    assert drives_of(hass) == []
    hass.states.async_set(LIBRARY_FREE, "812", {"drives": [{"drive": "f:", "free_gb": 42}, "junk", {"drive": ""}]})
    assert drives_of(hass) == [{"drive": "F:", "free_gb": 42}]
    hass.states.async_set(LIBRARY_FREE, "812", {})
    assert drives_of(hass) == []
    # A named PC reads its own sensor
    kids = PcNames("kinderpc", "Kinder-PC")
    assert drives_of(hass, kids) == []
    hass.states.async_set("sensor.steam_pc_kinderpc_library_free", "100", {"drives": [{"drive": "D:", "free_gb": 100}]})
    assert drives_of(hass, kids) == [{"drive": "D:", "free_gb": 100}]
