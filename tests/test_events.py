"""The events for automations of one's own (steam_events.py): the job end with the games sorted by
outcome, an aborted job with what is still open, an end read again after a reconnect is not
fired (the end of the next job with the same state is), games that start and end (not over an
MQTT reconnect), and updates that are new since the last check."""

from __future__ import annotations

from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.steam_engine.const import DOMAIN, ENTITY_CURRENT_GAME
from custom_components.steam_engine.steam_events import (
    EVENT_GAME_ENDED,
    EVENT_GAME_STARTED,
    EVENT_JOB_DONE,
    EVENT_UPDATES_FOUND,
)

from .test_jobs_power import (  # noqa: F401 (setup_entry is a fixture)
    AGENT_ONLINE,
    GAME_RUNNING,
    LIBRARY,
    STATUS,
    Setup,
    _build_answer,
    _done,
    _pc_on,
    _settle,
    setup_entry,
)


def _catch(hass: HomeAssistant, event_type: str) -> list[dict[str, Any]]:
    caught: list[dict[str, Any]] = []

    @callback
    def listener(event: Event) -> None:
        data = dict(event.data)
        assert data.pop("pc") == ""  # the PC without a name
        caught.append(data)

    hass.bus.async_listen(event_type, listener)
    return caught


def _library(hass: HomeAssistant, *games: tuple[str, str]) -> None:
    hass.states.async_set(
        LIBRARY,
        str(len(games)),
        {"games": [{"appid": a, "name": n, "buildid": "1", "size_mb": 100} for a, n in games]},
    )


async def test_job_done(hass: HomeAssistant, setup_entry: Setup) -> None:
    _pc_on(hass)
    _library(hass, ("10", "Alpha"), ("20", "Beta"))
    await setup_entry()
    async_mock_service(hass, "mqtt", "publish")
    done = _catch(hass, EVENT_JOB_DONE)

    hass.states.async_set(STATUS, "started", {"appids": ["10", "20", "30", "40"], "count": 4})
    await _settle(hass)
    hass.states.async_set(STATUS, "error", {"appid": "20", "reason_code": "disk_write"})
    await _settle(hass)
    no_space = [{"appid": "40", "needed_gb": 50, "free_gb": 10, "drive": "F:"}]
    hass.states.async_set(
        STATUS, "done_with_errors", _done(ok=["10", "30"], new=["30"], failed=["20"], no_space=no_space)
    )
    await _settle(hass)

    assert len(done) == 1
    event = done[0]
    assert event["result"] == "done_with_errors"
    assert event["updated"] == [{"appid": "10", "name": "Alpha"}]
    assert event["installed"] == [{"appid": "30", "name": "Game 30"}]
    assert event["failed"] == [{"appid": "20", "name": "Beta", "reason": "disk_write"}]
    assert event["no_space"] == [{"appid": "40", "name": "Game 40", "needed_gb": 50, "free_gb": 10, "drive": "F:"}]
    assert event["open"] == []


async def test_aborted_job_and_replayed_end(hass: HomeAssistant, setup_entry: Setup) -> None:
    """An aborted job names the games still to do; a retained end read again on a reconnect is no
    new end."""
    _pc_on(hass)
    _library(hass, ("10", "Alpha"))
    await setup_entry()
    async_mock_service(hass, "mqtt", "publish")
    done = _catch(hass, EVENT_JOB_DONE)

    await hass.services.async_call(DOMAIN, "install", {"appid": "10"}, blocking=True)
    await _settle(hass)
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1})
    await _settle(hass, 3)
    hass.states.async_set(STATUS, "aborted", {"reason": "The job was interrupted"})
    await _settle(hass)
    assert len(done) == 1
    assert done[0]["result"] == "aborted"
    assert done[0]["open"] == [{"appid": "10", "name": "Alpha"}]

    # HA's MQTT connection drops and comes back with the same retained end
    hass.states.async_set(STATUS, "unavailable")
    await _settle(hass, 3)
    hass.states.async_set(STATUS, "aborted", {"reason": "The job was interrupted"})
    await _settle(hass, 3)
    assert len(done) == 1


async def test_end_after_an_end_read_again(hass: HomeAssistant, setup_entry: Setup) -> None:
    """The end of the job before, read again on a reconnect, is no new end; the end of the running
    job that follows is one, also with the same state."""
    _pc_on(hass)
    _library(hass, ("10", "Alpha"), ("20", "Beta"))
    await setup_entry()
    async_mock_service(hass, "mqtt", "publish")
    done = _catch(hass, EVENT_JOB_DONE)
    first_end = {**_done(ok=["10"]), "job": "0000000a"}
    hass.states.async_set(STATUS, "started", {"appids": ["10"], "count": 1, "job": "0000000a"})
    hass.states.async_set(STATUS, "done", first_end)
    await _settle(hass)
    assert len(done) == 1

    # The next job installed its game and runs the library report; HA's MQTT connection drops and
    # comes back with the cached state and the retained end of the job before
    installed = {"appid": "20", "position": 1, "count": 1, "percent": 100, "job": "0000000b"}
    hass.states.async_set(STATUS, "started", {"appids": ["20"], "count": 1, "job": "0000000b"})
    hass.states.async_set(STATUS, "installed", installed)
    await _settle(hass)
    hass.states.async_set(STATUS, "unavailable")
    hass.states.async_set(STATUS, "installed", installed)
    hass.states.async_set(STATUS, "done", first_end)
    await _settle(hass)
    assert len(done) == 1

    hass.states.async_set(STATUS, "done", {**_done(ok=["20"]), "job": "0000000b"})
    await _settle(hass)
    assert len(done) == 2
    assert done[-1]["updated"] == [{"appid": "20", "name": "Beta"}]


async def test_game_started_and_ended(hass: HomeAssistant, setup_entry: Setup) -> None:
    _pc_on(hass)
    _library(hass, ("10", "Alpha"), ("20", "Beta"))
    await setup_entry()
    started = _catch(hass, EVENT_GAME_STARTED)
    ended = _catch(hass, EVENT_GAME_ENDED)

    hass.states.async_set(GAME_RUNNING, "on", {"appid": 10})
    await _settle(hass)
    assert hass.states.get(ENTITY_CURRENT_GAME).state == "Alpha"
    assert started == [{"appid": "10", "name": "Alpha"}]
    assert ended == []

    # Straight to another game: an end and a start
    hass.states.async_set(GAME_RUNNING, "on", {"appid": 20})
    await _settle(hass)
    assert ended == [{"appid": "10", "name": "Alpha", "minutes": 0}]
    assert started[-1] == {"appid": "20", "name": "Beta"}

    hass.states.async_set(GAME_RUNNING, "off", {"appid": 0})
    await _settle(hass)
    assert ended[-1] == {"appid": "20", "name": "Beta", "minutes": 0}
    assert len(started) == 2


async def test_game_events_follow_a_renamed_sensor(hass: HomeAssistant, setup_entry: Setup) -> None:
    """The game events watch the sensor "Current game" by its unique ID: renamed (the integration
    reloads), they still come."""
    _pc_on(hass)
    _library(hass, ("10", "Alpha"))
    await setup_entry()
    er.async_get(hass).async_update_entity(ENTITY_CURRENT_GAME, new_entity_id="sensor.my_game")
    await _settle(hass)
    started = _catch(hass, EVENT_GAME_STARTED)

    hass.states.async_set(GAME_RUNNING, "on", {"appid": 10})
    await _settle(hass)
    assert hass.states.get("sensor.my_game").state == "Alpha"
    assert started == [{"appid": "10", "name": "Alpha"}]


async def test_game_kept_over_an_mqtt_reconnect(hass: HomeAssistant, setup_entry: Setup) -> None:
    """HA's MQTT connection drops while a game runs (the agent's entities unavailable): the game
    keeps running with its start, no end and no new start. An agent known to be gone ends it."""
    _pc_on(hass)
    _library(hass, ("10", "Alpha"))
    await setup_entry()
    started = _catch(hass, EVENT_GAME_STARTED)
    ended = _catch(hass, EVENT_GAME_ENDED)
    hass.states.async_set(GAME_RUNNING, "on", {"appid": 10})
    await _settle(hass)
    since = hass.states.get(ENTITY_CURRENT_GAME).attributes["started"]

    hass.states.async_set(GAME_RUNNING, "unavailable")
    hass.states.async_set(AGENT_ONLINE, "unavailable")
    await _settle(hass)
    assert hass.states.get(ENTITY_CURRENT_GAME).state == "Alpha"
    hass.states.async_set(AGENT_ONLINE, "on")
    hass.states.async_set(GAME_RUNNING, "on", {"appid": 10})
    await _settle(hass)
    state = hass.states.get(ENTITY_CURRENT_GAME)
    assert state.state == "Alpha"
    assert state.attributes["started"] == since
    assert started == [{"appid": "10", "name": "Alpha"}]
    assert ended == []

    # The PC went off: the agent's last will, its game sensor expires
    hass.states.async_set(AGENT_ONLINE, "off")
    hass.states.async_set(GAME_RUNNING, "unavailable")
    await _settle(hass)
    assert hass.states.get(ENTITY_CURRENT_GAME).state == "unknown"
    assert ended == [{"appid": "10", "name": "Alpha", "minutes": 0}]


async def test_updates_found_only_when_new(
    hass: HomeAssistant, setup_entry: Setup, aioclient_mock: AiohttpClientMocker
) -> None:
    _pc_on(hass)
    _library(hass, ("10", "Alpha"), ("20", "Beta"))
    _build_answer(aioclient_mock, "10", "2")
    _build_answer(aioclient_mock, "20", "1")
    await setup_entry()
    async_mock_service(hass, "mqtt", "publish")
    found = _catch(hass, EVENT_UPDATES_FOUND)

    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert found == [{"games": [{"appid": "10", "name": "Alpha"}], "total": 1}]

    # Same state again: nothing new
    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert len(found) == 1

    # Beta gets an update too: only Beta is new
    aioclient_mock.clear_requests()
    _build_answer(aioclient_mock, "10", "2")
    _build_answer(aioclient_mock, "20", "2")
    await hass.services.async_call(DOMAIN, "check_updates", {}, blocking=True, return_response=True)
    await _settle(hass)
    assert found[-1] == {"games": [{"appid": "20", "name": "Beta"}], "total": 2}
