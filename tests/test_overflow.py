"""The overflow drive (steam_jobs.py): a new purchase installed by itself or by the button of the
purchase push that found no space on the default drive, or found that drive not there, goes to the
overflow drive once, at once; the result push of that end leaves it out, the next one says where it
went and why. Not for games asked for by hand, with a target, that do not fit there, or when the
overflow drive is the full one; a second time without space gives the push "not enough space"."""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.steam_engine.const import (
    CONF_NOTIFY_SERVICE,
    CONF_OVERFLOW_DRIVE,
    CONF_STEAM_LINK,
    SECTION_NOTIFY,
    SECTION_STEAM,
)
from custom_components.steam_engine.game_size import async_game_sizes
from custom_components.steam_engine.push import TAG_PROGRESS
from custom_components.steam_engine.space import GB
from custom_components.steam_engine.steam_jobs import Overflow

from .test_jobs_power import (  # noqa: F401 (setup_entry is a fixture)
    LIBRARY,
    PHONE,
    STATUS,
    Setup,
    _done,
    _installs,
    _pc_on,
    _settle,
    _store,
    setup_entry,
)
from .test_no_space import LINK, NO_SPACE_40, _job_without_space, _library_games, _pushes, _run, _sizes
from .test_space import DRIVES, LIBRARY_FREE


def _options(overflow: str = "e", **extra: Any) -> dict[str, Any]:
    return {
        SECTION_NOTIFY: {CONF_NOTIFY_SERVICE: PHONE},
        SECTION_STEAM: {CONF_STEAM_LINK: LINK, CONF_OVERFLOW_DRIVE: overflow},
        **extra,
    }


def _results(pushes: list[Any]) -> list[dict[str, Any]]:
    """The result pushes (TAG_PROGRESS with a View button; not the start of a job)."""
    return [p for p in _pushes(pushes, TAG_PROGRESS) if p["data"].get("actions")]


async def _next_job_ends(hass: HomeAssistant, appids: list[str], **lists: list[Any]) -> None:
    hass.states.async_set(STATUS, "started", {"appids": appids, "count": len(appids)})
    await _settle(hass)
    hass.states.async_set(STATUS, "done_with_errors" if lists.get("no_space") or lists.get("failed") else "done", _done(**lists))
    await _run(hass)


async def test_no_space_goes_to_the_overflow_drive_once(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """No space on F: for a new purchase: sent again to E: at once, the result push of that end does
    not say "not enough space" and no push "not enough space" comes; the next result says where it
    went. No space on E: either: the push "not enough space" (saying so), not a third try."""
    entry, pushes, publishes = await _job_without_space(
        hass, hass_storage, setup_entry, [NO_SPACE_40], options=_options(), lists={"purchased": ["40"]}
    )
    jobs = entry.runtime_data.jobs
    assert _installs(publishes) == ["install:40@e,"]
    assert jobs.targets == {"40": "e"}
    assert jobs.overflowed == {"40": Overflow("e", "F:", "no_space")}
    assert _pushes(pushes) == []  # no push "not enough space"
    [result] = _results(pushes)
    assert result["message"] == "G30 is up to date."

    await _next_job_ends(hass, ["40"], ok=["40"], new=["40"])
    result = _results(pushes)[-1]
    assert result["title"] == "Steam: Installation done"
    assert result["message"] == "Game 40 is installed on E: (HDD), there was no space on F: (SSD) (overflow drive)."
    assert jobs.overflowed == {}
    assert _installs(publishes) == ["install:40@e,"]


async def test_no_space_on_the_overflow_drive_either(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    entry, pushes, publishes = await _job_without_space(
        hass, hass_storage, setup_entry, [NO_SPACE_40], options=_options(), lists={"purchased": ["40"]}
    )
    full_e = {"appid": "40", "needed_gb": 66, "free_gb": 30, "drive": "E:"}
    await _next_job_ends(hass, ["40"], no_space=[full_e])
    assert _installs(publishes) == ["install:40@e,"]  # once only: it has a target now
    [push] = _pushes(pushes)
    assert push["title"] == "Steam: not enough space on E: (HDD)"
    assert push["message"].startswith(
        "Game 40 needs 66 GB (with reserve), 30 GB are free. "
        "It went to the overflow drive as there was no space on F: (SSD)."
    )
    # Only a game without space: no result push beside it (only the one of the first job)
    assert len(_results(pushes)) == 1
    assert entry.runtime_data.jobs.overflowed == {}


async def test_overflow_try_that_does_not_install_says_why(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """The try on the overflow drive is not started because a game ran: the result says why it went
    there and the note is kept; it then fails: the result says so again, then the note goes."""
    entry, pushes, _publishes = await _job_without_space(
        hass, hass_storage, setup_entry, [NO_SPACE_40], options=_options(), lists={"purchased": ["40"]}
    )
    jobs = entry.runtime_data.jobs
    note = "Game 40 went to E: (HDD) (overflow drive) as there was no space on F: (SSD)."

    await _next_job_ends(hass, ["40"], game_running=["40"])
    assert _results(pushes)[-1]["message"].endswith(note)
    assert jobs.overflowed == {"40": Overflow("e", "F:", "no_space")}

    await _next_job_ends(hass, ["40"], failed=["40"])
    result = _results(pushes)[-1]
    assert result["message"] == f"Failed: Game 40. {note}"
    assert jobs.overflowed == {}


async def test_overflow_drive_space_is_shared(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """Two purchases without space on F: that each fit on E: (812 GB free), but not both: only the
    first goes there, the second gets the push "not enough space"."""
    no_space = [NO_SPACE_40, {**NO_SPACE_40, "appid": "41"}]
    no_space = [{**k, "needed_gb": 500} for k in no_space]
    entry, pushes, publishes = await _job_without_space(
        hass,
        hass_storage,
        setup_entry,
        no_space,
        options=_options(),
        sizes={"40": 450 * GB, "41": 450 * GB},
        lists={"purchased": ["40", "41"]},
    )
    assert _installs(publishes) == ["install:40@e,"]
    assert entry.runtime_data.jobs.overflowed == {"40": Overflow("e", "F:", "no_space")}
    [push] = _pushes(pushes)
    assert push["title"] == "Steam: not enough space on F: (SSD)"
    assert "Game 41" in push["message"]
    assert "Game 40" not in push["message"]


@pytest.mark.parametrize(
    ("overflow", "lists", "no_space"),
    [
        ("e", {}, NO_SPACE_40),  # asked for by hand
        ("e", {"purchased": ["40"], "targets": {"40": "f"}}, NO_SPACE_40),  # with a target
        ("e", {"purchased": ["40"]}, {**NO_SPACE_40, "needed_gb": 900}),  # does not fit on E: either
        ("f", {"purchased": ["40"]}, NO_SPACE_40),  # the overflow drive is the full one
        ("z", {"purchased": ["40"]}, NO_SPACE_40),  # the overflow drive is not there
        ("", {"purchased": ["40"]}, NO_SPACE_40),  # no overflow drive
    ],
)
async def test_no_overflow(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    setup_entry: Setup,
    overflow: str,
    lists: dict[str, Any],
    no_space: dict[str, Any],
) -> None:
    """Nothing is sent again: the push "not enough space" comes as for any game."""
    entry, pushes, publishes = await _job_without_space(
        hass, hass_storage, setup_entry, [no_space], options=_options(overflow), lists=lists
    )
    assert _installs(publishes) == []
    assert entry.runtime_data.jobs.overflowed == {}
    [push] = _pushes(pushes)
    assert push["title"] == "Steam: not enough space on F: (SSD)"
    assert "overflow" not in push["message"]
    assert "Not enough space: Game 40" in _results(pushes)[-1]["message"]


async def test_default_drive_not_there_goes_to_the_overflow_drive(
    hass: HomeAssistant, hass_storage: dict[str, Any], setup_entry: Setup
) -> None:
    """The default drive is unplugged (drive_unavailable, no drive is the default): a new purchase
    goes to the overflow drive; nothing is told until its result."""
    pushes = async_mock_service(hass, "notify", PHONE)
    _store(hass_storage, job=["40"], purchased=["40"])
    _sizes(hass_storage, **{"40": 60 * GB})
    await async_game_sizes(hass)
    _pc_on(hass)
    library = _library_games()
    hass.states.async_set(LIBRARY, str(len(library)), {"games": library})
    hass.states.async_set(LIBRARY_FREE, "812", {"drives": [{**d, "default": False} for d in DRIVES if d["drive"] != "F:"]})
    entry = await setup_entry(_options())
    publishes = async_mock_service(hass, "mqtt", "publish")
    hass.states.async_set(STATUS, "started", {"appids": ["40"], "count": 1})
    await _settle(hass)
    hass.states.async_set(
        STATUS,
        "error",
        {"appid": "40", "position": 1, "count": 1, "reason": "not available", "reason_code": "drive_unavailable"},
    )
    await _settle(hass)
    hass.states.async_set(STATUS, "done_with_errors", _done(failed=["40"]))
    await _run(hass)
    assert _installs(publishes) == ["install:40@e,"]
    assert entry.runtime_data.jobs.overflowed == {"40": Overflow("e", "", "drive_unavailable")}
    assert _results(pushes) == []  # no "failed"

    await _next_job_ends(hass, ["40"], ok=["40"], new=["40"])
    assert _results(pushes)[-1]["message"] == (
        "Game 40 is installed on E: (HDD), the default drive was not available (overflow drive)."
    )


async def test_purchase_marks(hass: HomeAssistant, setup_entry: Setup) -> None:
    """New purchases (automatic or by the button of the purchase push) are marked while open; the
    same game asked for by hand loses the mark, an automatic update leaves it."""
    _pc_on(hass)
    entry = await setup_entry(_options())
    jobs = entry.runtime_data.jobs
    async_mock_service(hass, "mqtt", "publish")
    hass.states.async_set(STATUS, "started", {"appids": ["99"], "count": 1})
    await _settle(hass, 3)

    await jobs.async_install(["40"], purchase=True)
    await jobs.async_install(["41"], automatic=True, purchase=True)
    assert jobs.queue == ["40", "41"]
    assert jobs.purchased == ["40", "41"]
    await jobs.async_install(["41"], automatic=True)
    assert jobs.purchased == ["40", "41"]
    await jobs.async_install(["40"])
    assert jobs.purchased == ["41"]
    # Gone with the game from the queue
    jobs._put("queue", [])  # noqa: SLF001
    assert jobs.purchased == []
