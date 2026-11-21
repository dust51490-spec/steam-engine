# Changelog

## 1.0.0 – 2026-11-21

### Changes (integration and card)

- New sensor `sensor.steam_engine_pc_state` (off, starting, on, shutting down, restarting): a tap on
  switch on shows at once that the PC is starting, on every device. The card shows it in the PC chip
  and the PC section (blue, pulsing), keeps the button busy while the action is on its way, and says
  when the PC did not come on, did not shut down or did not report back after a restart. The
  browser-side wake hold of the card is gone.
- The card's URL carries a hash of the card file besides the version (`?v=<version>-<hash>`):
  browsers and the companion app load a changed card by themselves after Home Assistant restarts,
  also within the same version, without clearing their cache.
- Muted games: a switch per installed game in the card (in the game's sheet below *Automatic
  updates*, in the tiles style next to the *auto* switch) and the action `steam_engine.set_muted`.
  A muted game's updates are not in the update push nor in its *Install all*, and
  `sensor.steam_engine_updates` does not count them (new attribute `muted`; `games` still lists
  them). The card lists them last under the updates, marked *muted*, without counting them in the
  tab, the chip or *Install all*. A muted game with automatic updates updates quietly (only a
  failure is pushed), also without *Automatic updates quietly*.

### Changes (setup)

- `Uninstall.cmd` asks whether to turn Windows autologon off too while it signs in this account
  (`Uninstall-SteamEngine.ps1 -AutologonOff` without asking). That comes first: if autologon stays
  on, nothing is removed and the lock stays. From a folder `Set-Autologon.ps1` refuses (below) it
  says how instead of asking. Kept, the uninstall warns in its dry run and its real run that the PC
  then starts with its desktop open (the lock goes with Steam Engine) and the password stays stored.
  Before, the lock went without a word.
- `Set-Autologon.ps1` no longer starts itself with administrator rights from a folder other Windows
  accounts can change (e.g. one directly under `C:\` or on another drive): it names the account and
  says to extract the zip again under the user profile. `Install.cmd` and `Uninstall.cmd` start Windows PowerShell by its full path. The
  README says to extract the zip under the user profile (e.g. `C:\Users\<you>\SteamEngine`) and to
  keep that folder.
- The setup sets up the lock before autologon comes on, `-Update -Autologon on` offers it too while
  the task is missing, and the end of the setup warns about autologon without the lock. While
  autologon stays with another Windows account the lock is not offered, as it would lock every
  sign-in of this account by hand (`-LockAtLogon on` still sets it up).
- The setup sees autologon set up for another Windows account (local name or Microsoft account): it
  warns and asks whether to set it up for this account instead (default no), and its end says so.
- `Set-Autologon.ps1 -Query` tells, for scripts, whether autologon signs in the account that runs
  it and whether `-State` would refuse to run from this folder.
- README: the commands use the paths of the extracted zip (`setup\...`; `pc\setup\...` in the
  repository), and the uninstall without asking is a full command.

### Fixes (PC)

- A PC woken for a game update shuts down again afterwards: Steam's window after `steam://validate`
  (or a client start by a job) counted as input for Windows, so the woken PC was taken for one used
  by hand. The jobs note their own Steam calls (`own_steam_calls.txt`), and the agent does not
  report an input that comes up within 30 s after one (its log says so).
- `Set-Autologon.ps1`: an administrator window closed with its X after it was done no longer reads
  "Autologon was not changed"; the result then comes from the registry (if autologon already was as
  asked, e.g. on again after a new Windows password, it says it cannot tell), and the state is
  always shown at the end. The setup's closing hint reads the state again too.
- `Uninstall-SteamEngine.ps1` keeps `%LOCALAPPDATA%\SteamEngine` with the MQTT login when not one
  topic could be cleared (Home Assistant or its broker not reachable), so a second run can clean up
  Home Assistant; it stops trying after three failures with none cleared, and names this PC's device
  when only some were cleared.
- `Install.cmd` and `Uninstall.cmd` see negative exit codes of PowerShell (e.g. the script not
  found), and say to extract the whole zip first when `setup\` is not next to them. Before,
  `Uninstall.cmd` still asked to remove.
- A package folder with square brackets in its path (e.g. `SteamEngine [0.9.0]`) no longer stops the
  setup with "Source missing".

- A download Steam goes on with (paused, held for a game, a job sent again) shows its progress from
  what Steam had staged already, no longer from 0 %.
- A first install sent again before Steam finished it (e.g. after it was held for a game) still
  counts as new (`new`): Home Assistant reports it installed, not updated.
- A game whose folder another installed app uses too (e.g. a game and its editor) is no longer
  uninstalled or moved: the new reason `shared_folder` (with texts in Home Assistant and the card).
  Before, the folder went as a whole and the other app was left without its files. A move checks
  again right before the switch, an uninstall right before deleting, and the old folder of a move
  stays when another app has it by then. A manifest of that library that cannot be read (after 3
  tries) stops the job with `steam_busy` and keeps the old folder of a move for later, instead of
  counting as no other app.
- A game or workshop folder that is itself a junction or symbolic link is neither uninstalled nor
  moved (`unexpected_folder`). An uninstall removes a link inside the game folder itself, never what
  it points to, as a move already did.
- A move or uninstall also counts a game as running (`game_running`) when Steam runs it as a second
  game (`Running` = 1, not in `RunningAppID`) or a program runs from its folder, also one Steam did
  not start (e.g. Wallpaper Engine started with Windows); a move checks again right before the
  switch, and its old folder is deleted only once no program runs from it (also when a cut-off
  move is finished at the agent's start). Before, a move deleted the files of such a program after
  the copy.
- The space check of a move counts what the game and workshop folders hold when that is more than
  Steam's `SizeOnDisk` (e.g. mods in the game folder): `no_space` before the copy instead of a full
  target drive and `copy_failed`.
- A move whose old folder could not be fully deleted no longer keeps every later move out with
  `job_running` and is never taken back, also when the game was uninstalled since: the rest is
  deleted later, unless the game or another app with that folder is in that library again. A move
  cut off after its switch whose game was uninstalled meanwhile (also in the Steam client, which
  leaves the folder with saves and mods) ends with `not_installed` instead of writing the manifest
  back and deleting the target folder: nothing is deleted, and the old folder that stays is named
  (`warning: source_not_deleted`). Only while the target library is there: another disk under the
  target's letter waits like a missing drive. A copy at the target that could not be fully deleted
  when a move was taken back is deleted at the next start without keeping moves out; a game folder
  at the target without the move's manifest is never deleted. Each move has its own journal
  (`jobs\move_pending_<appid>_<time>.json`).
- When the Steam client does not exit to take a failed move back, the move counts only when Steam
  had loaded the target library (a new one keeping every library from before), no longer because of
  the move's own copy of the manifest; else `rollback_failed`. A new library Steam did not take then
  leaves `libraryfolders.vdf.restore_pending` as for an install, and the next start finishes the
  move only when Steam lists the target library (else it takes it back), no longer deleting the
  source and reporting `ok` for a library Steam does not know.
- An uninstall of a game whose move was cut off after its switch is refused with `job_running` until
  the move is finished or taken back. Before, the old folder of the move stayed without a record.
- A move of a game with `appworkshop_<appid>.acf` but no workshop content to a library without a
  `steamapps\workshop` folder no longer fails with `copy_failed`.
- A staging folder an interrupted move left on a drive's root is deleted also after that drive got
  a library.
- `Uninstall-SteamEngine.ps1` stops while a move is not finished (naming the game, both libraries
  and a drive that is missing, with the way out when it is gone for good) and names the manifests of
  failed installs that no job removes after the uninstall.
- Every state of an install job carries the job ID Home Assistant gives the job (`job`, from the
  command `install:<job>:<appids>`), also the end kept in `install_final.json` and the `aborted` of
  `report.ps1` (from `install.lock`).
- A game whose download Steam holds because a game was started meanwhile (`Suspended` with the
  reason `Disabled (Suspended)` or none) ends as `game_running` at once, the end naming the game
  running then (`game_appid`), so Home Assistant sends it again once that game is closed; before, it
  failed after 10 minutes (`steam_error`) or an hour (`no_progress`). Held behind another download
  (`Priority (Suspended)`) the job waits on as before.
- After an error only real progress (32 MB written or read) counts as Steam getting past it, no
  longer the log lines of its retries: an error that stays ends the game with `steam_error` after
  10 minutes instead of holding the job.
- Time the PC slept during a job counts for none of its time limits: after standby a download no
  longer ends at once with `no_progress` or `download_not_started`.
- The end state of a move that could not be sent (broker or network down at its end) is kept in
  `jobs\move_final.json` and sent later (`late`) by the next move or `report.ps1`; before, it was
  lost. Home Assistant never takes such an end for the answer to a new move of the same game.
- `Uninstall-SteamEngine.ps1` names this PC's device (its PC name, "Steam PC" without one) when Home
  Assistant has to be cleaned up by hand, no longer always "Steam PC", the first PC's device.
- A first install whose `installdir` from api.steamcmd.net is not a plain folder name (the rule of a
  move, now the shared `Test-SteamPlainFolderName`) ends with `install_folder_unknown` instead of
  going into the manifest. The game's name drops control characters and escapes `\` for the vdf, so
  a name ending in `\` no longer makes the manifest unreadable for Steam.
- The manifest of a first install is written through a temporary file (`Write-SteamFileAtomic`): a
  failed write no longer leaves an empty or cut-off manifest that later jobs take for an installed
  game.
- An install job whose `libraryfolders.vdf` stays unreadable ends each game with
  `library_unreadable` (updates and first installs, see PROTOCOL.md), no longer with
  `unexpected_error`.
- A library that is no longer in `libraryfolders.vdf` at all (a move or first install took a new
  library back) counts as gone for the agent: its games start a library report at once, so Home
  Assistant no longer shows the game on the drive of the library taken back until the regular
  report. A library listed but on a drive that is not there still keeps its last list.
- `move.log` no longer says "Old staging folder ... removed." for a workshop staging folder that
  was never there (every move of a game without workshop content).
- README: how to restart the agent after editing `plugins.psd1` (*Run* in the Task Scheduler does
  nothing while it runs). CHANGELOG 0.9.0: the pushes after *Free up space* and *Other drive* show
  up to two entries and *Back*, not three; the early statements on target drives without a library
  and the preselected drive say what 0.9.0 does in the end.

### Fixes (integration)

- `game_running` of a move or an uninstall reads "a game or a program from the game's folder is
  running" in pushes and the card, as the PC counts such a program now. A move cut off whose game
  was uninstalled meanwhile: the push names the old folder that stays, and the event
  `steam_engine_moved` carries `warning` on a failure too.
- Someone who sits down at a PC an automation woke is seen right away: a last input well after the
  first one the agent reports (the logon's own; both by the PC's clock) counts, no longer only input
  five minutes after the PC came on. Before, a plugin's `pc_shutdown_when_idle` that finished within those minutes shut the
  PC down while it was used; it now also checks the last input itself right before shutting down.
- The end of a game counts like input for *Safe to shut down* and auto off, also over a restart:
  after a game played with a controller only, the idle time starts at its end, not at the last
  keyboard or mouse input.
- The idle push is removed when a game starts, and its *Shut down* button (still on another phone)
  no longer shuts down a PC that was used again or runs a game; a push says why instead, sent like
  every answer to a push button even when the kind *Problems* is not chosen.
- A wake that has not brought the PC up within 20 minutes takes its wake flag back, and a start by
  hand through the PC switch no longer counts as woken by an automation because of an old wake flag
  (auto off applied to a PC started by hand). A PC that boots slowly, after the caller of the wake
  gave up but within the 20 minutes, still counts as woken.
- A wake flag set by a wake path of one's own is kept when the PC comes on, and the wake flag is no
  longer cleared when Home Assistant starts before its MQTT connection is up.
- The minimum time after a wake also applies with a wake entity that is off again right away (a
  switch giving a pulse) and with a wake path of one's own.
- Job ends are told by their job ID, each once: an older end read again after a restart of HA or an
  MQTT reconnect no longer takes the games of a newer job of the same game off the job list (the job
  was lost); the end of the job handed over (or of the job seen running, when the PC left out a
  command sent while it ran unseen) that came while HA was not listening is told after all (result
  push in place of the progress push, event `steam_engine_job_done`); and a job's end right after the
  end of the job before was read again is no longer lost for the event and the push.
- Games not started because a game ran wait for it to close also when the agent does not report the
  game yet (the end names it, `game_appid`), instead of going out again at once and ending a second
  time; if the agent still reports no game 30 s later, it was closed meanwhile and they go out.
- The end of a move whose progress push is out is told after a restart of HA too, so its progress
  push no longer stays.
- A changed Steam ID no longer makes the games of the new account new purchases (a push with all of
  them and, with *Install new purchases* on, their install): the owned games are kept over a
  restart or reload only for the account they were checked for, so the first check of another
  account stays quiet like a first check. Without key and Steam ID the list is empty, no longer the
  last one known.
- A further PC no longer takes over the first PC's own fields when it is set up: *PC reachable* and
  *Turn on with* start empty, *Start games without the lock* off. Before, the new PC counted as on
  whenever the first one was (no wake, jobs ending with `agent_offline`, a wrong hint under Repairs).
- An action or a card without `pc` while the PC without a name is not loaded (reloading, disabled,
  failed to set up) fails with `not_loaded` instead of going to the other PC.
- A found PC that was ignored no longer counts as a PC: the PC without a name is still found, a
  further PC's settings are filled in with the first PC's, and push titles, the card editor and the
  removal of the last PC no longer count it.
- Hints under Repairs name the PC at the start of their title while there is more than one PC, as
  push titles do (the PC without a name as "Steam PC").
- "Not enough space" push: a game sent again meanwhile (in the job, the queue or waiting for a game
  to close) or, a first install, in the library now no longer counts as waiting: the buttons of its
  old push (another phone) say the push is out of date and neither delete nor install anything.
  Before, "Yes, delete" still deleted a game for it.
- "Not enough space" push: a game's button of an old "Free up space" push no longer asks to delete
  it without the game it was for (its "Yes, delete" then deleted it after all), and an old "Other
  drive" button no longer installs anything (also not for an update, which has no other drive).
- "Not enough space" push: a move from the push that ends without an end (the PC did not answer or
  went off) brings the first push again; before, every further move of that game from the push was
  ignored until Home Assistant restarted. An uninstall or move from the push whose result never came
  is forgotten after 6 hours: a later uninstall of that game no longer sends the game without space
  unasked.
- The result of an uninstall from "Yes, delete" comes also when uninstall pushes are not chosen (it
  answers a button), like the result of a move from the push.
- App IDs accept only ASCII digits (one shared check for actions, push buttons, the card's size
  query and the stored lists). Before, a digit like "²" passed, and one such entry in the job list
  made the agent refuse every later install command, also after restarts; such entries are dropped
  when the lists are loaded.
- A job end arriving while an install was still storing its target drive or purchase mark could
  remove them before the game joined the job list (installed to Steam's default library, or a new
  purchase lost its way to the overflow drive). Games being added keep both until they have joined.
- Uninstalling a game takes it out of the queue and after_game, and out of the job list while no job
  runs. Before, the PC woken for the uninstall could get the open install of the same game at the
  same time and update it, or install it again right after deleting it.
- The wake entity is not pressed a second time while the PC is still starting from our own press
  (3 minutes, until it is seen on); wakes at the same moment share one press, the PC switch does
  not press again either. A device that presses the power button no longer switches a booting PC
  off.
- The update push has its own tag (`steam_engine_updates`, before `steam_engine_progress`): the end
  of a job for another game no longer removes the announcement while its games count as announced
  (they were then never announced again). *Install all* and the last open update going away clear
  it.
- The *Launch game* button says "a game is being moved" during a move; before, it did nothing and
  silently cleared the pick.
- *Current game* keeps its game and start time while the agent's game sensor is unavailable because
  Home Assistant's MQTT connection drops or reconnects (this fired a false game end and start and
  reset the start time). The game still ends when the agent is gone (PC off).
- The game started and ended events find *Current game* by its unique ID; after renaming the sensor
  they stopped silently.
- The update check interval counts from the start of a check: a check longer than 5 minutes made
  the next hourly check fall out.
- Without an answer from api.steamcmd.net a game stays outdated only while the build it was found
  with is still installed (kept over a restart). Before, a game updated meanwhile stayed outdated:
  extra validate jobs, a warning and a false update push.
- Texts: the install action's *drive* field says that an empty field keeps a drive stored for the
  game (otherwise Steam's default library); the push kind *Uninstalled* is now *Result of an
  uninstall (done or failed)*, as turning it off turns off the failure pushes too. Removed reason
  texts that are never pushed (`settings_unreadable`, `agent_offline`, `already_sent`,
  `invalid_drive`, `moving`).

### Fixes (dashboard card)

- An open confirmation of shut down, restart or forced shut down shows the warnings that come up
  while it is open (a game, a job, a download), not only those from when it was opened.
- Times and numbers follow the time and number format of the Home Assistant profile, not only its
  language: 12/24-hour clock (`time_format`) and decimal and grouping style (`number_format`,
  including *none*) for all times, dates, sizes and rates. The card's text language is unchanged.
- The uninstall confirmation names the size of the game together with its workshop content, which
  an uninstall deletes as well (as the move dialog already counts it).
- In the install and move dialogs a drive row showing *does not fit* no longer runs out of the
  dialog on narrow phones: the free-space text wraps.
- With the PC details open, telemetry (network rate, last input) changes the values in place
  instead of rebuilding the whole PC section, so taps on its buttons and the auto-off switch are no
  longer lost.
- A disabled core entity no longer makes every open card ask `steam_engine/entities` once a minute
  forever: the card asks again only when an entity goes missing after the last answer.

## 0.9.0 – 2026-11-12

### Changes (PC)

- `sensor.steam_pc_network` has no `state_class` any more: it changes every 20 seconds, and its
  long-term statistics only grew without use. Home Assistant offers under *Repairs* to delete the
  old statistics. The README shows how to leave the two telemetry sensors out of the recorder.
- New games go to Steam's default library, the one with the star in Steam under *Settings →
  Storage* (`LastInstallFolderIndex` in the Steam account's `localconfig.vdf`), read at the start of
  every install job before it closes the Steam client. If it cannot be read, the main Steam folder
  is used, as in Steam. If the drive of the default library is not there, a first install fails
  with `drive_unavailable` instead of going to another drive. Installed games stay where they are.
- The setting `InstallLibrary` is gone, also the setup's `-InstallLibrary` and its question for the
  library; a value left in `settings.psd1` is ignored. Reason codes: `settings_unreadable` is no
  longer sent, `library_unreadable` now also stops a first install when `libraryfolders.vdf` cannot
  be read, `not_a_library` means the folder of the default library (or of the library on the target
  drive) is gone (its text changed).
- The agent sends `drives` in `steam/agent/state`, also as an attribute of
  `sensor.steam_pc_library_free`: one entry per drive with its free space, its Steam library (or none
  for an NTFS drive with at least 10 GB free, looked for every 5 minutes) and which one is the
  default for new games (see PROTOCOL.md). Drive letters are sent in upper case (`C:`).
- Shared functions `Get-SteamDefaultLibrary`, `Get-SteamDriveList` and `Get-SteamCandidateDrives`;
  `Get-SteamLibraryInfo` gives each library its block number in `libraryfolders.vdf` (`Index`).
- Target drive: the install command takes a drive letter per game,
  `install:<appid>[@<letter>],...` (e.g. `install:440@e,570,`, see PROTOCOL.md); without one the
  game goes to Steam's default library as before. The agent accepts the new form and hands it on.
  For a first install `install.ps1` uses the Steam library on that drive, by the same rule as the
  `drives` list (on a drive with several libraries the default one, else the lowest `Index`); a drive
  that is not there fails with `drive_unavailable`, a drive without a Steam library gets a new one
  (see *New Steam library* below). The space check looks at the drive picked. An installed game
  ignores the drive and stays where it is.
- The attribute `libraries` of `sensor.steam_pc_library_free` and the field `libraries` of
  `steam/agent/state` are gone; `drives` has the same values for every drive with a library.
- New Steam library: a first install to a drive without a library (fixed, NTFS, as in `drives`)
  creates `<drive>:\SteamLibrary` while the Steam client is closed for the job (shared function
  `New-SteamLibrary`), instead of failing with `not_a_library`. Both copies of
  `libraryfolders.vdf` are saved as `.bak_<time>` (older ones removed only after Steam took the
  new library), the new list is checked before and after it is
  written, and everything is put back on a failure. After the client starts the job waits until
  Steam has loaded the new library and checks that every old library is still listed; otherwise the
  old list is put back and the manifest removed (the new folder stays after
  `library_config_lost`). A list that cannot be put back leaves
  `config\libraryfolders.vdf.restore_pending`, and no new library is made while it exists. New
  reason codes `drive_not_suitable` (not a fixed NTFS drive, or `<drive>:\SteamLibrary` is not empty
  or holds an old library), `library_not_created` and `library_config_lost`. The `drives` list leaves
  out a drive whose `SteamLibrary` folder is not empty. A library once created
  stays, also when its download fails; further games of the job to that drive find it.
- Installed games: the previous owner of a game's manifest is looked for in all libraries, not only
  in the target one.
- Each entry of `drives` has two more fields: `bus`, how the disk is attached (`usb`, `nvme`,
  `sata`, `sas`, `raid`, ... in lower case, `null` if unknown), and `media`, `ssd` or `hdd` (`null`
  if unknown; NVMe counts as SSD). Both come from the physical disk(s) behind the drive letter, also
  for dynamic volumes; a volume over several disks that do not agree gets `null`. They are read
  together with the list of drives without a library (at most every 5 minutes, never in the way of
  the agent's round); a failed query gives `null` (see PROTOCOL.md).
- Each game of the library report (`steam/library`, attribute `games` of
  `sensor.steam_pc_library`) has the field `drive`: the drive letter of its library in upper case
  (`F:`), `null` when unknown (see PROTOCOL.md).
- `no_space` of an install job names the drive in upper case (`F:`), the same form as `drives` and
  the games' `drive`.
- Test switch `space_test_free_gb.txt` next to `install.ps1`: besides a plain number for every drive
  it takes one value per drive, one per line (`F:=20`); a plain number then applies to the drives not
  named (see PROTOCOL.md).
- Moving a game to another drive: the command `move:<appid>@<letter>` starts the new job
  `jobs\move.ps1` (job lock shared with install and uninstall): it copies the game folder and its
  workshop content with robocopy to a staging folder in the target library while Steam runs, closes
  the Steam client for the rest and a comparison, renames the copy, moves the manifest and starts
  Steam, which lists the game in the new library by itself (nothing is downloaded); only then the old
  folder is deleted. A drive without a library gets one (`New-SteamLibrary`). Any failure before
  takes it back, a journal (`move_pending.json`) finishes or takes back a move cut off after its
  switch. Progress and result on `steam/move`, the agent reports `steam_moving`, the library has
  `workshop_mb` per game. See PROTOCOL.md, *Move*.

### Changes (integration)

- The action `steam_engine.install` has an optional field `drive`: the letter of the
  drive for a first install, with or without a Steam library (`E`, `e` or `E:`, sent in lower case;
  empty is the same as none). The drive is stored per
  game and PC with the job lists, so it holds when the job goes out again (an interrupted job, a
  restart of Home Assistant) and while a game waits for a running game to close; it goes once the
  game has left the job, the queue and the games waiting for a game. Installs without a drive (the
  buttons, the pushes, automatic updates, new purchases) never change a stored drive. A new drive
  for a game that is only queued (or whose job waits for the PC) replaces the old one; once the PC
  has started its job, the answer is `already_queued` as before.
- Dashboard card: with two or more drives with a Steam library the install dialog offers the drive,
  each with its free space and a star (`mdi:star`) after Steam's default, which is picked at first
  when the game fits there (see the fit check below).
  The drive picked is always sent, also the default, so a star moved before the job runs does not
  change it. The tab *Space* lists the drives with a Steam library from the attribute `drives`, with
  the same star after the default drive.
- Dashboard card: the install dialog also offers the drives without a Steam library (after those
  with one), with the hint *new Steam library*; the choice shows from two drives on, so one library
  and one empty drive are a choice.
- Dashboard card: the tab *Space* also lists the drives without a Steam library (after those with
  one, in the order the PC sends them), muted, with free space and size and the hint *no Steam
  library*, without the use bar. A USB drive has a small *USB* badge after its letter, in the tab
  *Space* and in the install dialog. The icon of a drive shows its kind: the card's own SSD icon
  (Material Design Icons has none) for an SSD, `mdi:harddisk` as before for an HDD or an unknown kind
  (an older agent without `bus`/`media`).
- Texts (en, de) for the reason codes `drive_not_suitable`, `library_not_created` and
  `library_config_lost`.
- Install size of a game that is not installed: the new WebSocket command `steam_engine/game_size`
  answers `{appid, size_mb, download_mb}` (`null` when unknown), counted by the rule of the install
  job's space check (`Get-DepotSize`: public manifests of the Windows depots, without DLC, other
  languages than English and low-violence depots) from `api.steamcmd.net`. Asked only when the card
  asks; one request at a time with the update check's pause, at most 10 s, kept for 7 days in
  `.storage/steam_engine.game_sizes` (one day when no size was known, 10 minutes in memory after an
  error); requests for the same game at the same time share one fetch. The update check and the
  size use the same request function (`async_get_app_info`).
- Dashboard card: the sheet of an owned game that is not installed shows its install size right of
  the name (`…` while it is asked, nothing when unknown); installed games are unchanged. The install
  dialog says "About 2.7 GB will be installed on the PC." when the size is known, the general hint
  otherwise.
- Dashboard card: an installed game shows the drive it is on with its size, "31 GB on F:", in its
  sheet (style library) and in the list *Installed* (style tiles); the capsules of the grid keep the
  size alone and say the drive in their accessible name. In the tab *Space* a drive with a Steam
  library shows the number of its games and is a button: a tap opens the list of its games below it,
  each with its size, the largest first (one drive at a time, a tap again closes it; a library
  without games says so). Drives without a library are not buttons. From an older PC without
  `drive` per game the card shows neither the drive nor the lists.
- Dashboard card, install dialog: each drive shows its kind (*SSD*, *HDD*, nothing when unknown)
  after its letter and, when the install size is known, whether the game fits there: "1,179 GB free
  · fits" or "42 GB free · does not fit (31 GB missing)". The rule is the one of the install job's
  space check (`install.ps1`): the size plus a reserve of 10 %, at least 5 GB, against the free
  space. When the game does not fit on Steam's default drive, the first drive it fits on is picked
  (drives with a Steam library before those without, an SSD before an HDD), with the hint "F: is too
  full, E: preselected."; when it fits nowhere the default stays picked. A warning says when the
  game probably does not fit on the drive picked ("Probably not enough space on F: (31 GB missing:
  needs 73 GB, 42 GB free)."), also with a single drive that offers no choice; installing stays possible, the PC
  makes the final check. Without a known size the dialog shows no fit and no warning.
- Not enough space: a push of its own for the first game that did not fit (tag
  `steam_engine_no_space`, icon `mdi:harddisk-remove`, kind *problems*, so it comes also when result
  pushes are not chosen; the others are named in its text), titled "Not enough space on F: (SSD)" or,
  for an installed game, "No space for the update on F: (SSD)". The result push still names every
  game without space, but has no deletion buttons any more. Buttons of the new push (at most three):
  - *Free up space* when removing one installed game on the full drive frees enough (what lacks is
    `needed_gb` minus `free_gb` of the PC, the reserve included; no combinations): a push with up to
    two such games and *Back* follows, the largest first, games played in the last 14 days after
    them, games with an automatic update pending last; never the running game, an unavailable game, a game
    without space, nor a game the job, the queue or after_game holds. A game's button asks first and
    names the game that waits ("Then Cyberpunk 2077 will be installed on F: (SSD)." / "... will be
    updated."). After *Yes, delete* and a successful uninstall with enough free space (`free_gb` of
    the uninstall result) the game is sent again by itself, a first install to the drive it was meant
    for; a failed uninstall or space still short gives a short push instead. Kept in memory only.
  - *Other drive*, only for a first install, when the game fits on another drive of the PC (install
    size from `game_size.py`, free space from `drives`, the rule of the install job; drives without a
    library count): a push with up to two drives and *Back* follows ("E: HDD", "G: SSD USB"), a
    tap installs the game on that drive.
  - *View* when there is neither. If no single game frees enough, the push says so.
  Everything these buttons start goes out at once, as by hand (also outside the active hours). A
  button whose game is no longer known (installed or sent again meanwhile, or Home Assistant was
  restarted) only says so; *Yes, delete* then deletes nothing.
- Push actions are dispatched by their prefix: `STEAM_ENGINE_ROOM_<appid>`,
  `STEAM_ENGINE_OTHER_<appid>`, `STEAM_ENGINE_DRIVE_<appid>_<letter>`,
  `STEAM_ENGINE_UNINSTALL_ASK|YES_<appid>[_<waiting appid>]` and `STEAM_ENGINE_UNINSTALL_NO_0[_<waiting
  appid>]`, each with `@<id>` for a named PC.
- An automatic update the PC had no space for is not tried a second time automatically for the same
  public state (it would only push again); it is listed in the update push like other updates. Making
  room through the push sends it again and lets the automatic updates try it again.
- Drives in the pushes are named with the kind of disk from `drives`: `F: (SSD)`, `G: (HDD, USB)`,
  `G: (USB)` when the media is unknown, the letter alone when nothing is known.
- New module `space.py`: the drives of a PC (attribute `drives` of its `library_free` sensor, looked
  up like the card does), drive labels, the fit rule of the install job's space check (size plus
  max(5 GB, 10 % of the size)) and the deletion candidates; new module `no_space.py`: the push "not
  enough space" and its buttons.
- Overflow drive: a new option in the *Steam* section of the settings (`overflow_drive`, a lower-case
  drive letter, empty for none; per PC, not taken over when a further PC is set up). The list offers
  the drives of the PC's `drives` with their kind and free space ("E: · HDD · 812 GB free", "· new
  library", the default library's drive "· default library, no effect"), plus the stored drive while
  it is not reported. Only for new purchases installed by
  themselves or with the button *Install on <PC>* of the purchase push (marked in the new list
  `purchased` of the store while they are in the job, the queue or after_game; asked for by hand
  otherwise, the mark goes): when their job ends with `no_space` on the default drive or with
  `drive_unavailable` (the default drive unplugged), the game is sent once more at once with the
  overflow drive as its target, if that is another drive, it is in `drives` and the game fits there
  (`needed_gb` of the PC's entry; for `drive_unavailable` the known install size, unknown: the PC
  decides; several games of one end share its free space). The target is the loop guard: a game
  with a target never goes to the overflow drive.
  The result push of that end leaves the game out (no "not enough space", no push of its own); the
  result of the next try says "X is installed on E: (HDD), there was no space on F: (SSD) (overflow
  drive)." (or "... F: was not available ..."); a try there that fails, is skipped or waits for a
  running game says "X went to E: (HDD) (overflow drive) as there was no space on F: (SSD)." (kept
  until an end other than game_running). No space there either: the push "not enough space"
  as for any game, with a line that it was meant for the overflow drive. The button of the purchase
  push now installs as a purchase (`async_install(..., purchase=True)`).
- Moving a game to another drive: the action `steam_engine.move` (`appid`, `drive`) wakes the PC and
  sends `move:<appid>@<letter>`; it answers without a command what HA knows already (not installed,
  same drive, a drive the PC does not report, a game or a Steam job running, `already_sent`). The
  move (`steam_move.py`) keeps Steam busy, installs wait in the queue and games are not started
  (`launch` answers `moving`) while it runs. Pushes for its start, progress and result (tag
  `steam_engine_move`), the event `steam_engine_moved`, and the attributes `move` and `move_result`
  of the download progress sensor for the card.
- Dashboard card: *Move* in the sheet of an installed game (library style, `mdi:folder-move-outline`)
  when the PC reports another drive and no Steam job or move runs. Its dialog lists the other drives
  like the install dialog (kind, free space, *USB*, *new Steam library*, fits or how much is missing
  by the rule of `move.ps1`: size plus workshop content plus the reserve of an install), the first
  fitting drive picked (libraries first, an SSD before others). While a move runs the card shows it
  like a job (route, step, percent; chip *Moving 42 %*; the game tagged *moving*), no game is started
  and no other move offered; its end is a message. A move counts as a job in the PC area.
- Not enough space: a third way, *Move a game*, when a game on the full drive frees enough alone
  and fits on another drive (`space.move_targets`): a push with up to two such games (the order of
  *Free up space*) and *Back*, a game's button a push with up to two drives it fits on and *Back*,
  a drive's button starts the move at once. After a successful move with enough space the game that
  did not fit is sent again by itself, as after *Free up space*; the move's result push says so, or
  after a failure that it is not installed (then also when result pushes are not chosen). New push
  actions `STEAM_ENGINE_MOVE_<appid>`, `STEAM_ENGINE_MOVEGAME_<appid>_<waiting appid>` and
  `STEAM_ENGINE_MOVETO_<appid>_<waiting appid>_<letter>`. Texts en and de.

## 0.8.0 – 2026-11-10

### Changes

- Notifications: choose which kinds of pushes come (job started and progress, result, new updates,
  new purchases, uninstalled, idle PC and automatic shut down, auto off reminder, problems), all on
  by default; answers to a button of a push always come. *No pushes* sends nothing (no warning in
  the log any more); the events stay.
- *Automatic updates quietly* (off by default): jobs of only games with automatic updates push
  nothing while they run or when they went well; a push comes on too little space or when the
  second try failed too. The update push leaves the automatic updates out.
- The integration zip of a release holds the license too.

## 0.7.1 – 2026-11-10

### Changes

- License: PolyForm Noncommercial 1.0.0 instead of MIT. Commercial use is not allowed.
- The free space sensor (`sensor.steam_pc_library_free`) no longer expires: while the PC is off it
  keeps the last known values, and the card shows them as the last known state.

## 0.7.0 – 2026-11-09

### Changes (integration)

- Every name of a PC (MQTT topics, the agent's unique IDs, the own entity IDs, push tags and
  actions, store, label, device name) comes from one place (`pc.py`); tests pin the names of the PC
  without a name.
- Every action has an optional field `pc` (the name of the PC). Without it, the action goes to the
  only PC, else to the PC without a name, else it says that a PC is needed; an unknown name says
  which PCs there are. With one PC nothing changes.
- Several PCs: Steam Engine can be added once per PC. The first PC has no name; adding another one
  asks for the name it got in its setup, then for its settings (filled in with the first PC's). Each
  PC has its own device ("Steam Engine <name>"), entities (`switch.steam_engine_<id>_pc`, ...),
  MQTT topics, agent entities, job lists and image cache; the dashboard card's WebSocket commands
  take `pc` like the actions. The PC's name shows in push titles only while there is more than
  one PC.
- One Steam account on several PCs: a new purchase is reported once, by the PC without a name (else
  the first one set up), with a button per PC that lacks the game ("Install on <PC>"); only that
  PC installs new purchases by itself. Every event has `pc` (the PC's name, empty for the PC
  without a name).
- Dashboard card: one card per PC, option `pc` (the card editor offers it once there is more
  than one PC). New WebSocket command `steam_engine/pcs`.
- A PC whose agent runs but that is not set up yet shows as found under *Devices & services*
  (MQTT discovery of `agent/info`), the first PC as well as further ones.
- Per named PC: pushes have a tag and button actions of their own (a button acts only on its PC),
  and hints under Repairs, the job lists in `.storage` and the label "PC busy" are kept apart.

### Breaking

- The agent's entities have English unique IDs now, `steam_pc_<key>` like their entity IDs (e.g.
  `steam_pc_lock` instead of `steam_agent_sperren`); plugin entities `steam_pc_run_<name>` and
  `steam_pc_busy_<name>`.

### Removed

- Upgrade paths from older versions, which nobody needs before the first public release: the setup no
  longer cleans up SteamCMD leftovers or the task "Steam Engine Library" of 0.2 and no longer warns
  about agents before 0.7; `InstallMethod`/`SteamCmdDir` in `settings.psd1` are no longer filtered;
  a plain-text `password=` line in `mqtt_credentials.txt` is no longer read (the setup writes the
  encrypted one, `-MqttLogin` enters it again); install jobs of 0.4 are no longer waited for; the
  integration no longer strips a saved `notify.` prefix when it sends, no longer ignores progress
  times without offset, and the attribute `with_free_games` of `sensor.steam_engine_not_installed`
  is gone. The card's option `collapsed` is no longer mentioned (unknown options are ignored). The
  "Upgrading from" sections of this changelog became "Behaviour changes" where they held some.

### Changes (PC side)

- Several PCs: the scripts take their MQTT topics and entity names from one place
  (`Get-SteamEngineNames` in `mqtt_publish.ps1`). The uninstall clears only this PC's topics and
  entities.
- The agent sends on each connect who runs as this PC (`steam/agent/info`, retained, with the
  computer name), see PROTOCOL.md.
- A new setup asks for the PC's name (`PcName` in `settings.psd1`; Enter = none, the first PC) and
  checks with the broker that no other computer already runs under that name; it stops unless you
  confirm.
- A `settings.psd1` that is there but cannot be read (or a name without letters or digits) now stops
  the agent, the jobs and the library report with a line in their logs, instead of running as the
  PC without a name, which could take over the jobs of another computer.

## 0.6.0 – 2026-11-08

### Changes (integration)

- Start a game on a switched-off PC without the lock at logon (integration option *Start games
  without the lock*, settings page, section PC, off by default). When a game is started from Home
  Assistant and the PC has to be woken for it, the integration first leaves a retained note on
  `steam/launch_pending` (game and a validity of 10 minutes, see PROTOCOL.md); the lock script reads
  it after the automatic sign-in, leaves the lock out once and the game starts. The integration
  clears the note once the start command was sent, when the PC does not come, after 10 minutes at
  the latest and at each start of the integration (a clear that fails is tried again every minute
  while the note is valid). A start while the PC is on, a manual wake and
  other jobs never set it. Security trade-off: until you lock it, anyone at the PC can use it. Needs
  the PC side of 0.6.
- Events for automations of your own: `steam_engine_job_done`, `steam_engine_uninstalled`,
  `steam_engine_updates_found`, `steam_engine_purchased`, `steam_engine_game_started` and
  `steam_engine_game_ended`, with the games as `{appid, name}` (see README, Events). They are fired
  whether pushes are set up or not.
- *Sync now* in the card (was *Check for updates*) and the new action `steam_engine.refresh`: asks
  the PC for its game library first (when the agent is online), then checks for updates and new
  purchases; the card says how many of each it found.
- The services are registered once when Home Assistant starts, so they stay available while the
  integration reloads (after a settings change); a call while it is not loaded says so instead of
  "service not found".
- The hint *Agent not running* under Repairs comes exactly when the agent has been silent for 30
  minutes while the PC is reachable, and goes as soon as it reports (before: checked every 5 minutes).
- The Steam sign-in hint no longer reads the reason text of PC sides before 0.5 (only the reason
  code).
- Internal: one base class for all entities (unique ID, device, write on change), the keys of the
  entities as constants, the start button as its own class; tests also run against the oldest
  supported Home Assistant (2026.8.0).

### Changes (PC side)

- The lock at logon (`lock-at-logon.ps1`, task "Steam Engine Lock") looks for the note on
  `steam/launch_pending` before it locks, with a hard limit of about 3 s (it reads the retained
  message with the agent's settings and stored MQTT login). A valid note (game as digits, validity
  not over) leaves the lock out once, is written to `lock-at-logon.log` and is cleared. No note, an
  expired or malformed one, a timeout or any error: it locks as before. The PC does not know the
  option, so the desktop can stay open for up to about 3 s after each automatic logon.
- Shared library: `Get-MqttRetainedMessage` reads the retained message of one topic.

## 0.5.0 – 2026-11-06

A dashboard card of its own: add **Steam Engine** to a dashboard and it shows the running game, the
job, updates, installed and owned games and the update history, and installs, updates, starts and
uninstalls the game you tap. Its default look is a game library with the games' Steam artwork and a
sheet per game; a second look with tiles is an option. It also controls the PC: switch on, shut
down, restart, lock, close the running games, shut down anyway, auto off, and a short status. Real
game images: the integration looks up the image URLs in the Steam store, so newer games no longer
show a grey box. Active hours: automatic updates and purchases can be limited to a time of day (off
by default). A sturdier PC side: SteamCMD is gone, every job ends for Home Assistant even after an
unexpected error, the agent no longer stops or reconnects in a loop because of a locked log or a
typo in `plugins.psd1`, and a paused download in Steam no longer keeps the PC on for good. And a
sturdier integration: no shutdown between two games of a job, no update sent twice, no queue held
up by a job the PC never finished, and a wake flag that is reset without the optional "PC
reachable" sensor.

### Behaviour changes

- SteamCMD is removed, also as an option: the Steam client is the only installer (the setup has no
  `-InstallMethod` and `-SteamCmdDir` any more, the uninstall no `-SteamCmd`).
- The PC's logs (`install.log`, `uninstall.log`, `report.log`, `steam-agent.log`,
  `lock-at-logon.log`) are written as UTF-8 and move to `<name>.log.old` at 1 MB.
- Of the old script versions only the newest `.bak_<time>` per script stays, and in
  `agent\task_backup` the newest three definitions per task.
- Plugins: names in `plugins.psd1` may only contain letters, digits and `_`, and a busy entry needs
  `Processes`. The agent leaves out other entries and says why in its log; the setup shows it at
  its end. `mqtt_publish.ps1` has new shared functions (all with `Steam` or `Mqtt` in the name,
  plus `Get-AcfValue`, `Read-FileTail` and `Read-SharedTextFile`, see the README); a plugin must not
  define functions of the same names before it dot-sources it.
- `steam_engine.uninstall`, `steam_engine.launch` and `steam_engine.set_auto_update` take exactly
  one app ID (a list or `440,550` is refused with an error). `steam_engine.launch` without a
  response fails with "A game is already running on the PC" while a game runs.
  `steam_engine.check_updates` and `steam_engine.check_owned` take no fields.
- The PC switch says why it does nothing: switching it on without a wake entity is an error, and
  switching it off while Steam works or a plugin keeps the PC busy is an error that names the
  reason (the push still comes). Switching it on while the PC is on does nothing.
- The push about new updates no longer comes while a job runs: it waits for the end of the job and
  then lists the updates still open. Its tag stays `steam_engine_progress`.
- `steam_engine.install` answers `reason: already_queued` for games the running job or the queue
  has already.
- Removing the integration also deletes its lists (`.storage/steam_engine.lists`).
- The picture of `sensor.steam_engine_current_game` is the image URL from the Steam store once
  the integration has looked it up (the old CDN path until then). Home Assistant asks
  `store.steampowered.com` for the images of your games (no key, no account data).
- The new section **Active hours** under *Configure* is off by default.

### Changes (integration)

- Game images from the Steam store: the old fixed image paths of the Steam CDN give many newer games
  (e.g. Battlefield 6) a grey placeholder, so the dashboard showed a grey box. The integration now
  asks the store's public API (`appdetails`, no key) for the real image URLs of the games it knows
  (installed, with an update, owned and not installed, running): one game every 2 seconds, starting
  a minute after Home Assistant started, a new game about half a minute after it appeared, waiting
  5 minutes (doubling up to 2 hours) when the store refuses. The answers are kept in
  `.storage/steam_engine.images` (removed with the integration) and asked again after 30 days, a game
  without images on the store after 7 days. The picture of `sensor.steam_engine_current_game` uses
  them (the old path while there is no answer). New WebSocket command `steam_engine/images`
  (`{"images": {"<appid>": {"header", "capsule"}}}`, only games with an answer), the subscription
  `steam_engine/subscribe_images` (the same map right away and again whenever answers were added or
  changed, open to users who are not administrators) and the event `steam_engine_images_updated`
  when answers were added or changed (for automations; Home Assistant lets only administrators
  subscribe to it). The dashboard card uses the subscription, so it shows new images for every user
  without polling; while Home Assistant starts it tries again after 5, 15 and 60 seconds, and at
  once when the integration's entities appear or the connection is back. No new entities.

- New settings section **Active hours** (off by default, 08:00 to 20:00 preset): automatic updates
  (with their second try), the automatic install of new purchases and switching the PC on for them
  start only inside this time of day. What comes up outside it waits in the queue without waking
  the PC and starts by itself when the active hours begin (an update found at 22:00 starts at 08:00),
  also after a restart of Home Assistant; games that need nothing any more by then are left out.
  Everything started by hand (card, buttons, *Install all*, `steam_engine.install`, `uninstall`,
  `launch`, `pc_wake`) runs at once, and the update and purchase checks keep running at any time. A
  job running when the active hours end is not stopped; automatic games queued behind it wait for
  the next start. The window may cross midnight; the same start and end means all day; it follows
  Home Assistant's time zone across daylight saving time changes. The update push counts waiting
  automatic updates with their start time ("1 runs automatically at 08:00"), the purchase push says
  when the install starts. `sensor.steam_engine_updates` has the new attributes `active_hours`
  (`{start, end}` as `HH:MM`, or null while off), `deferred` (app IDs waiting) and `deferred_until`
  (their start as local ISO time, or null). Games waiting for the active hours stay in the queue
  (`queue` of `sensor.steam_engine_download_progress`); the store keeps which of them automatic work
  added (lists `auto_added`, `auto_added_new` for new purchases). If the PC cannot be woken when
  the window opens, they stay in the queue; an automatic update of a game uninstalled meanwhile is
  dropped. The dashboard card marks waiting games as *waiting*, shows a line with their start
  ("Waiting for active hours – starts at 08:00") and the window under *Details*; its *Update*,
  *Install* and *Install all* start such a game at once. No new entities.

- New dashboard card **Steam Engine** (`custom:steam-engine-card`, see *Dashboard card* in the
  README): status (running game, job, queue, PC on or off), a PC section (PC on or off, agent online
  or offline; switch the PC on while it is off, shut it down, restart and lock it, the first only
  while it is off and the others only while the agent is online; shut down and restart ask first
  and warn about a running game, the restart also about a busy Steam; a shut down refused because
  Steam or a plugin keeps the PC busy shows the reason, and the push still comes), updates with
  *Install all*, installed games with auto-update switch, *Start* and *Uninstall*, owned games with
  *Install* and the switch for new purchases, and free space. Every game button calls the integration's action with the app ID of
  its row, so a list that changes in between can no longer hit the game next to it; uninstall and
  install ask first with the game's name, and the card says with the game's name when a job was
  started, queued, rejected (with the reason) or done. Options `title`, `sections`, `show_images`
  and `actions_when_pc_off` (show the buttons while the PC is off, a tap switches it on; on by
  default), all but `sections` also in the card editor. English and German. The
  integration serves the card itself and loads it into the frontend (no resource entry, no HACS
  frontend repository); `frontend`, `http` and `websocket_api` are new dependencies in
  `manifest.json`. The card finds
  its entities with `steam_engine/entities`, so renamed entities work. No new entities.
- The card's PC section also shows a short status list (Steam running, Steam busy, last input,
  network throughput, safe to shut down with the planned auto off time, a busy plugin), the switch
  *Auto off when idle*, a button to close the running games (only while the agent is online and a
  game runs; it asks first and names the game) and, set apart, *Shut down anyway*: it presses the
  agent's shut down button, so the PC shuts down even while Steam downloads, a job runs, a plugin
  needs it or a game runs; its confirmation lists which of these apply right now. New section
  *Update history* (`history`) with the last 10 entries of the agent's
  history, newest first. `steam_engine/entities` names the agent's new close games button
  (`close_games_button`); the card shows that button only once the PC side is on 0.5.0 (see
  *Changes (PC side)*).
- The card looks like Home Assistant's own tile cards instead of a column of fold-out sections:
  coloured tags for the state of each game (no icon per row), status chips (PC, download, updates,
  free space), the running game with its header image, the job as a tile with a progress bar, and
  the PC area with tinted buttons; both are always shown at the top. The PC's status tiles are
  behind *Details* (closed at first, remembered in the browser); while a job, a download of Steam
  or a plugin keeps the PC from shutting down, one line under the PC state says so. The lists (free
  space, updates, installed, history, owned) are tabs below, in the order of `sections`, which now
  starts with `free_space` so the card does not open on the long installed list; the tab you pick
  is remembered in the browser, the updates tab only shows while there are updates. Colours,
  radii and fonts come from the theme, so light, dark and own themes fit. The option `collapsed` is
  deprecated: it is still accepted and ignored. Inside, the card is split into a logic part and a
  render part, so another look can be added later without touching the logic.
- The card has a second look, `style: library`, which is the default (also in the card editor;
  `style: tiles` keeps the tiles look, an unknown value shows the library): the running game, or the download while no game runs, as a large
  header image with the progress, the PC as a slim bar with the same buttons, *Details* and the
  awake line, and the lists as segments in the same order (free space first, choice remembered).
  Installed, updates and owned games are a grid of Steam capsule images with small state badges;
  games with an update are framed, *Install all* and *Check for updates* stay. A tap on a game opens
  a sheet from the bottom with its picture, state, size and last played and all its actions (start,
  update, automatic updates, uninstall or install; uninstall and install still ask first). The
  images load lazily from Steam's CDN; with `show_images: false`, or when Steam has no image, the
  grid shows tinted tiles with the names. A progress tick no longer rebuilds parts of the card that
  did not change, so images are not loaded again.
- Settings: emptied text fields (Steam Web API key, Steam ID, the two links) are saved empty; before,
  the old value came back, so owned games could not be switched off again.
- Settings: the notify service may be entered with or without "notify." in front; a changed one
  that does not exist is refused instead of silently dropping every push.
- The list of owned games is no longer written to the recorder (large accounts went over its
  attribute limit); the sensor state, the number of games, still is.
- Steam busy stays on between two games of a job (the result of one game, then app info, space
  check and the library report before the next state), so `steam_engine.pc_shutdown_when_idle`
  (e.g. after a backup) and the PC switch no longer shut the PC down in the middle of a job. It is
  also on while a job is being handed to the agent. One definition of a running job is used by the
  busy sensor, the queue and the pushes.
- The wake flag is reset when the PC is off for two minutes, also without the optional "PC
  reachable" sensor (then the agent's reports tell). Before, the flag stayed on after the first
  automatic wake, so every later manual start was shut down by auto off without asking. A wake
  within those two minutes keeps it, and a wake right after a shutdown wakes the PC once it is off.
- An automatic update is no longer sent again while its own job runs (it was queued and then
  validated a second time); `steam_engine.install`, "Install all" and new purchases leave out games
  a job or the queue has. The decision about automatic updates moved from the pushes to the jobs,
  and the limit of two tries per public state of a game survives a restart; a game that used them
  up is listed in the update push again, and the log says so once.
- A job that the PC never finished (switched off, crashed) no longer holds up later installs:
  while the PC is off or the agent offline, new games are sent instead of queued. `aborted` moves the
  queue on, and its push names the open games instead of app IDs.
- The end state read again after a restart of Home Assistant or an MQTT reconnect is compared with
  the job by its result lists: games it names leave the job list, the queue moves on, and the rest
  is sent again once the agent is online (the PC leaves out games of a job that still runs). Before,
  an old `done` could drop the queue or keep finished games that the next install validated again.
  A job is accepted only when the PC reports a state of its games.
- A job that could not be handed over (agent unreachable) gets a push and is sent once the agent is
  online; a job interrupted by a reload of the integration goes out again too.
- An update check asked for while one runs follows right after it, so a game updated just now is
  no longer left as outdated (and updated again automatically).
- The stall push no longer claims that the reporting script sends nothing while it runs; it shows the
  phase and how long the PC stays on. After a restart of Home Assistant the 6 h limit of a stuck job
  continues from the PC's time of the last progress instead of starting over. The stall push goes
  away when the job goes on or ends.
- The result push names the reason of each failed game (from `reason_code` since 0.5, e.g. "the
  Steam client did not sign in"), and says "already running in the Steam client" instead of "Update
  done" when nothing was done. The progress push keeps install or update as known at the start.
- The Repairs hint "Steam client not signed in" uses `reason_code`; the old text match stays for a
  PC side of 0.4. The uninstall result knows `unexpected_error`.
- The silent agent push no longer comes while Home Assistant's MQTT connection is down.
- Jobs started without waiting (services without a response, buttons, pushes, the queue, checks)
  belong to the integration's entry: a reload stops them instead of leaving them running next to the
  new instance, and their errors and the stop are logged.
- A renamed agent entity or entity of the integration reloads the integration, so auto off, the
  queue and the pushes follow the new entity ID.
- The owned games are compared also when no game is installed yet (the list of games to install was
  empty for a new user), only when the set of installed games changes, and without a warning every
  hour when no Steam Web API key is set. A game without a name gets the translated "Game <id>".
- The update check runs when its interval has passed since the last check (before, at hours divisible
  by the interval, so 5 h gave a gap of 4 h at midnight), and "query" no longer shows `stale` between
  two checks of a long interval.
- The safe-to-shut-down sensor shows no `off_at` while something other than the idle time keeps the
  PC on, the idle prompt is not sent again after a restart or options change, and the network limit
  also works when the network sensor shows another unit than B/s.
- Disabling the busy sensors or the PC switch no longer stops auto off or the shutdown lock: the
  logic reads them directly instead of through their states. Newly labelled "PC busy" entities count
  at once (no 30 s poll).
- The games of a job keep the order in which they were asked for (before, sorted by app ID).
- "Current game" no longer shows a start time from before a restart of Home Assistant when that game
  is started again later (the time is kept only while the game still runs).
- The account check in the settings asks Steam once more after an empty answer before it says the
  profile is private (Steam sends that now and then for a public profile too). The check in the
  settings and the owned games use the same request now.
- The settings page no longer says "You can change them later under Configure" while you are there.
- New WebSocket command `steam_engine/entities` for dashboard cards: the current entity IDs of the
  integration's entities (by the key of their unique ID) and of the agent's (including its free
  space sensor and its restart and lock buttons as `library_free`, `restart_button` and
  `lock_button`), so a card keeps working after entities were renamed.
- The workflow *Release package* runs all checks of *Validate* first and packs only if they pass;
  *Validate* also compares `strings.json` with `translations/en.json` and the keys and placeholders of
  `translations/de.json`, and checks the dashboard card with Node (`node --check` and the tests in
  `tests/js`).

### Changes (PC side)

- SteamCMD removed: no install method, no SteamCMD download or login in the setup, no
  `steam_login.txt`. Install folder, name and size of a game still come from api.steamcmd.net, a
  public web API.
- Every job ends for Home Assistant: an unexpected error now ends it with `done_with_errors`
  (`reason_code` `job_failed`, the games not handled yet in `failed`). Before, Home Assistant could
  stay at "running" until its restart and queued every later install. If MQTT is down at the end,
  the end state is kept and sent later instead of "aborted".
- One Steam job at a time, safely: install and uninstall share a lock (the PID check before could be
  fooled by a PID used again after a power cut, and dropped the job without a message). An install
  command for games the running job has, or a job that ended since the command came, is ignored, so
  a command Home Assistant sends again while an uninstall runs installs the games once. Other games
  wait for a running install job, and up to 10 minutes for anything else (then `done_with_errors`,
  `job_running`). An install job of 0.4 still running during the update counts as running.
- The library report runs once at the end of a job that installed or updated a game (before: after
  every game), with a time limit of 5 minutes.
- Updates of installed games also run when api.steamcmd.net does not answer (without the space
  check); only a first install needs it. The cheap local checks now come before that web call.
- A paused or queued update in Steam no longer counts as busy: a job starts it instead of skipping
  it, it can be uninstalled, and it no longer keeps the PC from shutting down for good (the agent
  counts it only while Steam wrote to its download folder within 15 minutes).
- Uninstalling a game never leaves the Steam client closed, also when it exits late. The library
  report after an uninstall runs once (started by the agent) instead of twice.
- A `settings.psd1` that cannot be read is logged, and first installs are refused (`reason_code`
  `settings_unreadable`) instead of going to the main Steam folder unnoticed. An `MqttPort` that is no
  number now means 1883 and a log line instead of a stop of every script.
- A log that cannot be written (locked by a reader such as `tail -f`, disk full) no longer ends the
  agent or a job; all logs rotate at 1 MB.
- Job states carry a `reason_code` on `error` and `skipped` (codes in PROTOCOL.md; the English
  `reason` stays). `error` is always about one game. `updated` is UTC with offset now.
- Agent: an error while collecting the state is logged and no longer drops the MQTT connection, a
  command that fails (e.g. `launch` without a handler for `steam://`) is logged, and only a deliberate
  exit ends the agent. Connection errors name their cause, a refused login is tried less and less
  often (up to every 5 minutes), a repeated error is logged once, and errors at its start land in
  `steam-agent.log`. A subscription the broker refuses is logged.
- Agent: the last input is written independent of the PC's language settings. With e.g. Finnish
  settings it was no valid time, so auto off and the idle prompt never acted. The same for the
  history times.
- Agent: a busy entry without `Processes` no longer sends the agent into an endless reconnect loop,
  invalid plugin names are left out with a warning, a plugin command whose script is missing is
  refused with a log line, and the entities of plugins removed from `plugins.psd1` disappear from
  Home Assistant.
- Agent: its time limits run in UTC, so the change to or from summer time no longer causes a
  reconnect or an hour too much of "busy".
- Agent: new command `close_games` and button *Close games* (`button.steam_pc_close_games`,
  unique ID `steam_agent_close_games`). It closes every game Steam runs right now (also several at
  once): first it asks them to close, after 10 s it stops the ones still running. The Steam client
  and tools in `steamapps\common` that Steam did not start are left alone. The state follows about
  3 s after the end. An agent of 0.4 ignores the command and has no button, so the card's *Close
  the running games* appears only after the PC update.
- Agent: less work every 20 seconds (manifests read only when they changed, the default route looked
  up every 5 minutes, the Steam registry key read once). A network rate that cannot be measured is
  logged once, and `network_adapter` is empty meanwhile.
- Library report: reads only the end of the history and the job logs, counts games removed by an
  uninstall from Home Assistant as `ha` in the history, sorts by UTC, and logs why it stopped.
- Setup: `-MqttHost` and `-InstallLibrary` change an existing `settings.psd1`, also with `-Update`;
  the library must be one of the Steam client's libraries. `-New` offers the old values and keeps
  the port. The hints name `Install.cmd -Update -...` (without `-Update` the whole setup runs again).
- Uninstall: stops while a game is being installed or uninstalled, stops the agent before it clears
  the retained topics (before, the agent's last will put "offline" back), also clears plugin
  entities, and a broken `plugins.psd1` no longer stops it.
- Autologon: with a Microsoft account, "only allow Windows Hello sign-in" is turned off only after
  the password check, and on again if autologon cannot be set up.
- The workflow *Validate* also checks the PC side: every script parses, `pc/` is ASCII only, the
  `.cmd` files have CRLF.

## 0.4.0 – 2026-10-30

Easier to start with: the integration's entities have a device of their own, games can be
installed, uninstalled and started from the device page without scripts, the settings check the
Steam account, Settings → Repairs says what is wrong, and the PC setup sets up Windows autologon
itself. Needs Home Assistant 2026.8 or newer.

### Changes

- The entities of the integration belong to a device "Steam Engine" now, shown as connected through
  the agent's device "Steam PC". Entity IDs stay the same. The device name now comes first in an
  entity's name (e.g. "Steam Engine Current game"), so three names lost their "Steam": "Updates",
  "Last answer", "Query".
- Install and uninstall games without your own scripts: pick a game in "Game to install" or
  "Installed game" and press "Install game" or "Uninstall game"; "Auto-update picked game" switches
  automatic updates of the installed game picked (five new entities on the device "Steam Engine").
  A pick lasts 10 minutes, so a forgotten one is not acted on later. Picking a game
  to install needs the Steam Web API key and SteamID64. The README has a dashboard example with
  built-in cards only.
- Start a game from Home Assistant: the action `steam_engine.launch` and the button "Launch game"
  (for the game picked in "Installed game") wake the PC if needed and start the game through the
  agent (`launch:<appid>`, `steam://rungameid`); Steam starts first if it does not run. Not while a
  game runs. Handy with Steam Remote Play or Steam Link on another screen.
- The PC setup sets up Windows autologon itself (`Install.cmd -Autologon on|off`; a new setup asks
  while autologon is off), so Sysinternals Autologon is not needed any more. Like that tool it checks
  the password with a real logon and stores it encrypted as the LSA secret `DefaultPassword`, never
  in plain text; only this part asks for administrator rights. Microsoft accounts work too (e-mail
  address and account password), and the setup offers to turn off "only allow Windows Hello
  sign-in". `off` also deletes the stored password. `pc\setup\Set-Autologon.ps1 -State on -DryRun`
  only checks the password. Not yet tried with a Microsoft account on a real PC.
- Free-to-play games you have played once now count as owned, so they show up among the games that
  are not installed and can be installed from Home Assistant. The first check after the update takes
  them in quietly (no "new purchase" push). Free games never played still need "Play" in Steam once.
- Fixed: the push "Steam Engine is not reporting" came right after waking the PC when it had been
  off for more than about 10 minutes. The 10 minutes now count from the PC switching on.
- Firmware and build guide for a Wake-on-LAN box (`hardware/wol-box/`): a WT32-ETH01 with ESPHome
  that switches on a PC on Wi-Fi through a cable into its LAN port.
- Each release also has `steam_engine-<version>.zip` with the integration, for installing it without
  HACS.
- The settings check the Steam Web API key and the Steam ID with Steam before saving them (only when
  they changed) and say what is wrong. The Steam ID field also takes the profile address or its
  custom name and saves the SteamID64.
- Hints under Settings → Repairs for problems you have to fix: the PC side has not reported yet, Steam
  rejects the key, the profile shows no games, the Steam client on the PC is not signed in, the agent
  does not run although the PC is reachable. Each goes away by itself once fixed.
- After the agent's first start the integration reloads by itself to pick up its entities (no more
  reloading by hand).
- Fixed: the help text under "Push to" on the settings page showed "Translation error" instead of
  the text (an example in angle brackets).

## 0.3.0 – 2026-10-28

The Steam client installs the games now instead of SteamCMD, the update check also finds updates of
single depots, and the PC side got smaller and safer: one scheduled task, the MQTT password
encrypted, and optionally the screen locked after an autologon. New agent entities get English
entity IDs, and the leftovers of the unreleased 0.1 are gone.

### Current game

- New `sensor.steam_engine_current_game`: the name of the Steam game running on the PC (from the
  library, "Game <appid>" if it has none), unknown when no game runs. Attributes `appid` and
  `started` (kept over a restart of Home Assistant while the same game runs); the entity picture is
  the game's header image from the Steam CDN, loaded by the browser. Use it in cards, or trigger on
  it to start a scene per game. Only games started through Steam are seen (the agent reads the
  running app ID from Steam); the PC side needs no update for it.

### Installing and updating with the Steam client

- Games are installed and updated by the Steam client instead of SteamCMD. A first install writes a
  minimal appmanifest and restarts the client, an update runs `steam://validate/<appid>` (this also
  pulls in an update Steam had put off). Owned DLC is installed too, no Steam login is stored, and the
  client is no longer logged off. Progress comes from the Steam content log and the bytes `steam.exe`
  writes. Install folder, name and size of each game come from api.steamcmd.net, which the PC now
  asks itself (SteamCMD's `app_info_print` before); if it does not answer, the game fails with
  "install folder unknown", also for an update.
- New setting `InstallMethod` in `settings.psd1` (`client` by default). SteamCMD is used only with
  `InstallMethod = 'steamcmd'`, never as an automatic fallback. The setup downloads SteamCMD only
  for that method; `Install-SteamEngine.ps1 -InstallMethod steamcmd` also writes the setting into an
  existing `settings.psd1` (its hint to run the setup again without `-Update` comes only when SteamCMD
  or its login are missing), and the setup uses the SteamCMD folder named there. A setup with `-New`
  keeps the method and SteamCMD folder chosen before unless they are given again.
- An update stays in the library the game is in; only first installs go to the library for new games,
  which has to be a library Steam knows.
- A first install is not started while any game runs (the client cannot restart then), and an update
  of the game being played is not started (an automatic one is not even sent). The job result lists
  such games under `game_running` and the job ends as `done_with_errors`; the push says so, and the
  integration starts them by itself once no game runs and the agent is online (also after the PC or
  Home Assistant was restarted in between). Sending the game again or uninstalling it with Steam
  Engine in the meantime takes it off that list; an install or uninstall in the Steam client itself
  does not.
- A job starts the Steam client if it is not running. Steam errors that Steam retries by itself (a
  short connection loss) end a job only if Steam does not get past them within 10 minutes; a started
  download is never thrown away. A game fails when Steam makes no progress for 60 minutes (less than
  32 MB written or read), when the Steam client is closed for 2 minutes, or when a client the job
  started does not log on within 3 minutes. A game that Steam does not start within 5 minutes (not
  owned, Steam offline) fails, and its manifest is removed, or by the next job if that is not possible
  right then (kept in `jobs\pending_manifest_cleanup.txt`; that job closes and restarts the Steam
  client to remove it, not while a game runs); until then the agent does not count it as a download,
  so it does not block the shutdown. A client the job shut down is always started again.

### Update check

- The update check also finds updates that change a single depot but keep the build ID (Steam can
  keep such an update waiting for days, and the check used to report none). The PC sends the
  installed depots with their manifest IDs on the new retained topic `steam/depots` (cleared by
  `Uninstall-SteamEngine.ps1`), and the integration compares them with the public manifests from
  api.steamcmd.net.
- Automatic updates are sent at most twice per public state of a game (counted since Home Assistant
  or the integration was last started), so a game that the check keeps seeing as outdated after
  Steam reported it fine (e.g. a beta branch) is not updated again and again.

### Libraries, drives and paths

- A Steam library on a drive that is not there (USB disk unplugged) no longer breaks the agent's
  state report, the library report, an uninstall or a job. Its games stay in the library with their
  last known data (from `library\library_cache.json`, written by every report; after the update from
  0.2 only once a report of 0.3 has seen the drive) and `unavailable: true` (not reported as removed;
  left out of update checks and deletion suggestions). An install of such a game fails with the
  reason "the drive of the library <path> is not available" instead of installing it a second time
  elsewhere, an uninstall with the new reason code `drive_unavailable`; the agent still notices
  uninstalls in the other libraries, and the setup marks the drive as not available. A library folder
  that was deleted while its drive is there counts as empty.
- The agent, the library report and the install and uninstall jobs read the libraries through one
  function (`Get-SteamLibraryInfo` in `mqtt_publish.ps1`): `libraryfolders.vdf` read shared and as
  UTF-8, tried again while Steam writes it (and an error instead of a list without the other
  libraries; an uninstall then fails with the new reason code `library_unreadable`), no duplicate
  main folder (the library report listed games in the main Steam folder twice when the registry and
  the vdf differ in case), and `-LiteralPath`, so `[ ]` is no wildcard (with `[ ]` in the library path
  an uninstall reported "not installed"; with `[ ]` in the game folder name it removed only the
  manifest and left the game folder behind). The uninstall reads the manifest as UTF-8 (a game folder
  with non-ASCII characters was not found).

### PC side: security and fewer parts

- The MQTT password on the PC is stored encrypted with Windows DPAPI (`password_dpapi=` in
  `mqtt_credentials.txt`), readable only for the Windows user who ran the setup, on that PC. The
  setup encrypts a password still in plain text on every run, also with `-Update`; a hand-written
  `password=` line keeps working until then. New switch `-MqttLogin` enters the MQTT login again.
  The logs say why a login cannot be used (file missing, password not readable for this user), and
  the setup and the uninstall script no longer try to log in to the broker then. A hand-written file
  in ANSI is read as ANSI (a non-ASCII password is not garbled before it is encrypted). Plugins that
  read the login with `Get-MqttCredentials` need no change.
- One scheduled task less: the agent starts the library report itself, a minute after its start and
  every 30 minutes (a report that runs longer than 5 minutes is stopped), besides the existing starts
  after an uninstall and on the command `library`. The setup removes the task "Steam Engine Library"
  (its definition is saved in `agent\task_backup`), the uninstall script still removes it if present.
- New option `Install-SteamEngine.ps1 -LockAtLogon on`: the task "Steam Engine Lock" locks the screen
  right after every logon of this Windows user at the PC itself while Windows autologon is set up
  (a sign-in by hand too; nothing in a remote desktop session or without autologon), so a PC woken by
  Home Assistant does not stand open; Steam and the agent keep running. `-LockAtLogon off` removes
  it, the uninstall script too. A setup without `-Update` also warns, while autologon is off, when
  "only allow Windows Hello sign-in" would keep autologon from working.
- Setup: works with only the main Steam folder as library (the list became a single string, and the
  choice "1" picked its first letter), checks the library number, writes `settings.psd1` as UTF-8
  with all quote characters doubled and reads it back, and copies the shared `mqtt_publish.ps1`
  first.
- Ready-made package for the PC: each release has a `SteamEngine-PC-<version>.zip` with
  `Install.cmd` (set up or update by double-click; the window stays open at the end) and
  `Uninstall.cmd` (shows what it would remove, then asks). Built by a GitHub Action that also checks
  the tag against the version in `manifest.json`.
- Setup: a new MQTT login is tried with the broker before it is saved (three tries; a wrong password
  asks for the login again, an unreachable broker for its address if that was asked in the same run,
  otherwise whether to save it unchecked); a kept login is tried on every run and only warns. A setup
  without `-Update` asks whether to lock the screen after an automatic logon (unless the task exists
  or `-LockAtLogon` is given). At the end it checks that the agent runs and says what is left to do in
  Home Assistant (add the integration on a first setup, otherwise the same version).
- Setup, library for new games: asked right below the list of libraries (numbered "1 = <path>"),
  three tries for a valid number; with only one library there is no question. The paths are shown
  and saved as the file system spells them (Steam keeps its own folder in lower case).

### Entity IDs, names and texts

- The agent and `install.ps1` announce their entities with `default_entity_id` instead of the
  deprecated MQTT `object_id` (Home Assistant 2025.10 or newer; this integration needs 2026.3
  anyway). Entities new to Home Assistant get English entity IDs such as `sensor.steam_pc_library`,
  `binary_sensor.steam_pc_steam_downloading` and `button.steam_pc_shutdown` (all of them in the
  README); entities that already exist keep their entity IDs. The discovery unique IDs and topics
  stay as they are and are documented as frozen. Until the agent has run once, the integration
  looks for its entities under these new entity IDs too.
- The PC scripts connect with new MQTT client IDs: `<pc>-steam-job`, `<pc>-steam-uninstall`,
  `<pc>-steam-library` (`-depots`, `-history`) and `<pc>-steam-job-check`. A broker that only lets
  in known client IDs needs them.
- German: "PC" instead of "Büro-PC" and other mixed wording in the pushes, umlauts fixed, and the
  push "PC bleibt an" points to the shut down button of the PC. The German push channel of the PC is
  now called "PC", so Android creates a new channel; set its sound and importance again. The German
  entity names follow the English ones (e.g. "Steam beschäftigt", "Sicher zum Abschalten", "Nicht
  installierte Spiele"); entity IDs do not change, dashboard cards without a name of their own show
  the new names.
- English: the settings section "Office PC" is now "PC".
- An outdated game without a name is listed as "Game <appid>" in the language of Home Assistant
  (it was always the German "Spiel <appid>").
- The service examples use app ID 440. PROTOCOL.md lists only the current names, with a table of the
  discovery unique IDs and default entity IDs.

### Removed

- The one-time takeover of the YAML setup of the unreleased 0.1 (its input_boolean and input_text
  helpers for the switches and job lists, and fixed entities for "PC reachable" and waking the PC).
  Updating from that setup is no longer supported, updating from 0.2.0 is not affected.
  `input_boolean` is no longer an after-dependency.
- The German label ID `pc_beschaftigt`: only the label "PC busy" (label ID `pc_busy`) keeps the PC
  from shutting down.
- Restoring the attributes `besitz` and `letzter_fortschritt` of versions before 0.2 after a restart.
- `Install-SteamEngine.ps1` no longer moves an installation of 0.1 to the current layout and no
  longer clears its MQTT topics; `Uninstall-SteamEngine.ps1` no longer removes its folders, tasks and
  topics. `report.ps1` no longer reads history lines with the German keys of 0.1 and no longer
  renames its history files.
- The German switch aliases: use `-Update` and `-New` (`Install-SteamEngine.ps1`) and `-Yes`
  (`Uninstall-SteamEngine.ps1`) instead of `-Aktualisieren`, `-Neu` and `-Ja`.

### Fixes

- Two library reports at once (the one `install.ps1`/`uninstall.ps1` runs after a game and the one
  the agent starts for Home Assistant in the same second) added history entries twice and shared
  their MQTT client IDs. `report.ps1` now runs one at a time; a second one waits up to 2 minutes for
  the first and is dropped after that.
- A job that finished within a second (skipped at once) was not seen as accepted, so the
  integration sent it twice more, by then as an empty `install:` command. New games are also queued
  while a job is between two games.
- The push "Steam Engine is not reporting" names the scheduled task "Steam Engine Agent" (it still
  said "SteamAgent", the name of 0.1).
- Errors of the Steam Web API are logged without the request URL, so the API key and the SteamID no
  longer end up in `home-assistant.log`.

## 0.2.0 – 2026-10-22

First public version.

- Integration with one settings page: update check, install and uninstall jobs with a queue, new
  purchases, waking the PC, auto off, idle prompt, reminder and the PC switch with a shutdown lock.
- Entity names, attributes and push notifications in English and German, following the language of
  Home Assistant. New installs get English entity IDs (`switch.steam_engine_pc`, …).
- PC side: Steam agent, SteamCMD job scripts and library report in `%LOCALAPPDATA%\SteamEngine`,
  set up and updated by `Install-SteamEngine.ps1`, removed by `Uninstall-SteamEngine.ps1`.
- MQTT protocol in English, see [PROTOCOL.md](PROTOCOL.md).
- Plugins: entities with the label "PC busy" keep the PC on; scripts listed in `plugins.psd1` can be
  started from Home Assistant.

0.1 was an unreleased predecessor.
