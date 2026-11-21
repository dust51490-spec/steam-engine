"""Small readers for values in states and messages of the PC (numbers, lists of app IDs, game names),
shared by the logic parts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, TypeVar

_D = TypeVar("_D")


def number(value: Any, default: _D = 0.0) -> float | _D:  # type: ignore[assignment]
    """The value as a float, the default for anything that is no number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def whole(value: float) -> int:
    """Round half up, like Jinja's round(0), not Python's half to even."""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def id_list(value: Any) -> list[str]:
    """App IDs from a list, or a single value, as text; empty for None and ''."""
    if isinstance(value, (list, tuple)):
        return [str(i) for i in value]
    return [str(value)] if value not in (None, "") else []


def known_name(games: Iterable[Mapping[str, Any]], appid: Any) -> str | None:
    """The name of the game with this app ID in a list of games (library, owned), None if not there."""
    for game in games:
        if str(game.get("appid")) == str(appid) and game.get("name"):
            return str(game["name"])
    return None
