"""Buttons that install, uninstall or start the game picked in the matching select (select.py).

They start the same jobs as the actions steam_engine.install, steam_engine.uninstall and
steam_engine.launch, without waiting for the PC to wake (up to 6 min), and clear the pick. The job
belongs to the config entry: a reload cancels it, and its errors are logged (parts.start_task).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    DOMAIN,
    ENTITY_INSTALL_GAME,
    ENTITY_LAUNCH_GAME,
    ENTITY_UNINSTALL_GAME,
    KEY_INSTALL_GAME,
    KEY_LAUNCH_GAME,
    KEY_UNINSTALL_GAME,
)
from .entity import SteamEngineEntity
from .game_choice import PICK_INSTALL, PICK_INSTALLED
from .parts import start_task
from .steam_jobs import SteamJobs

Action = Callable[[SteamJobs, str], Coroutine[Any, Any, Any]]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(
        [
            GameButton(
                entry, KEY_INSTALL_GAME, PICK_INSTALL, ENTITY_INSTALL_GAME, "mdi:download",
                lambda jobs, appid: jobs.async_install([appid]),
            ),
            GameButton(
                entry, KEY_UNINSTALL_GAME, PICK_INSTALLED, ENTITY_UNINSTALL_GAME, "mdi:trash-can-outline",
                lambda jobs, appid: jobs.async_uninstall(appid),
            ),
            LaunchButton(entry),
        ]
    )


class GameButton(SteamEngineEntity, ButtonEntity):
    """install_game: installs the game picked in game_to_install; uninstall_game: uninstalls the
    game picked in installed_game."""

    def __init__(self, entry: ConfigEntry, key: str, pick: str, entity_id: str, icon: str, action: Action) -> None:
        super().__init__(entry, key, entity_id)
        self._entry = entry
        self._choice = entry.runtime_data.choice
        self._jobs = entry.runtime_data.jobs
        self._key = key
        self._pick = pick
        self._action = action
        self._attr_icon = icon

    def _check(self) -> None:
        """Raise when the job cannot start. Said here, as the job runs in the background and its
        answer would be lost."""

    async def async_press(self) -> None:
        appid = self._choice.get(self._pick)
        if appid is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_game_picked")
        self._check()
        self._choice.set(self._pick, None)
        start_task(self.hass, self._entry, self._action(self._jobs, appid), self._key)


class LaunchButton(GameButton):
    """launch_game: starts the game picked in installed_game; not while a game runs or a game is
    being moved."""

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(
            entry, KEY_LAUNCH_GAME, PICK_INSTALLED, ENTITY_LAUNCH_GAME, "mdi:play",
            lambda jobs, appid: jobs.async_launch(appid),
        )

    def _check(self) -> None:
        if self._jobs.game_running():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="game_running")
        if self._jobs.move.active:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="moving")
