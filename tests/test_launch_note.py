"""Starting a game on a switched-off PC without the lock at logon: the note for the lock script
(steam/launch_pending, retained). Only with the option on and only when the start has to wake the
PC is it sent, before the PC is woken; it is cleared once the command was sent, when the agent does
not come, after 10 minutes at the latest and at the start of the integration. A start while the PC
is on, a manual wake and other jobs never send it.

The agent's entities are plain states (their default entity IDs). The wake entity is a service of
the test that brings the agent up (or not), and every MQTT publish and wake is recorded in one list,
so the order shows. The 2 s polls of the wait and the pause after booting are made short; the
10 minutes move with async_fire_time_changed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from datetime import datetime, timedelta
import json
from typing import Any
from unittest.mock import patch

import pytest

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.steam_engine.const import (
    CONF_LAUNCH_UNLOCKED,
    CONF_WAKE_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
    LAUNCH_PENDING_TOPIC,
    SECTION_PC,
    merged_options,
)

# Default entity IDs of the agent
AGENT_ONLINE = "binary_sensor.steam_pc_agent_online"
GAME_RUNNING = "binary_sensor.steam_pc_game_running"
NETWORK = "sensor.steam_pc_network"
LAST_INPUT = "sensor.steam_pc_last_input"
SHUTDOWN = "button.steam_pc_shutdown"
WAKE = "input_button.wake_pc"

Setup = Callable[..., Awaitable[MockConfigEntry]]


def _pc_on(hass: HomeAssistant) -> None:
    hass.states.async_set(NETWORK, "100", {"unit_of_measurement": "B/s"})
    hass.states.async_set(AGENT_ONLINE, "on")
    hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
    hass.states.async_set(SHUTDOWN, "unknown")
    hass.states.async_set(LAST_INPUT, (dt_util.utcnow() - timedelta(hours=2)).isoformat())


def _pc_off(hass: HomeAssistant) -> None:
    hass.states.async_set(AGENT_ONLINE, "off")
    hass.states.async_set(NETWORK, "unavailable")
    hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
    hass.states.async_set(SHUTDOWN, "unavailable")


def _options(unlocked: bool) -> dict[str, Any]:
    return {SECTION_PC: {CONF_WAKE_ENTITY: WAKE, CONF_LAUNCH_UNLOCKED: unlocked}}


class Recorder:
    """MQTT publishes and wakes in the order they happened. The wake brings the agent up unless
    agent_comes is False."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.events: list[tuple[str, Any]] = []
        self.agent_comes = True
        # This many clears of the note fail (the broker away), not recorded in events
        self.failing_clears = 0
        self.failed_clears = 0
        hass.services.async_register("mqtt", "publish", self._publish)
        hass.services.async_register("input_button", "press", self._wake)

    async def _publish(self, call: ServiceCall) -> None:
        if call.data["topic"] == LAUNCH_PENDING_TOPIC and not call.data["payload"] and self.failing_clears:
            self.failing_clears -= 1
            self.failed_clears += 1
            raise HomeAssistantError("broker away")
        self.events.append(("mqtt", dict(call.data)))

    async def _wake(self, _call: ServiceCall) -> None:
        self.events.append(("wake", None))
        if self.agent_comes:
            _pc_on(self.hass)

    def notes(self) -> list[dict[str, Any]]:
        """What was sent on the note topic."""
        return [data for kind, data in self.events if kind == "mqtt" and data["topic"] == LAUNCH_PENDING_TOPIC]

    def order(self) -> list[str]:
        """The events in short: note, clear, wake, launch:<appid> (other commands are left out)."""
        result = []
        for kind, data in self.events:
            if kind == "wake":
                result.append("wake")
            elif data["topic"] == LAUNCH_PENDING_TOPIC:
                result.append("note" if data["payload"] else "clear")
            elif str(data["payload"]).startswith("launch:"):
                result.append(str(data["payload"]))
        return result


@pytest.fixture(autouse=True)
def short_waits() -> Generator[None]:
    with (
        patch("custom_components.steam_engine.parts.POLL_SECONDS", 0.01),
        patch("custom_components.steam_engine.pc_power.BOOT_PAUSE", 0),
    ):
        yield


@pytest.fixture
async def setup_entry(hass: HomeAssistant) -> AsyncGenerator[Setup]:
    entries: list[MockConfigEntry] = []

    async def setup(options: dict[str, Any] | None = None) -> MockConfigEntry:
        entry = MockConfigEntry(domain=DOMAIN, data={}, options=merged_options(options or {}))
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        entries.append(entry)
        return entry

    yield setup
    for entry in entries:
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_option_off_by_default() -> None:
    assert DEFAULT_OPTIONS[SECTION_PC][CONF_LAUNCH_UNLOCKED] is False


async def test_note_before_the_wake_and_cleared_after_the_launch(hass: HomeAssistant, setup_entry: Setup) -> None:
    _pc_off(hass)
    rec = Recorder(hass)
    entry = await setup_entry(_options(True))
    assert rec.order() == ["clear"]  # a stale note of an earlier run goes at the start
    rec.events.clear()

    before = dt_util.utcnow()
    result = await entry.runtime_data.jobs.async_launch("1234")
    assert result == {"sent": True}
    assert rec.order() == ["note", "wake", "launch:1234", "clear"]
    note = rec.notes()[0]
    assert note["retain"] is True
    assert note["qos"] == 1
    payload = json.loads(note["payload"])
    assert payload["appid"] == "1234"
    assert payload["until"].endswith("Z")
    until = datetime.fromisoformat(payload["until"].replace("Z", "+00:00"))
    assert timedelta(minutes=9) < until - before <= timedelta(minutes=10, seconds=1)
    assert rec.notes()[1] == {"topic": LAUNCH_PENDING_TOPIC, "payload": "", "qos": 1, "retain": True}

    # The timer went with the clear: no further note after 10 minutes
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(rec.notes()) == 2


async def test_no_note_without_the_option(hass: HomeAssistant, setup_entry: Setup) -> None:
    _pc_off(hass)
    rec = Recorder(hass)
    entry = await setup_entry(_options(False))
    rec.events.clear()
    assert await entry.runtime_data.jobs.async_launch("1234") == {"sent": True}
    assert rec.order() == ["wake", "launch:1234"]


async def test_no_note_while_the_pc_is_on(hass: HomeAssistant, setup_entry: Setup) -> None:
    _pc_on(hass)
    rec = Recorder(hass)
    entry = await setup_entry(_options(True))
    rec.events.clear()
    assert await entry.runtime_data.jobs.async_launch("1234") == {"sent": True}
    assert rec.order() == ["launch:1234"]


async def test_no_note_for_a_manual_wake_or_an_install(hass: HomeAssistant, setup_entry: Setup) -> None:
    _pc_off(hass)
    rec = Recorder(hass)
    entry = await setup_entry(_options(True))
    rec.events.clear()
    await hass.services.async_call(DOMAIN, "pc_wake", {}, blocking=True, return_response=True)
    assert rec.order() == ["wake"]

    _pc_off(hass)
    await hass.async_block_till_done()
    rec.events.clear()
    with patch("custom_components.steam_engine.steam_jobs.SEND_WAIT", timedelta(seconds=0.05)):
        await entry.runtime_data.jobs.async_install(["20"])
    assert "wake" in rec.order()
    assert rec.notes() == []


async def test_note_cleared_when_the_agent_does_not_come(hass: HomeAssistant, setup_entry: Setup) -> None:
    _pc_off(hass)
    rec = Recorder(hass)
    rec.agent_comes = False
    entry = await setup_entry(_options(True))
    rec.events.clear()
    with patch("custom_components.steam_engine.steam_jobs.WAKE_TIMEOUT", timedelta(seconds=0.2)):
        result = await entry.runtime_data.jobs.async_launch("1234")
    assert result == {"sent": False, "reason": "agent_offline"}
    assert rec.order() == ["note", "wake", "clear"]


async def test_note_cleared_after_ten_minutes(hass: HomeAssistant, setup_entry: Setup) -> None:
    """The start still waits for the agent: the note goes after 10 minutes anyway."""
    _pc_off(hass)
    rec = Recorder(hass)
    rec.agent_comes = False
    entry = await setup_entry(_options(True))
    rec.events.clear()
    with patch("custom_components.steam_engine.steam_jobs.WAKE_TIMEOUT", timedelta(hours=1)):
        launch = asyncio.create_task(entry.runtime_data.jobs.async_launch("1234"))
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert rec.order() == ["note", "wake"]

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=10, seconds=1))
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert [bool(n["payload"]) for n in rec.notes()] == [True, False]
        launch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await launch


async def test_stale_note_cleared_at_the_start_without_the_option(hass: HomeAssistant, setup_entry: Setup) -> None:
    """The option switched off while a note was left: the start clears it anyway."""
    _pc_on(hass)
    rec = Recorder(hass)
    await setup_entry(_options(False))
    assert rec.order() == ["clear"]


async def test_failed_clear_is_tried_again(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A clear the broker did not take is sent again a minute later, not left until the note ends."""
    _pc_off(hass)
    rec = Recorder(hass)
    entry = await setup_entry(_options(True))
    rec.events.clear()
    rec.failing_clears = 1
    assert await entry.runtime_data.jobs.async_launch("1234") == {"sent": True}
    assert rec.order() == ["note", "wake", "launch:1234"]
    assert rec.failed_clears == 1

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1, seconds=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert rec.order() == ["note", "wake", "launch:1234", "clear"]

    # Cleared: no further tries
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert rec.order() == ["note", "wake", "launch:1234", "clear"]


async def test_failed_clears_stop_when_the_note_ends(
    hass: HomeAssistant, setup_entry: Setup, freezer: FrozenDateTimeFactory
) -> None:
    """The tries go on only while the note is valid: after its "until" the lock script ignores it."""
    _pc_off(hass)
    rec = Recorder(hass)
    entry = await setup_entry(_options(True))
    rec.events.clear()
    rec.failing_clears = 1000
    assert await entry.runtime_data.jobs.async_launch("1234") == {"sent": True}
    assert rec.failed_clears == 1

    for _ in range(12):
        freezer.tick(timedelta(minutes=1, seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    tries = rec.failed_clears
    assert 5 < tries <= 12

    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert rec.failed_clears == tries


async def test_failed_clear_at_the_start_is_tried_again(hass: HomeAssistant, setup_entry: Setup) -> None:
    """A note an earlier run left may still be valid: a clear at the start that fails is tried again."""
    _pc_on(hass)
    rec = Recorder(hass)
    rec.failing_clears = 1
    await setup_entry(_options(False))
    assert rec.order() == []
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1, seconds=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert rec.order() == ["clear"]
