"""Push notifications through the notify service chosen on the settings page, only of the kinds
chosen there."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_NOTIFY_SERVICE, CONF_PUSH_KINDS, DOMAIN
from .pc import UNNAMED, PcNames

_LOGGER = logging.getLogger(__name__)

# Tags of the pushes (of the unnamed PC, pc.py): a push replaces the one with the same tag on the
# phone. Fixed, as automations may clear a push by its tag.
TAG_PC_IDLE = UNNAMED.tag("steam_engine_pc_idle")
TAG_AUTO_OFF_REMINDER = UNNAMED.tag("steam_engine_auto_off_reminder")
TAG_PC_LOCK = UNNAMED.tag("steam_engine_pc_lock")
TAG_PROGRESS = UNNAMED.tag("steam_engine_progress")  # start, progress and result of a job
# New updates: their own tag, so a job's pushes do not take the announcement away while its games
# still count as announced (steam_jobs.py, reported)
TAG_UPDATES = UNNAMED.tag("steam_engine_updates")
TAG_STALLED = UNNAMED.tag("steam_engine_stalled")  # shared by the stalled and the aborted push
TAG_AGENT = UNNAMED.tag("steam_engine_agent")
TAG_UNINSTALL = UNNAMED.tag("steam_engine_uninstall")
TAG_UNINSTALL_ASK = UNNAMED.tag("steam_engine_uninstall_ask")
TAG_MOVE = UNNAMED.tag("steam_engine_move")  # start and result of moving a game to another drive
# "not enough space" with its buttons and the pushes that follow them (no_space.py)
TAG_NO_SPACE = UNNAMED.tag("steam_engine_no_space")
TAG_LAUNCH = UNNAMED.tag("steam_engine_launch")
TAG_PURCHASE = UNNAMED.tag("steam_engine_new_purchase")


def _without_empty_links(data: dict[str, Any]) -> dict[str, Any]:
    """The links on the settings page are optional: drop an empty click target and "open" buttons without one."""
    extra = data.get("data")
    if not isinstance(extra, dict):
        return data
    extra = dict(extra)
    if not extra.get("clickAction"):
        extra.pop("clickAction", None)
    if "actions" in extra:
        actions = [a for a in extra["actions"] if not (a.get("action") == "URI" and not a.get("uri"))]
        if actions:
            extra["actions"] = actions
        else:
            extra.pop("actions")
    return {**data, "data": extra}


def for_pc(data: dict[str, Any], names: PcNames, show_name: bool = True) -> dict[str, Any]:
    """A push of a named PC: its own tag (so it does not replace the other PCs' pushes), its
    buttons marked as its own (@<id>, see PcNames.action_of; a button already marked for another PC
    stays so) and, with show_name, its name before
    the title. The unnamed PC's pushes stay as they are."""
    if not names.named:
        return data
    data = dict(data)
    if show_name and "title" in data:
        data["title"] = names.title(str(data["title"]))
    extra = data.get("data")
    if isinstance(extra, dict):
        extra = dict(extra)
        if "tag" in extra:
            extra["tag"] = names.tag(str(extra["tag"]))
        if "actions" in extra:
            extra["actions"] = [
                a if a.get("action") == "URI" or "@" in str(a.get("action")) else {**a, "action": names.action(str(a.get("action")))}
                for a in extra["actions"]
            ]
        data["data"] = extra
    return data


def several_pcs(hass: HomeAssistant) -> bool:
    """Whether Steam Engine is set up for more than one PC: only then is a PC's name shown (push
    titles, hints under Repairs); with one PC it would just take room. An ignored found PC is none."""
    return len(hass.config_entries.async_entries(DOMAIN, include_ignore=False)) > 1


def wanted(notify: dict[str, Any], kind: str | None) -> bool:
    """A push of this kind goes out (a service is chosen and, for a kind, it is chosen too)."""
    if not notify.get(CONF_NOTIFY_SERVICE):
        return False
    return kind is None or kind in (notify.get(CONF_PUSH_KINDS) or [])


def send_push(
    hass: HomeAssistant, notify: dict[str, Any], data: dict[str, Any], names: PcNames = UNNAMED, kind: str | None = None
) -> None:
    """Send without waiting, with the notify section of the options. kind: one of PUSH_KINDS; a
    kind not chosen is not sent. None for what is always sent: clearing a push, and the answer to a
    button of a push. No service chosen: nothing is sent. A missing service only logs, it must not
    stop the caller."""
    if not wanted(notify, kind):
        _LOGGER.debug("Push of kind %s not chosen, not sent: %s", kind, data.get("title"))
        return
    service = str(notify[CONF_NOTIFY_SERVICE])
    data = for_pc(data, names, several_pcs(hass))
    if not hass.services.has_service("notify", service):
        _LOGGER.warning("Notify service notify.%s not found, message dropped: %s", service, data.get("title"))
        return
    data = _without_empty_links(data)
    hass.async_create_task(hass.services.async_call("notify", service, data, blocking=False))
