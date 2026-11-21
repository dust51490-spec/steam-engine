"""The games picked in the two selects (select.py), read by the buttons (button.py) and the auto
update switch of the picked game (switch.py).

A pick lives in memory only and ends after PICK_TIMEOUT, when a button starts its job, when the game
leaves its list, and on a restart of HA or a reload of the integration. So a pick made and
forgotten cannot be acted on days later (the Press button on the device page asks nothing).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import KEY_GAME_TO_INSTALL, KEY_INSTALLED_GAME

# Keys of the two picks, also the keys (translation, unique ID) of the selects
PICK_INSTALL = KEY_GAME_TO_INSTALL
PICK_INSTALLED = KEY_INSTALLED_GAME
PICK_TIMEOUT = timedelta(minutes=10)


class GameChoice:
    """App ID picked per select, or None."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._picks: dict[str, str | None] = {PICK_INSTALL: None, PICK_INSTALLED: None}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._listeners: list[Callable[[], None]] = []

    def get(self, key: str) -> str | None:
        return self._picks[key]

    @callback
    def set(self, key: str, appid: str | None) -> None:
        if (cancel := self._timers.pop(key, None)) is not None:
            cancel()
        if appid is not None:

            @callback
            def expire(_now: datetime) -> None:
                self._timers.pop(key, None)
                self.set(key, None)

            self._timers[key] = async_call_later(self.hass, PICK_TIMEOUT, expire)
        if self._picks[key] != appid:
            self._picks[key] = appid
            for listener in list(self._listeners):
                listener()

    @callback
    def async_stop(self) -> None:
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            self._listeners.remove(listener)

        return remove
