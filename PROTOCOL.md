# MQTT protocol between the PC and Home Assistant

The Windows side (Steam agent, `install.ps1`, `uninstall.ps1`, `move.ps1`, `report.ps1`) and the integration talk
over MQTT.

Retained topics keep their last message, so Home Assistant has the state again after a restart.

## Topics

| Topic | Retained | Sender | Content |
|---|---|---|---|
| `steam/agent/state` | yes | agent | Steam and game state, drives and libraries, plugin busy flags |
| `steam/agent/status` | yes (last will) | agent | `online` / `offline` |
| `steam/agent/telemetry` | no | agent | last input, network rate |
| `steam/agent/info` | yes | agent | who runs as this PC: `{"computer": "<computer name>", "pc_id": "", "pc_name": ""}`, sent on each connect; cleared by the uninstall. Home Assistant shows a PC that is not set up yet as found (MQTT discovery) |
| `steam/agent/command` | no | HA | commands, see below |
| `steam/job` | final states only | `install.ps1`, `report.ps1` (`aborted`, or an end state `install.ps1` could not send) | job state |
| `steam/library` | yes | `report.ps1` | installed games |
| `steam/depots` | yes | `report.ps1` | installed depots per game with their manifest IDs, `{"<appid>": {"<depot>": "<manifest id>"}}`; sent just before `steam/library`. The update check compares them with the public manifests, because an update of a single depot can keep the build ID. Not part of the library sensor (its attributes stay small) |
| `steam/history` | yes | `report.ps1` | recent installs and updates |
| `steam/uninstall` | no | `uninstall.ps1` | uninstall result |
| `steam/move` | end states only, QoS 1 | `move.ps1`, `report.ps1` (an end state `move.ps1` could not send) | progress and result of moving a game to another drive, see below |
| `steam/launch_pending` | yes, QoS 1 | HA (set and clear), lock script (clear) | note that a game start from Home Assistant woke the PC, so the lock at logon is left out once, see below |

## Commands (`steam/agent/command`)

| Command | Effect |
|---|---|
| `install:[<job>:]<appid>[@<letter>],<appid>[@<letter>]` | install or update these games with the Steam client (an app ID given twice counts once: its first part counts). `<job>` is the job ID Home Assistant gives each job it hands over (1 to 16 lower-case letters and digits, e.g. `install:3f9a0c1e:440,570`): every state of the job carries it as `job` (see *Job state*); a job started without one sends none. `@<letter>` is the target drive of a first install, a lower-case letter like `install:440@e,570` (never a path): the game goes to the Steam library on that drive (several there: the same one `drives` names), never to another drive; a drive without a library gets a new one, `<letter>:\SteamLibrary` (see *New library* below); without `@<letter>` to Steam's default library. An installed game stays where it is and ignores the target. One job at a time: games the running job has, or a job that ended since the command came, are left out (so the same command sent again runs once); the others wait for a running install job, and up to 10 minutes for anything else (an uninstall or a move; a longer move makes them fail with `job_running`). Nothing is sent on `steam/job` while they wait |
| `library` | send the library again |
| `uninstall:<appid>` | uninstall a game |
| `move:<appid>@<letter>` | move an installed game to the Steam library on drive `<letter>` (a lower-case letter like `move:440@e`, never a path); a drive without a library gets a new one, `<letter>:\SteamLibrary`. See *Move* below |
| `launch:<appid>` | start a game with `steam://rungameid/<appid>`; Steam starts first if it does not run |
| `shutdown` | shut the PC down |
| `restart` | restart the PC |
| `lock` | lock the PC |
| `close_games` | close all running games: every process of the logged-on user's session whose executable lies in the install folder (`<library>\steamapps\common\<installdir>\`) of an app Steam runs right now (`RunningAppID` and every app with `Running` = 1 under `HKCU\Software\Valve\Steam\Apps`, so several games at once are covered). The Steam client itself is never touched, nor a tool in `steamapps\common` that Steam did not start (e.g. Wallpaper Engine started with Windows). The games are asked to close first (their main window gets a close request); those still running after 10 s are stopped by force. A process that cannot be stopped (elevated, an anti-cheat service) is skipped and logged. The agent keeps taking commands meanwhile and sends its state about 3 s after the end, so `game_running` turns `OFF` without waiting for the next round. |
| `ping` | nothing, only a line in `steam-agent.log` (tests the command path) |
| `run:<name>[:<arg>]` | run a plugin script from `plugins.psd1`; the argument goes to it as `-Job` (command and argument are lower-cased; the argument may only contain a-z, 0-9 and `_ . , = ; -`, otherwise the command is ignored) |

## Agent state (`steam/agent/state`)

| Field | Value |
|---|---|
| `steam_running` | `ON` / `OFF` |
| `steam_downloading` | `ON` / `OFF` |
| `downloading_appids` | list of app IDs |
| `steam_moving` | `ON` / `OFF`: a move job (`move.ps1`) runs |
| `game_running` | `ON` / `OFF` |
| `game_appid` | app ID or 0 |
| `free_gb` | most free space of all drives with a library |
| `drives` | the drives to install to, see below |
| `busy` | `{<plugin>: ON/OFF}` |

`steam_downloading` is `ON` while the manifest of a game shows that Steam works on it right now
(StateFlags: update running, reconfiguring, validating, adding files, preallocating, downloading,
staging, committing), or while Steam wrote to `steamapps\downloading\<appid>` within the last 3
minutes (15 minutes for a game whose update is only paused or queued). A paused or queued update
alone does not count, so it does not block the shutdown for good.

`steam_moving` is `ON` while `move.ps1` runs (it names itself in `jobs\move.lock`): the copy, the
time the Steam client is closed and the deletion of the source. The binary sensor "Steam
downloading" carries it as the attribute `moving` (`ON` / `OFF`). The integration follows a move by
its messages on `steam/move` (see *Move*) and also counts it as running while `moving` is `ON` and
the agent is online: after a restart of HA that is all it knows until the next `running` message.

`drives` has one entry per drive letter: `{drive, free_gb, total_gb, library, default, bus, media}`.

- `drive`: the letter in upper case (`"F:"`); `free_gb` rounded down,
  `total_gb` rounded.
- `library`: the path of the Steam library on that drive, or `null` for a drive without one. Every
  Steam library whose folder is there is listed (read every round). Several libraries on one drive:
  the default one wins, else the one with the lowest number in `libraryfolders.vdf`. An install
  with a target drive uses the same library.
- Drives without a library: fixed drives (a USB disk that reports itself as fixed too) that are
  ready, NTFS, have a letter and at least 10 GB free. A first install there creates the library
  (see *New library* below). They are looked for at most every 5 minutes (it
  may wake sleeping hard disks; nothing is written), their space is from that time. Left out: drives
  that hold a library Steam lists but whose folder is gone, and drives with a
  `<letter>:\SteamLibrary\libraryfolder.vdf` that Steam does not list (not handled yet).
- `default: true` for exactly the library a first install uses: Steam's default library (the star
  in *Steam > Settings > Storage*, `LastInstallFolderIndex` in the account's `localconfig.vdf`), or
  the main Steam folder when that cannot be read. When its drive or folder is not there, no entry
  is the default (a first install then fails, see `drive_unavailable`).
- Order: the default, then the libraries by letter, then the drives without a library by letter.
- `bus`: how the disk behind the drive is connected, in lower case: `"usb"`, `"nvme"`, `"sata"`,
  `"sas"`, `"raid"`, `"scsi"`, `"ata"`, `"spaces"`, `"virtual"`, ... (the `BusType` of
  `MSFT_PhysicalDisk`; a disk that only `Win32_DiskDrive` knows as USB gives `"usb"`), or `null`
  when unknown.
- `media`: `"ssd"`, `"hdd"` (the `MediaType` of `MSFT_PhysicalDisk`; NVMe without one counts as
  `"ssd"`), or `null` when unknown.
- `bus` and `media` follow the drive letter to its partitions, so they work for a dynamic volume
  over several disks too; such a volume gets a value only when all its disks agree, else `null`.
  They are read (CIM, read only) together with the drives without a library, and again as soon as
  a drive letter comes or goes. A failing query gives `null`.

The sensor `library_free` has `drives` as its attribute (an empty list when a state has none).

### New library

A first install to a drive without a Steam library (`install:<appid>@<letter>`) adds the library
`<letter>:\SteamLibrary` while the job has the Steam client closed for the install (so not while a
game runs: the game is reported as `game_running` and keeps its target in HA). Steam has no
interface for this, so the job writes what Steam's storage manager writes:

1. Checked again first: the drive is fixed, ready, NTFS and has no library Steam lists;
   `<letter>:\SteamLibrary` does not exist or is empty (an old library there, with its
   `libraryfolder.vdf`, is never taken over or overwritten). Otherwise `drive_not_suitable`.
2. The new `libraryfolders.vdf` (one block more, a new random `contentid`) is built and checked
   before anything is written: it must parse and keep every library block with its number, path
   and `contentid`.
3. Both `libraryfolders.vdf` (`config\` and the copy in `steamapps\`, which Steam reads at its
   start) are backed up as `libraryfolders.vdf.bak_<yyyyMMdd_HHmmss>` next to them (older backups
   of this kind are removed only after Steam has taken the new library), `<letter>:\SteamLibrary\steamapps`
   and `libraryfolder.vdf` are created, and both files are written atomically and read back. A
   failure puts the backups back and removes what was created (`library_not_created`).
4. After the client start the job waits up to 90 s for Steam's line `Loaded <n> apps from install
   folder "<letter>:\SteamLibrary\steamapps\..."` in `content_log.txt`, then checks that every
   library from before is still in `config\libraryfolders.vdf` (Steam resets all libraries to its
   main folder when it cannot read the file).
   - A library from before is missing: the client is closed, the backups are put back, the manifest
     is removed, the client is started again; `library_config_lost`. The new folder stays with its
     `libraryfolder.vdf` (it is no library after the restore), so the drive is no longer offered in
     `drives` until the folder is removed or added in Steam's storage settings.
   - Steam did not load the new library: the same, and the manifest and the new folder are removed;
     `library_not_created`.
   - When the backups cannot be put back (the client did not exit, a copy failed), the job writes
     `config\libraryfolders.vdf.restore_pending` next to them, naming each backup and its file.
     While that file exists no new library is created (`library_not_created`) and no backup is
     removed; it is deleted by hand after the backups are put back.
5. Then the download goes on as for any first install. A library Steam loaded stays, also when the
   download fails later. The next game of the same job to that drive uses it.

## Telemetry (`steam/agent/telemetry`)

| Field | Value |
|---|---|
| `last_input` | time of the last keyboard or mouse input, ISO 8601 in UTC, e.g. `2026-10-31T12:34:56+00:00` (independent of the PC's language settings) |
| `network_bytes_s` | bytes per second at the adapter with the default route; `null` in the first round and while it cannot be measured |
| `network_adapter` | name of that adapter; `null` while the rate cannot be measured |

## Job state (`steam/job`)

`state` is one of:

| State | Meaning |
|---|---|
| `started` | job accepted, attribute `appids` |
| `downloading` | one game is downloading |
| `installed` | one game is done |
| `skipped` | one game was skipped (Steam client working on it right now, not enough space, a game is running) |
| `error` | one game failed (always with `appid` and `position`) |
| `done` | job finished |
| `done_with_errors` | job finished; at least one game failed, had not enough space or was not started because a game was running. Also when the job itself failed: then `failed` holds all games not handled yet, with `reason` and `reason_code` |
| `aborted` | job was interrupted (PC off, script stopped); sent by `report.ps1` when it finds `install.lock` of a job that no longer runs, with the job's `job` from there |

Every job that sent `started` ends with `done`, `done_with_errors` or `aborted`. If `install.ps1`
cannot send its end state for about a minute (MQTT down), it keeps it in `jobs\install_final.json`;
the next `report.ps1` or job sends it (retained) instead of `aborted`. A game the Steam client only
holds paused or queued is not skipped: the job starts it with `steam://validate/<appid>`.

While a game downloads, `downloading` is sent every 10 seconds, also unchanged: only end states are
retained, so after a restart of Home Assistant or an MQTT reconnect a running job shows again within
seconds. The integration counts a job as running from `started` to its end, also while it reports
`installed`, `skipped` or `error` (with `position`) between two games.

Every state carries the job ID of the command as `job`, also the end kept in `install_final.json`
and the `aborted` of `report.ps1` (from `install.lock`). The integration gives each job it hands over
a new one and keeps it, with the job ID of the end it told last (`job_id` and `end_id` in its store,
`steam_progress.py`). A state of a running job that comes just now with another job ID makes that
job `job_id`: the PC left out the games of a command sent while it ran unseen (e.g. right after a
restart of Home Assistant, between two of its games). An end is news once: when it comes just now,
or, read again after a restart of Home Assistant or an MQTT reconnect, when it is the end of `job_id`
that was not told yet (it came while HA was not listening). Then it settles the job list and is told
(result push, event `steam_engine_job_done`) like an end that came live. Any other end read again
changes nothing: an older end does not end a newer job of the same game. No clock of the PC is
compared with Home Assistant's.

Time limits of a game (`install.ps1`): Steam must show activity for the game within 5 minutes
(`download_not_started`), an error Steam reports must be followed by real progress (32 MB written or
read by `steam.exe`) within 10 minutes (`steam_error`; Steam retries by itself, and the log lines of
its failing retries are no progress), and there must be progress at least once an hour
(`no_progress`; a log line of the game counts too while no error waits). Time the PC slept
(standby, hibernation) counts for none of them. When Steam holds the download because a game was
started meanwhile (`content_log.txt`: result `Suspended` with the reason `Disabled (Suspended)` or
none, while a game runs), the game ends as `game_running` at once, as when the game ran before. Held
behind another download (`Priority (Suspended)`, `Shader Priority (Suspended)`) the job waits on, as
Steam goes on with the game after it (that download's bytes count as progress); a download held
without a running game (e.g. paused in Steam) stays until `no_progress`.

Attributes:

| Field | Content |
|---|---|
| `job` | the job ID of the command (every state; missing for a job started without one) |
| `appids`, `count` | the app IDs of the job and their number |
| `position`, `appid` | the current game and its place in the job |
| `percent` | download progress of the current game |
| `phase` | `download`, `verify`, `commit` or `prepare` |
| `phase_percent` | progress of the current phase |
| `downloaded_gb` / `total_gb` | downloaded and total size |
| `updated` | time of the last progress, ISO 8601 in UTC with `+00:00` |
| `reason` | English text |
| `reason_code` | a code for `reason` on `error` and `skipped`, and on a `done_with_errors` of the whole job, see below |
| `needed_gb` / `free_gb` | space needed and free for a game skipped for lack of space |
| `ok`, `failed`, `skipped` | lists of app IDs; `skipped` only holds games the Steam client was already handling, games without space are in `no_space`, games held back by a running game in `game_running` |
| `no_space` | list of `{appid, needed_gb, free_gb, drive}`; `needed_gb` includes the reserve (the missing part of the game plus max(5 GB, 10 % of its size)), `drive` is the letter in upper case (`"F:"`) |
| `new` | app IDs installed for the first time (also a first install sent again before Steam finished it, e.g. after `game_running`) |
| `game_running` | app IDs not started because a game was running |
| `game_appid` | with `game_running`: the app ID of the game running when the end was sent (missing when none runs by then). Home Assistant then waits for that game to close before it sends the games again, also while the agent does not report it yet (it reads `RunningAppID` once a round); if the agent still reports no game 30 s later, the game was closed before that and they go out |

`reason_code` (`reason` stays the English text):

| Code | Meaning |
|---|---|
| `drive_unavailable` | the drive of the game's library is not there (for a first install: the target drive, else the drive of Steam's default library; the game never goes to another drive instead) |
| `library_unreadable` | `libraryfolders.vdf` cannot be read: neither the library of an installed game nor one for a first install is known (updates and first installs; also for an uninstall, see there) |
| `not_a_library` | a first install: the folder of the library Steam lists on the target drive is gone, or the folder of Steam's default library is gone (Steam still lists it) |
| `drive_not_suitable` | a first install to a drive without a library: the drive cannot take a new one (not a fixed NTFS drive, or `<letter>:\SteamLibrary` is not empty or holds an old library) |
| `library_not_created` | a first install to a drive without a library: the new library could not be added, or Steam did not load it; `libraryfolders.vdf` was put back |
| `library_config_lost` | a first install to a drive without a library: after the start Steam no longer listed a library from before; `libraryfolders.vdf` was put back from the backup (when the client did not exit, `reason` names the backups to put back by hand) |
| `steam_busy` | `skipped`: the Steam client works on the game right now |
| `game_running` | `skipped`: a game is running, or one was started while Steam worked on the game and Steam holds the download |
| `install_folder_unknown` | a first install without app info from api.steamcmd.net, or its `installdir` there is not a plain folder name (it would go into the manifest) |
| `no_space` | `skipped`: not enough space (with `needed_gb`, `free_gb`, `drive`) |
| `steam_did_not_exit` | the Steam client did not exit for the restart a first install needs |
| `manifest_not_written` | the manifest for a first install could not be written |
| `steam_not_logged_on` | the Steam client did not log on |
| `not_installed_after` | Steam ended without the game installed |
| `steam_error` | Steam reported an error it did not get past (no real progress within 10 minutes) |
| `download_not_started` | Steam never started the download |
| `steam_not_running` | the Steam client is not running |
| `no_progress` | no progress for the idle time (an hour) |
| `unexpected_error` | an unexpected error with this game |
| `job_failed` | `done_with_errors`: the job stopped unexpectedly |
| `job_running` | `done_with_errors`: another Steam job (not an install) held the job lock for more than 10 minutes |

### Test switch for the space check

A file `space_test_free_gb.txt` next to `install.ps1` (`%LOCALAPPDATA%\SteamEngine\jobs`) makes the
space check use made-up free space in GB instead of the real one, to test what follows a
`no_space` without filling a disk. One value per line:

- `F:=20` (also `f=20`): 20 GB free on drive F:;
- a plain number (`20`): for every drive not named on a line of its own.

Other lines are ignored; a drive without a value (and no plain number) uses its real free space. The
log line of the space check ends in `(TEST VALUE from space_test_free_gb.txt)` when a value applied.
The install job and the move job (`move.ps1`, for the target drive) read it: `free_gb` and `drives`
in `steam/agent/state` stay real. Delete the file after the test.

## Library (`steam/library`)

| Field | Content |
|---|---|
| `count` | number of installed games |
| `games[].appid`, `name`, `buildid`, `size_mb` | per game (`size_mb`: `SizeOnDisk` of the appmanifest) |
| `games[].workshop_mb` | size of the game's workshop content (`SizeOnDisk` of `steamapps\workshop\appworkshop_<appid>.acf`), 0 without it; missing from older PCs |
| `games[].last_played` | `YYYY-MM-DD`, or an empty string when never played |
| `games[].drive` | drive letter of the game's library in upper case with colon (`F:`), `null` when unknown (a library path without a drive letter); missing from older PCs |
| `games[].unavailable` | `true` for a game on a library whose drive is not there (USB disk unplugged), listed with its last known data; the integration leaves it out of update checks and deletion suggestions |

## History (`steam/history`)

| Field | Content |
|---|---|
| `latest` | time of the newest entry |
| `total` | number of all entries |
| `entries` | the newest entries |
| `entries[].time`, `appid`, `name` | per entry |
| `entries[].kind` | `installed`, `updated`, `removed` |
| `entries[].source` | `ha` (installed, updated or removed by a Steam Engine job), `steam` (by the Steam client itself) |
| `entries[].old_build` / `new_build` | build IDs before and after |

## Uninstall result (`steam/uninstall`)

| Field | Content |
|---|---|
| `appid`, `name` | the game |
| `result` | `ok`, `error` |
| `reason` | a code (`invalid_appid`, `job_running` (an install job runs, another uninstall or a move still runs after 3 minutes, or a move of this game cut off after its switch is not finished yet: its journal in phase `switch`, see *Move*), `not_installed`, `drive_unavailable` (the game's library is on a drive that is not there), `library_unreadable` (libraryfolders.vdf could not be read), `steam_busy` (Steam works on the game right now; a paused or queued update does not count; or another manifest of the library cannot be read, so it is not known whether another app shares the folder), `unexpected_folder` (the manifest's `installdir` is not a plain folder name, or the game folder or its workshop folder is a junction or symbolic link), `shared_folder` (another app's manifest in the same library names the same install folder, e.g. a game and its editor: the folder would go as a whole; checked before the Steam client is closed and again right before deleting), `game_running` (a game runs, Steam runs this one (`Running` = 1 under `HKCU\Software\Valve\Steam\Apps\<appid>`), or a program runs from the game folder, also one Steam did not start; checked before the Steam client is closed and again right before deleting), `steam_did_not_exit`, `delete_failed`, `unexpected_error`), shown translated in HA |
| `free_gb` | free space on the drive afterwards |

The game folder is deleted without following links: a junction or symbolic link inside it is removed
itself, never what it points to.

After a successful uninstall the agent notices the removed manifest in its next round (20 s) and runs
`report.ps1`, so the library is up to date without waiting for the regular report.

## Move (`steam/move`)

`move:<appid>@<letter>` starts `jobs\move.ps1`, which moves an installed game to the Steam library on
another drive (several libraries there: the one `drives` names; a drive without one gets
`<letter>:\SteamLibrary` as in *New library* above). The source's files stay untouched until Steam has
taken the game at the target:

1. Checks: the game is installed (`appmanifest_<appid>.acf` in a library whose drive is there), its
   game folder and workshop folder are no junction or symbolic link (`unexpected_folder`), no other
   app's manifest in that library names the same install folder (`shared_folder`; `steam_busy` while
   one of them cannot be read or names no install folder after 3 tries), `StateFlags` is
   4, or 6 (an update Steam put off: it stays put off in the target), and
   `steamapps\downloading\<appid>` holds no files (else `steam_busy`), no game runs (Steam cannot be
   closed then), Steam does not run this game (`Running` = 1 under
   `HKCU\Software\Valve\Steam\Apps\<appid>`) and no program runs from its game folder, also one Steam
   did not start, e.g. a tool started with Windows (`game_running`), the target is another drive,
   ready, and its library has neither the game folder nor the manifest, the workshop content or
   `appworkshop_<appid>.acf` of the game; no folder on the way to the staging folder is a junction or
   symbolic link (`unexpected_folder`). Staging folders other moves left behind (`.se-move-<appid>`
   in `steamapps\common` and `steamapps\workshop\content` of every library, `.se-move-<appid>` and
   `.se-move-<appid>-workshop` on the root of every fixed drive and of every library's drive) are
   deleted. Space, as Home Assistant works it out from `steam/library`: `size_mb + workshop_mb` plus
   the reserve of an install, max(5 GB, 10 % of `size_mb`). On the PC each part counts at least what
   its folder holds (the whole folder is copied, also files Steam does not count, e.g. mods): the
   larger of `SizeOnDisk` of the appmanifest and the game folder, plus the larger of `SizeOnDisk` of
   `appworkshop_<appid>.acf` and the workshop folder, plus the reserve (of the game part), less what
   this move's staging folder holds already from an interrupted try. So the PC can answer `no_space`
   where Home Assistant expected the game to fit. The test switch `space_test_free_gb.txt` applies to
   the target drive.
2. Copy while Steam runs: robocopy mirrors `steamapps\common\<installdir>` into the staging folder
   `steamapps\common\.se-move-<appid>` of the target library (workshop content
   `steamapps\workshop\content\<appid>` into `steamapps\workshop\content\.se-move-<appid>`); on a
   drive without a library into `<letter>:\.se-move-<appid>` (and `...-workshop`), because the new
   library folder must be empty when it is added. Links (junctions, symbolic links) are not copied;
   a game folder with links is not moved. When the game starts or Steam starts working on it
   meanwhile, the copy stops (`game_running`, `steam_busy`; a manifest Steam is rewriting counts
   only when it cannot be read 3 times in a row) and the staging folder is deleted. This move's own
   staging folder from an interrupted try is mirrored again, not counted as the game.
3. The Steam client is closed (not while a game runs), the rest is copied (only what changed),
   file count and bytes of source and copy are compared. A drive without a library gets it now.
   Right before that and before the switch the client must still be closed and no game run, and
   right before the switch nothing may run from the game folder and no other app may name it (else
   `steam_busy`, `game_running` or `shared_folder`, nothing is switched). The switch: the move's
   journal `jobs\move_pending_<appid>_<time>.json` is written (see below), the manifest (and
   `appworkshop_<appid>.acf`) is written to the target `steamapps`, the staging folder is renamed to
   `<installdir>`, then the manifest is removed from the source. The source's files are still all
   there.
4. The client is started (also when it did not run before the move: the check needs it). Within
   180 s `content_log.txt` must show `Loaded <n> apps from install folder "<target library>\steamapps\..."`
   (Steam may update itself first), then within 90 s the target manifest `StateFlags` 4 or 6 (for
   5 s). Steam moves the game to the target library in `libraryfolders.vdf` by itself; nothing is
   downloaded or validated.
5. Only then the source folder, its workshop content and its `steamapps\shadercache\<appid>` are
   deleted. An entry that cannot be deleted (a locked file) is passed over and tried once more; when
   something is left the move still counts: `ok` with `warning: "source_not_deleted"`, and the rest
   is deleted at the next start of the agent or a move (it keeps no move out, see *Journal*). The
   same when a program runs from the source folder, or a manifest of the source library cannot be
   read: then nothing is deleted yet. A source folder another app of that library names as its
   install folder by then is that app's and stays.

A failure before step 5 takes everything back (the client closed again when it runs): the source
manifest is written back as it was, the copy and the staging folders are deleted, a new library
Steam did not load (or after which it lost libraries) is taken back as for an install. The source's
files are never changed before step 5; only its manifest is gone from the switch on and written back
when the move is taken back. If the client does not exit for that: when Steam had loaded the target
library (its `Loaded <n> apps from install folder` line was seen; a new library must also have kept
every library from before) and shows the game installed in the target by then, the move counts
(`ok`, the source is deleted); else nothing is taken back (`rollback_failed`, `reason` says where
the game is) and the journal finishes it later. A new library Steam did not take is then not put
back either: as for an install, `config\libraryfolders.vdf.restore_pending` names the backups to
put back by hand, and the journal waits for that. The client is always started again when the job
closed it.

**Journal.** From the switch on until the move is finished or taken back, its journal
`jobs\move_pending_<appid>_<time>.json` (one file per move, `<time>` the UTC time of the switch as
`yyyyMMddHHmmss`) holds what the move did (app ID, install folder, both libraries, the manifests as
they were, what Steam made of a new library) and its phase: `switch`, then `cleanup` once Steam
took the game at the target, or `undo` once a take-back put the source manifest back. A move cut
off there (PC off, job killed) or not taken back is dealt with by the next `move.ps1`, and by
`move.ps1 -Recover`, which the agent starts at its own start when a journal is there:

- `cleanup`: the move counts and is never taken back. What is left of the game in the source (its
  folder, workshop content, `appworkshop_<appid>.acf`, shader cache) is deleted as in step 5, also
  when the game is gone from the target since; nothing is deleted when the game is installed in
  the source library again. Only the source drive is needed. No end state is sent.
- `switch`, the target has the manifest and the game folder, the source no manifest, and Steam's
  `libraryfolders.vdf` lists the target library: the game counts as moved and the rest of the
  source is deleted as in step 5 (end state `ok`; with `warning: "source_not_deleted"` when
  something is left, the journal then goes on in `cleanup`). Nothing is done while
  `libraryfolders.vdf.restore_pending` is there after a new library Steam did not take.
- `switch`, neither library has the manifest (a move never leaves it so) and the target library
  folder is there: the game was uninstalled meanwhile in the Steam client (an uninstall from Home
  Assistant is refused while such a journal is there, `job_running`). Nothing is
  written back and no folder is deleted (end state `error` with `not_installed`, `reason` names
  the folders left; `warning: "source_not_deleted"` when the old folder is one of them). Without
  the target library folder (another disk under that letter) it waits like a missing drive.
- else the move is taken back as above (end state `error` with `interrupted`; the Steam client is
  closed for that when it could see the game in the target, not while a game runs): the source
  manifest is written back, the journal goes on in `undo`, then this move's copies at the target are
  deleted. A game folder at the target counts as this move's copy only while this move's manifest is
  there (else Steam may have left it after an uninstall, with files made since: it stays, `reason`
  names it).
- `undo`: the rest of the copy at the target is deleted (never the only copy of a game installed in
  the source); nothing is deleted when the game is in the target library and not in the source
  (installed or moved there since). No end state is sent.

The end state carries the `started` of the cut-off move. A journal that cannot be dealt with yet (a
drive missing, a locked file, a game running) stays and is tried again later. While one in phase
`switch` stays, new moves are answered with `job_running`, and so is an uninstall of that game;
one in `cleanup` or `undo` keeps nothing out. `Uninstall-SteamEngine.ps1` stops while a journal is
there; it names a drive that is missing, and for a drive gone for good the journal file can be
deleted by hand (the folder it names then stays).

One Steam job at a time: the job lock is shared with install and uninstall (an install job running
gives `job_running` at once, another uninstall or move is waited for up to 3 minutes). The log is
`jobs\move.log`, robocopy's own log `jobs\move_robocopy.log` (overwritten by every copy). After a
successful move the agent sees the manifest gone from the source library and sends the library
(20 s), where `games[].drive` then names the target drive. A new library taken back (no longer in
`libraryfolders.vdf`) counts as gone with its games, so the library is sent again after that too.

Every message is JSON with these fields:

| Field | Content |
|---|---|
| `state` | `running`, then one end state: `ok` or `error` |
| `started` | when the job started (ISO 8601 in UTC with `+00:00`); the same in all messages of one job, so an end state read again later (retained) is not taken for a newer move. A move finished or taken back by `-Recover` sends its end with the `started` of the interrupted run |
| `appid`, `name` | the game (`name` is `null` before its manifest was read) |
| `from` | the drive the game is on, like `"F:"` (`null` when not known) |
| `drive` | the target drive, like `"E:"` |
| `updated` | time of the message, ISO 8601 in UTC with `+00:00` |
| `late` | only in an end state sent late from `move_final.json`: `true` |

`running` (not retained, about every 5 s while the job runs, also while Steam is closed or starting;
only while the Steam client exits there can be a gap of up to 2.5 minutes):

| Field | Content |
|---|---|
| `phase` | `copy` (Steam runs), `sync` (Steam closed, copying what changed and comparing), `switch` (Steam closed, the game takes its place), `verify` (Steam starting), `cleanup` (deleting the source), `rollback` (taking the move back after a failure; the `error` end state follows) |
| `percent` | copied part of the game and its workshop content, 0-99 while copying, 100 from `switch` on |
| `copied_gb`, `total_gb` | copied and total size |

`ok` (retained):

| Field | Content |
|---|---|
| `library` | the target library |
| `new_library` | `true` when the move added that library |
| `source_free_gb`, `target_free_gb` | free space on both drives afterwards |
| `warning` | `null`, or `source_not_deleted` (the old folder is partly left, `reason` names it) |
| `reason` | `null`, or the English text of the warning |

`error` (retained):

| Field | Content |
|---|---|
| `reason_code` | see below |
| `reason` | English text (with what was taken back, or what could not be) |
| `needed_gb`, `target_free_gb` | only with `no_space`: the space needed (with the reserve) and free on the target |
| `warning` | `null`, or `source_not_deleted` with `not_installed` from the journal: the game was uninstalled meanwhile and its old folder stays (`reason` names it) |

| `reason_code` | Meaning |
|---|---|
| `invalid_request` | the app ID or the drive letter is not valid (a hand start) |
| `job_running` | an install job runs, another uninstall or move still runs after 3 minutes, or an interrupted move (a journal in phase `switch`) can be neither finished nor taken back yet |
| `library_unreadable` | `libraryfolders.vdf` cannot be read |
| `not_installed` | the game is not installed (no manifest, or its folder is missing); also the end of a move cut off after its switch whose game was uninstalled meanwhile (see *Journal*) |
| `drive_unavailable` | the drive of the game's library, or the target drive, is not there |
| `unexpected_folder` | the manifest's `installdir` is not a plain folder name, the game folder or its workshop folder or a folder on the way to the staging folder is a junction or symbolic link, or an old staging folder cannot be removed |
| `shared_folder` | another app's manifest in the game's library names the same install folder (e.g. a game and its editor): the folder would move as a whole and leave the other app without its files (checked before the copy and right before the switch) |
| `steam_busy` | Steam works on the game: `StateFlags` not 4 or 6 (downloading, an update queued or paused, validating), files in `steamapps\downloading\<appid>`, also when that starts during the copy; the Steam client was started while it had to stay closed; or another manifest of the source library cannot be read, so it is not known whether another app shares the folder |
| `game_running` | a game runs (Steam cannot be closed), Steam runs this game (`Running` = 1 under `HKCU\Software\Valve\Steam\Apps\<appid>`) or a program runs from its game folder, also one Steam did not start (checked before the copy and right before the switch), or a game was started during the copy or while the client was closed |
| `same_drive` | the game is on the target drive already |
| `drive_not_suitable` | the target drive cannot take a new library (not a fixed NTFS drive, `<letter>:\SteamLibrary` not empty or an old library, a library Steam lists there whose folder is gone) |
| `library_not_created` | the new library could not be added, or Steam did not load it; `libraryfolders.vdf` was put back |
| `library_config_lost` | after the start Steam no longer listed a library from before; `libraryfolders.vdf` was put back |
| `no_space` | not enough space on the target drive (with `needed_gb`, `target_free_gb`) |
| `target_exists` | the target library has the game's folder, manifest or workshop content already |
| `copy_failed` | robocopy failed (exit code 8 or more), the copy differs from the source, the game folder has links or cannot be read, or the game could not be put in place |
| `steam_did_not_exit` | the Steam client did not exit within 150 s |
| `not_accepted` | Steam did not load the target library within 180 s, or did not show the game installed there (StateFlags 4 or 6) within 90 s after that |
| `rollback_failed` | the move failed after the switch and could not be taken back (the Steam client did not exit): the game is in the target, its old folder in the source too; `reason` names the first failure. The journal finishes or takes it back later (after a new library Steam did not take, once `libraryfolders.vdf` is put back by hand) |
| `interrupted` | a move cut off after its switch was taken back at the next start (see *Journal*) |
| `unexpected_error` | anything else |

A move interrupted by force (PC off) before the switch sends no end state: the staging folder stays
(the next move of the game mirrors it again, other moves delete it), the source is untouched. From
the switch on the journal finishes or takes it back.

An end state that cannot be sent within about 20 s (broker or network down) is kept in
`jobs\move_final.json`, as it would have gone out plus `"late": true`. The next `move.ps1` sends it
(retained) before anything else, under the job lock, and so does `report.ps1` while no Steam job runs;
the file stays until it was sent. Home Assistant handles it once by `started`, `state`, `reason_code`
and `updated` (see below; `late` is not part of that), also when the first try reached the broker
after all, and never takes it for the answer to a move just sent, also one of the same game.

**In Home Assistant** (`steam_move.py`, `steam_jobs.async_move`): a move counts as running from the
command until its first message (5 minutes at most), while `running` messages come and while the
agent reports `steam_moving`; one without a `running` message for 5 minutes counts as ended (`running`
comes also while folders are compared or deleted) and its progress push is cleared. Meanwhile Steam
counts as busy (no shutdown), installs wait in the queue and games are not started (`launch` answers
`moving`). Only an end delivered live (retain flag off) is told: the push (start and progress as kind
"progress", the end as kind "result", tag `steam_engine_move`) and the event `steam_engine_moved`; a
retained end read again after a restart of HA or a reconnect is only the last result, except for the
end of the move whose progress was pushed (it came while HA was not listening; that move is kept over
a restart of HA, `move_pushed` in the store) and one answering the move just sent (never one sent
late). An end is handled once by `started`, `state`, `reason_code` and `updated`, so the
later end of a move finished by `-Recover` (after `rollback_failed`) is told too. The download progress sensor carries the running move (`move`) and the last end
(`move_result`) for the card. Before sending, the action `steam_engine.move` answers without a command
what HA knows already: `invalid_appid`, `invalid_drive`, `not_installed`, `drive_unavailable` (the
game's drive or a target drive the PC does not report), `same_drive`, `job_running` (an install job
or a move runs, an install is being handed over, or the queue is about to move on), `game_running`,
`already_sent` (the same game within 2 minutes, until the PC answered), `agent_offline`,
`publish_failed`.

## Game start without the lock (`steam/launch_pending`)

The scheduled task "Steam Engine Lock" (`lock-at-logon.ps1`) locks the screen right after
the automatic sign-in; Windows cannot be unlocked by a script, so a game started from Home Assistant
on a switched-off PC would run behind the lock screen. With the integration option *Start games
without the lock* (settings page, section PC, off by default) Home Assistant leaves a note first:

```json
{"appid": "<appid>", "until": "2026-11-07T12:00:00Z"}
```

`appid` is the game as text of digits, `until` the end of the note's validity in UTC (ISO 8601 with
`Z`), 10 minutes after it was sent. An empty retained message clears it.

| Who | When |
|---|---|
| HA sets it | only for `steam_engine.launch` (also the *Start game* button and the card), only with the option on, and only when the start has to wake the PC (it is off): right before the wake entity is triggered. A start while the PC is on, a manual wake (`pc_wake`, the PC switch) and install or uninstall jobs never set it |
| HA clears it | once the `launch:<appid>` command was sent (or sending it failed), when the agent does not come online within the wake timeout, 10 minutes after setting it at the latest (timer), and at every start or reload of the integration (a note an earlier run left, or one left while the option was on). A clear the broker does not take is sent again every minute as long as the note may still be valid |
| The lock script reads and clears it | before it locks (autologon on, not a remote session): it reads the retained message with a hard limit of about 3 s. A valid note (`until` in exactly the form above, in the future and at most 15 minutes ahead, `appid` only digits, not the note it used last, which it keeps in `lock-at-logon.used`) means it does not lock and clears the note (empty retained). No note, an expired, used or malformed one, a timeout or any error: it locks as before |

The PC does not know the option, so the lock script always looks for the note, which can leave the
desktop open for up to about 3 s after each automatic sign-in.

The note is not signed: any MQTT client allowed to publish on `steam/launch_pending` can leave the
lock out at the next automatic sign-in. With the option on, give the broker an ACL that lets only
Home Assistant's MQTT user and the PC's user write this topic.

## PC names

All topics and entities above belong to the PC without a name, the first and usual one. The scripts
take their names from one place (`Get-SteamEngineNames` in `mqtt_publish.ps1`), from `PcName` in
`settings.psd1` (asked by the setup; empty = no name).
A named PC has topics `steam/pc/<id>/...` instead of `steam/...`,
device `steam_pc_<id>` with the PC's name, unique IDs and entity IDs `steam_pc_<id>_<key>` (the key
as at the end of the default entity IDs below, plugins `run_<name>` and `busy_<name>`). The ID comes
from the name: lower case, umlauts as two letters, only `a-z` and `0-9`, at most 16 characters
(`Kinder-PC` becomes `kinderpc`). The unnamed PC keeps all names below unchanged.

## Entities by MQTT discovery

The agent announces its entities on `homeassistant/<component>/<unique_id>/config` (retained) each
time it connects, all on the device `steam_pc` ("Steam PC"). The configs are defined once in
`mqtt_publish.ps1` (`Get-SteamEngineDiscovery`), which `Uninstall-SteamEngine.ps1` also uses to clear
them.

Plugin names (keys in `plugins.psd1`) may only contain `A-Z`, `a-z`, `0-9` and `_`; they are
lower-cased in topics, unique IDs and entity IDs. The agent notes the plugin entities it announced in
`agent\announced_plugins.txt`. When a name is no longer in `plugins.psd1`, it clears that entity's
config (empty retained message) on its next connect. An entry it leaves out because of a mistake
(e.g. a busy entry without `Processes`) keeps its entity, and nothing is cleared while `plugins.psd1`
cannot be read.

The unique IDs are `steam_pc_<key>` (`steam_pc_<id>_<key>` for a named PC) and must not change:
Home Assistant knows each entity by its unique ID, and a new one orphans the entity together with its
history, area and labels. The
integration finds the agent's entities by these unique IDs. `default_entity_id` (Home Assistant
2025.10 or newer) only names entities new to Home Assistant; existing entities keep their entity ID.

| Entity | Component | Unique ID | Default entity ID |
|---|---|---|---|
| Steam library | sensor | `steam_pc_library` | `sensor.steam_pc_library` |
| Steam job | sensor | `steam_pc_install_status` | `sensor.steam_pc_install_status` |
| Update history | sensor | `steam_pc_history` | `sensor.steam_pc_history` |
| Agent online | binary_sensor | `steam_pc_agent_online` | `binary_sensor.steam_pc_agent_online` |
| Steam downloading | binary_sensor | `steam_pc_steam_downloading` | `binary_sensor.steam_pc_steam_downloading` |
| Steam running | binary_sensor | `steam_pc_steam_running` | `binary_sensor.steam_pc_steam_running` |
| Game running | binary_sensor | `steam_pc_game_running` | `binary_sensor.steam_pc_game_running` |
| Last input | sensor | `steam_pc_last_input` | `sensor.steam_pc_last_input` |
| Network throughput | sensor | `steam_pc_network` | `sensor.steam_pc_network` |
| Steam library free space | sensor | `steam_pc_library_free` | `sensor.steam_pc_library_free` |
| Shut down PC | button | `steam_pc_shutdown` | `button.steam_pc_shutdown` |
| Restart PC | button | `steam_pc_restart` | `button.steam_pc_restart` |
| Lock PC | button | `steam_pc_lock` | `button.steam_pc_lock` |
| Close games | button | `steam_pc_close_games` | `button.steam_pc_close_games` |
| Run `<name>` (plugin command) | button | `steam_pc_run_<name>` | `button.steam_pc_run_<name>` |
| Busy `<name>` (plugin busy entry) | binary_sensor | `steam_pc_busy_<name>` | `binary_sensor.steam_pc_busy_<name>` |
