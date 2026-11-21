"""Names of the MQTT protocol between the PC scripts and the integration (see PROTOCOL.md), and the
one definition of a running job."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .values import id_list

# Job states of the install status sensor
STARTED = "started"
DOWNLOADING = "downloading"
INSTALLED = "installed"
SKIPPED = "skipped"
ERROR = "error"
DONE = "done"
DONE_WITH_ERRORS = "done_with_errors"
ABORTED = "aborted"
# The end of a job: done/done_with_errors with the result lists, aborted (interrupted) without them
FINISHED = (DONE, DONE_WITH_ERRORS)
ENDED = (*FINISHED, ABORTED)

# Result lists of done/done_with_errors (app IDs; no_space holds {appid, needed_gb, free_gb, drive})
RESULT_LISTS = ("ok", "failed", "skipped", "game_running")

# Phase of a download in which percent counts (other phases: verify, commit, prepare)
PHASE_DOWNLOAD = "download"

# Attributes whose change counts as progress of a job. 'updated' moves only with real disk activity
# (commit and verify can run long without a new percent); downloading comes every 10 s unchanged.
PROGRESS_KEYS = ("phase", "phase_percent", "percent", "updated")

# reason_code of a game that failed because the Steam client did not sign in
CODE_NOT_LOGGED_ON = "steam_not_logged_on"

# Drive letters in commands: one lower-case letter (the agent lower-cases every command)
DRIVE_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyz")

# Attribute of every job state: the job ID HA gave the job in its install command
JOB_ID = "job"


def valid_drive(value: Any) -> bool:
    """A drive letter as stored and sent: exactly one lower-case letter a-z."""
    return isinstance(value, str) and value in DRIVE_LETTERS


def valid_appid(value: Any) -> bool:
    """An app ID as the agent takes it: ASCII digits only. str.isdigit alone is also true for other
    digits ('²', '٣'), which the agent refuses, and with them the whole install command."""
    return isinstance(value, str) and value.isascii() and value.isdigit()


def install_command(appids: Iterable[str], targets: Mapping[str, str], job: str = "") -> str:
    """The install command for the agent: "install:[<job>:]<appid>[@<letter>],...," with the job ID
    (lower-case letters and digits, sent back in every state of the job) and the target drive of
    each game that has one (without, the PC uses Steam's default library)."""
    parts = [f"{appid}@{targets[appid]}" if valid_drive(targets.get(appid)) else str(appid) for appid in appids]
    return "install:" + (f"{job}:" if job else "") + ",".join(parts) + ","


def job_id(attributes: Mapping[str, Any]) -> str:
    """The job ID of a job state, "" for a job started without one (by hand on the PC)."""
    return str(attributes.get(JOB_ID) or "")


def job_active(state: str | None, attributes: Mapping[str, Any]) -> bool:
    """A job runs: it started, a game downloads or is installed, or a game was skipped or failed
    with a position (the next game or the end of the job follows). Between two games install.ps1
    can work for minutes without a new state (app info, space check, the library report at the
    end), so these states count too. Every job ends with done, done_with_errors or aborted."""
    if state in (STARTED, DOWNLOADING, INSTALLED):
        return True
    return state in (SKIPPED, ERROR) and attributes.get("position") is not None


def result_appids(attributes: Mapping[str, Any]) -> set[str]:
    """App IDs that the end state of a job (done, done_with_errors) names in its result lists."""
    ids: set[str] = set()
    for key in RESULT_LISTS:
        ids |= set(id_list(attributes.get(key)))
    no_space = attributes.get("no_space")
    if isinstance(no_space, list):
        ids |= {str(k.get("appid")) for k in no_space if isinstance(k, dict) and k.get("appid") is not None}
    return ids


def state_appids(state: str | None, attributes: Mapping[str, Any]) -> set[str]:
    """App IDs a job state is about: the games of the job at its start, the results at its end,
    otherwise the one game the state is about."""
    if state == STARTED:
        return set(id_list(attributes.get("appids")))
    if state in FINISHED:
        return result_appids(attributes)
    return set(id_list(attributes.get("appid")))
