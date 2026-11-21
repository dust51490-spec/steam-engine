"""Space on the drives of a PC: the drives the agent reports, how they are named in pushes, whether a
game fits, and which installed game to delete when one did not fit.

The drives come from the attribute "drives" of the agent's free space sensor (library_free,
PROTOCOL.md): one entry per drive letter with free_gb, library, default, bus and media. The sensor is
looked up in the entity registry like the card does it (websocket.py); it is not one of
AGENT_ENTITIES, so a PC side without it raises no "agent missing" hint.
"""

from __future__ import annotations

from collections.abc import Iterable
import math
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .agent_entities import PLATFORM
from .pc import UNNAMED, PcNames
from .values import number

GB = 1024**3  # the PC counts in GiB (1GB in PowerShell)
# The reserve of the install job's space check: 10 % of the game size, at least 5 GB. Must match
# $spaceReserveShare and $spaceReserveMin in pc/jobs/install.ps1 (and the dashboard card).
RESERVE_SHARE = 0.10
RESERVE_MIN_GB = 5.0
SUGGEST_LIMIT = 3  # deletion candidates at most (three buttons on Android)


def drive_letter(value: Any) -> str:
    """A drive as the PC names it in upper case with a colon ("F:"), from "f", "F:", "f:\\..." or "";
    "" when there is no letter."""
    text = str(value or "").strip()
    if not text or not text[0].isalpha() or not text[0].isascii() or (len(text) > 1 and text[1] != ":"):
        return ""
    return text[0].upper() + ":"


def drives_of(hass: HomeAssistant, names: PcNames = UNNAMED) -> list[dict[str, Any]]:
    """The drives the PC reported (attribute drives of its library_free sensor), each with "drive" in
    upper case; [] while the sensor or the attribute is not there."""
    unique_id, default_entity_id = names.agent_entity("library_free")
    domain = default_entity_id.split(".", 1)[0]
    entity_id = er.async_get(hass).async_get_entity_id(domain, PLATFORM, unique_id) or default_entity_id
    state = hass.states.get(entity_id)
    drives = state.attributes.get("drives") if state is not None else None
    if not isinstance(drives, list):
        return []
    found = []
    for item in drives:
        if isinstance(item, dict) and drive_letter(item.get("drive")):
            found.append({**item, "drive": drive_letter(item.get("drive"))})
    return found


def find_drive(drives: Iterable[dict[str, Any]], drive: Any) -> dict[str, Any] | None:
    """The entry of a drive letter ("f", "F:"), None when the PC does not report it."""
    letter = drive_letter(drive)
    return next((d for d in drives if letter and d.get("drive") == letter), None)


def kinds_of(entry: dict[str, Any] | None) -> list[str]:
    """SSD or HDD, and USB for a disk on USB; only USB when the media is unknown."""
    if not entry:
        return []
    media = str(entry.get("media") or "").lower()
    kinds = [media.upper()] if media in ("ssd", "hdd") else []
    if str(entry.get("bus") or "").lower() == "usb":
        kinds.append("USB")
    return kinds


def drive_label(drive: Any, drives: Iterable[dict[str, Any]] = ()) -> str:
    """A drive for push texts: "F: (SSD)", "G: (HDD, USB)", "G: (USB)" when the media is unknown,
    only "F:" when nothing is known (or the drive is not reported)."""
    letter = drive_letter(drive) or str(drive or "")
    kinds = kinds_of(find_drive(drives, letter))
    return f"{letter} ({', '.join(kinds)})" if kinds else letter


def drive_short(drive: Any, drives: Iterable[dict[str, Any]] = ()) -> str:
    """A drive for a push button, short: "E: HDD", "G: SSD USB", "F:"."""
    letter = drive_letter(drive) or str(drive or "")
    return " ".join([letter, *kinds_of(find_drive(drives, letter))])


def needed_gb(size_bytes: float) -> float:
    """Space a first install of this size needs, with the reserve (install.ps1: the missing part of
    the game, all of it for a first install, plus max(5 GB, 10 % of the size))."""
    size_gb = size_bytes / GB
    return size_gb + max(RESERVE_MIN_GB, size_gb * RESERVE_SHARE)


def move_needed_gb(game: dict[str, Any]) -> float | None:
    """Space moving an installed game needs on the target drive, as move.ps1 counts it (PROTOCOL.md
    "Move"): size_mb + workshop_mb of the library plus the reserve of an install, max(5 GB, 10 % of
    size_mb); None when the size is not known."""
    size_gb = number(game.get("size_mb")) / 1024
    if size_gb <= 0:
        return None
    return size_gb + max(0.0, number(game.get("workshop_mb"))) / 1024 + max(RESERVE_MIN_GB, size_gb * RESERVE_SHARE)


def drive_rank(drive: dict[str, Any]) -> tuple[int, int]:
    """The order in which drives are offered: those with a Steam library before those without one
    (the PC creates one there), an SSD before others; sorted stably, so the PC's order stays next."""
    return (0 if drive.get("library") else 1, 0 if str(drive.get("media") or "").lower() == "ssd" else 1)


def move_targets(game: dict[str, Any], drives: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The drives an installed game fits on when moved (move_needed_gb), not its own drive, in the
    order of drive_rank; [] when its size or its drive is not known or it is on a drive that is
    not there."""
    needed = move_needed_gb(game)
    own = drive_letter(game.get("drive"))
    if needed is None or not own or game.get("unavailable"):
        return []
    fitting = []
    for drive in drives:
        free = drive.get("free_gb")
        if drive.get("drive") == own or free is None:
            continue
        try:
            if float(free) >= needed:
                fitting.append(drive)
        except (TypeError, ValueError):
            continue
    return sorted(fitting, key=drive_rank)


def fits(size_bytes: float | None, free_gb: Any) -> bool | None:
    """Whether a first install of this size fits into free_gb (install.ps1 refuses when free is less
    than needed); None when the size or the free space is not known."""
    if size_bytes is None or free_gb is None:
        return None
    try:
        free = float(free_gb)
    except (TypeError, ValueError):
        return None
    return free >= needed_gb(size_bytes)


def missing_gb(no_space: dict[str, Any]) -> float:
    """What a no_space entry of the PC lacks: needed_gb (with the reserve) minus free_gb."""
    return max(0.0, number(no_space.get("needed_gb")) - number(no_space.get("free_gb")))


def missing_whole_gb(no_space: dict[str, Any]) -> int:
    """missing_gb rounded up for texts, so it never says less than lacks."""
    return math.ceil(round(missing_gb(no_space), 3))


def deletion_candidates(
    library: Iterable[dict[str, Any]],
    no_space: dict[str, Any],
    *,
    exclude: Iterable[str] = (),
    recent_since: str = "",
    auto_pending: Iterable[str] = (),
    limit: int | None = SUGGEST_LIMIT,
) -> list[dict[str, Any]]:
    """Installed games whose removal alone frees enough on the drive of a no_space entry: only games
    on that drive (upper-case letters compared), each at least as big as what lacks. The largest
    first; games last played on or after recent_since (YYYY-MM-DD) after them, games with an
    automatic update pending last. Never a game of exclude (the running game, those a job, the
    queue or after_game holds, the games without space), nor one on a drive that is not there
    (unavailable). At most limit (None: all); [] when none frees enough alone (no combinations), or
    the entry has no drive. The games to move off the drive instead are the same in the same order,
    those that fit on another drive (move_targets)."""
    letter = drive_letter(no_space.get("drive"))
    if not letter:
        return []
    lacking = missing_gb(no_space)
    skip = {str(i) for i in exclude} | {str(no_space.get("appid"))}
    pending = {str(i) for i in auto_pending}
    found = []
    for game in library:
        appid = str(game.get("appid"))
        if appid in skip or game.get("unavailable") or drive_letter(game.get("drive")) != letter:
            continue
        size_gb = number(game.get("size_mb")) / 1024
        if size_gb <= 0 or size_gb < lacking:
            continue
        recent = bool(recent_since) and str(game.get("last_played") or "") >= recent_since
        found.append(((appid in pending, recent, -size_gb), game))
    found.sort(key=lambda pair: pair[0])
    return [game for _, game in (found if limit is None else found[:limit])]


def frees_enough_alone(library: Iterable[dict[str, Any]], no_space: dict[str, Any]) -> bool | None:
    """Whether some installed game on the drive of a no_space entry would free enough by itself,
    without the exclusions of deletion_candidates (running, held by a job, the queue or after_game).
    False only when that is certain: the library is known and every other game on that drive has a
    known size below what lacks. None when it cannot be told (no drive, no library, a game on that
    drive of unknown size)."""
    letter = drive_letter(no_space.get("drive"))
    games = list(library)
    if not letter or not games:
        return None
    lacking = missing_gb(no_space)
    own = str(no_space.get("appid"))
    unknown = False
    for game in games:
        if str(game.get("appid")) == own or game.get("unavailable") or drive_letter(game.get("drive")) != letter:
            continue
        size_gb = number(game.get("size_mb")) / 1024
        if size_gb <= 0:
            unknown = True
        elif size_gb >= lacking:
            return True
    return None if unknown else False
