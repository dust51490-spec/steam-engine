"""Plumbing shared by the logic parts (pc_power.py, steam_jobs.py, ...): listeners, subscriptions
that end on unload, jobs started without waiting, and waiting for a condition."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)

POLL_SECONDS = 2  # wait_until looks again this often


class Part:
    """A logic part: the entities that show its state listen to it, and its subscriptions end with
    async_stop (on unload)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._unsubs: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []

    @callback
    def async_start(self) -> None:
        """Subscribe; called once Home Assistant runs, so the watched entities have their states."""

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            self._listeners.remove(listener)

        return remove

    @callback
    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()


async def _logged[T](target: Coroutine[Any, Any, T], name: str) -> T | None:
    """A job nobody waits for: say when a reload or unload stops it, and log its errors, which
    would otherwise go unseen."""
    try:
        return await target
    except asyncio.CancelledError:
        _LOGGER.warning("%s stopped: Steam Engine was reloaded or unloaded", name)
        raise
    except Exception:
        _LOGGER.exception("%s failed", name)
        return None


@callback
def start_task(
    hass: HomeAssistant, entry: ConfigEntry, target: Coroutine[Any, Any, Any], name: str, eager: bool = True
) -> asyncio.Task[Any]:
    """Start a job without waiting for it. It belongs to the config entry, so a reload or unload
    cancels it instead of leaving it running next to the new instance."""
    return entry.async_create_background_task(
        hass, _logged(target, f"Steam Engine {name}"), f"steam_engine {name}", eager_start=eager
    )


async def wait_until(
    check: Callable[[], bool],
    timeout: timedelta,
    while_waiting: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> bool:
    """Wait until check() is true, looking every 2 s; False when the timeout ends first.
    while_waiting runs before each pause."""
    try:
        async with asyncio.timeout(timeout.total_seconds()):
            while not check():
                if while_waiting is not None:
                    await while_waiting()
                await asyncio.sleep(POLL_SECONDS)
    except TimeoutError:
        return check()
    return True
