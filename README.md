# Steam Engine

Home Assistant integration that keeps the Steam games on a Windows PC up to date and switches the PC
on and off for it.

- Checks your installed Steam games for updates every hour and has the Steam client install them, also
  games you bought on another device (optional).
- Wakes the PC when there is work to do (Wake-on-LAN box, smart plug, script, anything with a
  button or switch in Home Assistant) and shuts it down again once it has been idle long enough.
- Asks by push notification whether to shut down a PC that someone started by hand and then left
  idle, and never shuts down while Steam downloads, a game runs or a plugin needs the PC.
- Pushes with buttons: install all updates, delete a large game when space runs out, shut down.
- A dashboard card of its own: your Steam library with its artwork, updates, installs and the PC's
  buttons, with no extra download.
- Optional active hours: automatic updates and installs only at a time of day you choose.
- Start a game from Home Assistant on a switched-off PC: it wakes, skips the lock once (optional)
  and the game is running when you sit down.
- Events for automations of your own: job done, update found, new purchase, game started or ended.
- One settings page in Home Assistant for all wait times.

> **Status: early beta.** It runs on one setup so far. Names and notifications are in English or
> German, following the language of Home Assistant.

## How it works

```
Home Assistant                                Windows PC
  Steam Engine integration  <---- MQTT ---->    Steam agent (scheduled task at logon)
  (logic, settings, pushes)                     install.ps1 / uninstall.ps1 (Steam client)
                                                report.ps1 (library and update history,
                                                  started by the agent every 30 minutes)
```

The PC side is a small PowerShell agent. It reports the Steam library, the download state, the last
input and the network traffic by MQTT discovery and takes commands (install, uninstall, shut down,
restart, lock). The integration decides when to wake the PC, what to install and when to shut down.
No Home Assistant token is stored on the PC. The messages are described in [PROTOCOL.md](PROTOCOL.md).

Update checks and new purchases are looked up online (api.steamcmd.net, Steam Web API), so the PC
stays off until there really is something to do. The update check compares the build of each game and
the manifest of each installed depot, so it also finds updates that change one depot and keep the
build.

## Requirements

- Home Assistant 2026.8 or newer with the MQTT integration (e.g. the Mosquitto broker app).
- A Windows 10/11 PC with the Steam client and Windows PowerShell 5.1. The Steam client has to sign
  in by itself when it starts (account remembered, not in offline mode), because it installs the games.
- Something in Home Assistant that can switch the PC on: a button, switch or script. For example the
  button of the *Wake on LAN* integration (needs the PC's MAC address; over Wi-Fi it usually works only
  from sleep, not from off; for a PC on Wi-Fi see the [Wake-on-LAN box](hardware/wol-box/README.md)),
  a smart plug with "power on after AC loss" in the BIOS, or a small device that presses the power
  button. Optional: a binary sensor that shows whether the PC is
  reachable (e.g. Ping); without one, the agent's reports tell whether the PC is on.
- The Home Assistant Companion App for push notifications (optional: without it, choose *No pushes*).
- Optional: a Steam Web API key (steamcommunity.com/dev/apikey) and your Steam profile to notice new
  purchases and to pick games to install (without them the list of games that are not installed
  stays empty). The game details of the profile must be public. Games shared with you through Steam
  Family are not in that list (the Steam Web API only returns your own games); once installed, they
  are kept up to date like all others.

## Installation

### 1. Home Assistant

Home Assistant 2026.11 or newer, with the built-in Marketplace: [open this repository in the
Marketplace](https://my.home-assistant.io/redirect/marketplace_repository/?owner=dust51490-spec&repository=steam-engine&category=integration), or *Settings → Marketplace → Add from link* (upper right) and enter
`dust51490-spec/steam-engine`. Adding a repository from a link needs the GitHub connection of the
Marketplace (it only reads public data). Then install **Steam Engine** and restart Home Assistant.

Home Assistant 2026.8 to 2026.10, with HACS: *HACS → ⋮ → Custom repositories*, add this repository
as type *Integration*, then install **Steam Engine** and restart Home Assistant. After the update to
2026.11 the Marketplace takes it over and keeps updating it.

Manually: download `steam_engine-<version>.zip` from the release, unzip it into
`/config/custom_components/` (e.g. with the Samba share or the File editor app; you get
`/config/custom_components/steam_engine/`) and restart. To update, replace that folder with the one
of the new release and restart.

Then add it under *Settings → Devices & services → Add integration → Steam Engine* and fill in the
settings page (see below).

### 2. The PC

Download `SteamEngine-PC-<version>.zip` from the
[releases](https://github.com/dust51490-spec/steam-engine/releases) (the same version as the
integration), extract it into a folder under your user profile, e.g. `C:\Users\<you>\SteamEngine`,
and double-click **`Install.cmd`** in the extracted folder. Not directly under `C:\` or on another
drive (nor into a *Downloads* folder moved to another drive): other Windows accounts can change files
there, and the autologon part, which runs as administrator, does not start from such a folder; then
extract the zip again under your user profile. Keep the extracted folder: later changes (below) and `Uninstall.cmd` need it (or
download the zip of the same version again). Windows may ask once whether
to run a file from the internet; choose *Run*. With **Smart App Control** on (Windows 11), Windows
blocks the downloaded scripts, which are not signed: before extracting, right-click the zip,
*Properties*, tick *Unblock* at the bottom, *OK*, then extract it (again).

The commands below are run in the extracted folder (a command prompt or Windows PowerShell opened
there); `Install.cmd` is the same as:

```powershell
powershell -ExecutionPolicy Bypass -File setup\Install-SteamEngine.ps1
```

Instead of the zip you can also use the `pc` folder of a clone of this repository (cloned into a
folder under your user profile too): it holds the same files, so from the repository's root the commands start with
`pc\setup\` instead of `setup\`.

Options of the setup (below) can be given to `Install.cmd` too, e.g. `Install.cmd -Update -MqttLogin`
in a command prompt (without `-Update` it runs the whole setup again). The setup asks for the MQTT broker address and login and tries the login with the
broker before it saves it (a wrong password or address is asked for again), asks whether to lock the screen after an automatic logon (see below) and
creates the scheduled task "Steam Engine Agent" (the agent also sends the library every 30 minutes).
At the end it says
whether the agent runs and reaches Home Assistant, and what is left to do there. The MQTT password is
stored encrypted with Windows DPAPI for your Windows user, so run the setup as the user the scheduled
tasks run as. That keeps it from other Windows accounts and from copies of the file (backups, another
PC), not from programs running as your own user. MQTT on port 1883 sends the login unencrypted over
the network: use an MQTT user just for Steam Engine. To enter a new MQTT login later:

```powershell
powershell -ExecutionPolicy Bypass -File setup\Install-SteamEngine.ps1 -Update -MqttLogin
```

To change the MQTT address later:

```powershell
powershell -ExecutionPolicy Bypass -File setup\Install-SteamEngine.ps1 -Update -MqttHost 192.168.1.10
```

The setup changes only that value in `%LOCALAPPDATA%\SteamEngine\settings.psd1` and keeps the old
file as `.bak_<time>`. You can also edit the file (all keys are in
[`pc/setup/settings.example.psd1`](pc/setup/settings.example.psd1)), or run the setup with `-New`,
which asks for everything again, the MQTT login too.

New games go to Steam's default library: the one with the star in Steam under *Settings → Storage*.
Move the star there to change it; Steam Engine reads it before every job. If it cannot be read (no
Steam account found, for example), the main Steam folder is used, as Steam itself would. If the
drive of the default library is not there (a USB disk unplugged), a first install fails instead of
going to another drive. Games that are installed stay where they are. A single game can go to another
drive with a Steam library: pick it in the install dialog of the dashboard card, or give the action
`steam_engine.install` the field `drive` (its letter, e.g. `E`). A drive that is not there fails with
`drive_unavailable`.

A drive without a Steam library can be picked too, if it is a fixed NTFS drive (the card offers those
with at least 10 GB free). The first install there creates the library: while the Steam client is
closed for the job, Steam Engine makes the folder `<drive>:\SteamLibrary` and adds it to Steam's
list of libraries (`libraryfolders.vdf`), as Steam's *Add drive* does. A drive that has a folder
`SteamLibrary` with something in it is never used for that (`drive_not_suitable`). The safety net:
both copies of `libraryfolders.vdf` are saved first (`.bak_<time>` next to them), the new list is
checked before and after it is written, and after the client starts Steam Engine waits until Steam
has loaded the new library and checks that every old library is still in Steam's list. If Steam does
not take the new library, the old list is put back and the game fails with `library_not_created`;
if Steam lost one of the old libraries, the old list is put back too (`library_config_lost`). In
that case `<drive>:\SteamLibrary` stays on the drive, and the drive is not offered again until you
delete that folder or add it in Steam's storage settings. If the old list cannot be put back (the
Steam client did not close), Steam Engine writes `libraryfolders.vdf.restore_pending` in Steam's
`config` folder with the backups to copy back by hand, and creates no new library until that file is
deleted. A library once created stays, also when the download fails later; further games to that drive go to
it. While a game runs the Steam client cannot restart, so the install waits for the game to close,
as every first install does.

Games are installed and updated by the Steam client itself: a first install writes a small
`appmanifest_<appid>.acf` into Steam's default library and restarts the client, an update is
started with `steam://validate/<appid>` in whatever library the game is in. Owned DLC comes along,
and no Steam login is stored on the PC. The progress comes from the Steam log and the bytes the
client writes. While a game runs, a first install (and an update of that game) waits and starts by
itself once the game is closed.

Install folder, name and size of a game come from api.steamcmd.net (a public web API, no SteamCMD
on the PC). If it does not answer, an update of an installed game still runs (without the space
check); only a first install fails.

After the agent's first start the device **Steam PC** appears in Home Assistant, and the Steam Engine
integration reloads by itself to pick up the agent's entities. Until then *Settings → Repairs* says
that the PC side has not reported yet.

For waking to be useful the PC has to start without anyone logging in: Windows autologon. The setup
offers to set it up while it is off or set up for another Windows account (then it warns: Steam
Engine of your account would not start after a wake-up). It works like
[Sysinternals Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon): the password
is checked with a real logon first and then stored encrypted by Windows (as the LSA secret
`DefaultPassword`), never in plain text. Windows asks for administrator rights for this part, and the
password is typed into that window. A Microsoft account works too: it signs in with its e-mail
address and account password (not the PIN; a passwordless Microsoft account needs a password first),
and the setup offers to turn off "only allow Windows Hello sign-in", which would hide the password
sign-in. Domain and work or school accounts are not supported. After changing the Windows password,
run it again. To try it without changing anything, run
`powershell -ExecutionPolicy Bypass -File setup\Set-Autologon.ps1 -State on -DryRun`.

With autologon, the setup offers to lock the screen right after each logon at the PC itself while
autologon is set up, so a woken PC does not stand open (Steam and the agent keep running; a remote
desktop session is not locked; without autologon the lock does nothing). It sets the lock up before
autologon comes on, `-Autologon on` offers it too while it is missing, and the end of the setup
warns about autologon without the lock. While autologon signs in another Windows account, the lock
is not offered: it would lock every sign-in of your account by hand (`-LockAtLogon on` still sets it
up). To turn either on or off later (`off` turns autologon off and deletes the stored password, or removes the
lock):

```powershell
powershell -ExecutionPolicy Bypass -File setup\Install-SteamEngine.ps1 -Update -Autologon on
powershell -ExecutionPolicy Bypass -File setup\Install-SteamEngine.ps1 -Update -LockAtLogon on
```

Before it locks, the lock script looks for a note from Home Assistant that a game start woke the PC
(see *Start games without the lock* under [Settings](#settings)). It does this after every automatic
logon, because the PC does not know whether the option is on, so the desktop can stay open for up to
about 3 s before the lock. Without a valid note, or when the broker cannot be reached, it locks as
before.

### More PCs

One Home Assistant can look after several PCs, each with its own Steam Engine entry. Set the first
PC up as above; it needs no name. For every further PC:

1. On that PC, run `Install.cmd` and give it a name when the setup asks (e.g. `Kids PC`). The setup
   checks that no other computer uses that name.
2. In Home Assistant, the PC shows as found under *Settings → Devices & services* once its agent
   runs: *Add*, then its settings follow, filled in with those of the first PC except the PC's own
   fields: set *PC reachable* and *Turn on with* for this PC, and *Start games without the lock* if
   wanted. (Or add the integration again by hand: it asks for the PC's name, the same as on the PC.)

Each PC gets a device "Steam Engine <name>" with entities of its own (`switch.steam_engine_<id>_pc`,
...; the ID is the name in lower case, letters and digits only). The actions take an optional field
`pc`; without it they go to the PC without a name (while its entry is not loaded they fail, they
never go to another PC). The PC's name shows in push titles and hints under Repairs only while there
is more than one PC. When several PCs share one Steam account, a new purchase is reported once, with
a button per PC to install it there; only the PC without a name (else the first one set up) installs
new purchases by itself. A dashboard card shows one PC: add a card per PC and set its `pc` option.

### Updating

Update the integration in the Marketplace (HACS before Home Assistant 2026.11; the manual way: replace
the folder) and restart Home Assistant (then reload the browser page so the dashboard card is the
new one too). Then update the PC side with the scripts
of the same version: extract the new `SteamEngine-PC-<version>.zip` (in your user profile, as
above; keep this folder instead of the old one) and double-click `Install.cmd` (it keeps your
settings and MQTT login), or run in that folder:

```powershell
powershell -ExecutionPolicy Bypass -File setup\Install-SteamEngine.ps1 -Update
```

It only replaces changed scripts (the old one stays as `.bak_<time>`, only the newest per script)
and restarts the agent. At the end it shows what the agent warns about at its start (e.g. an entry
of `plugins.psd1` it left out). If the setup stops with an error, fix the cause and run the same
command again.

### Uninstalling

Double-click `Uninstall.cmd` in the extracted folder: it shows what it would remove and asks before
it does. The same without asking, run in that folder (add `-AutologonOff` to turn autologon off
too, see below):

```powershell
powershell -ExecutionPolicy Bypass -File setup\Uninstall-SteamEngine.ps1 -Yes
```

It removes the scheduled tasks, stops
the agent, removes the agent's entities (plugin entities too) in Home Assistant and deletes
`%LOCALAPPDATA%\SteamEngine` (without `-Yes` it only shows what it would do). If not one entity
could be removed in Home Assistant (Home Assistant or its MQTT broker not reachable), that folder
stays with the MQTT login: run it again once Home Assistant is reachable. While a game is being
installed, uninstalled or moved, or a move is not finished (cut off, or the old folder of a moved
game not fully deleted yet), it stops, says what to do (also for a drive that is gone for good) and
asks you to run it again later. Games and the Steam client stay, and so do the files of your
plugins; their entries in `agent\plugins.psd1` go with the folder, so keep a copy if you set Steam
Engine up again.

Windows autologon stays unless you turn it off: while it signs in your account, `Uninstall.cmd`
warns and asks whether to turn it off too (Windows asks for administrator rights). That comes first:
if autologon stays on (e.g. the Windows prompt declined), nothing is removed and the lock stays. From
a folder other Windows accounts can change it cannot turn autologon off; it says so instead of
asking (extract the zip again under your user profile and run `Uninstall.cmd` there). Kept, Windows
signs in by itself at every start, and without the lock of Steam Engine the PC then stands open; the
Windows password stays stored (encrypted by Windows).
"Only allow Windows Hello sign-in", if the setup turned it off, stays off (*Settings → Accounts →
Sign-in options*).

Then remove the integration under *Settings → Devices & services*.

## Settings

*Settings → Devices & services → Steam Engine → Configure*

| Section | What you set |
|---|---|
| PC | "PC reachable" sensor, the entity that switches the PC on, and *Start games without the lock* (off by default, see below) |
| Notifications | Companion App notify service or *No pushes*, which kinds of pushes come, *Automatic updates quietly*, page to open when tapping a notification |
| Auto off | Idle time, quiet time, minimum time after waking, network limit |
| Idle prompt | When to ask about a PC that was started by hand |
| Reminder | Reminder while auto off is switched off |
| Steam | Update check interval, stall limit, page to open, Steam Web API key and Steam ID, overflow drive |
| Active hours | Whether automatic work is limited to a time of day, and from when until when (off by default; 08:00 to 20:00 preset) |

**Notifications:** under *Pushes about* you choose the kinds: job started and progress, result of a
job, new updates, new purchases, result of an uninstall (done or failed), idle PC (shut down?) and automatic shut down, reminder
that auto off is switched off, and problems (not enough space, stalled or aborted job, agent silent
or not reachable, PC not shut down). All are on by default. The answer to a button of a push (e.g.
*Delete?* after *Not enough space*) always comes. Without the idle kind nobody is asked about a PC switched on by
hand; one woken by Home Assistant still shuts down by itself. *No pushes* sends nothing; the
[events](#events) stay for your own automations.

**Not enough space:** the result push names each game that did not fit with its drive and the kind
of disk, e.g. *F: (SSD)*, *G: (HDD, USB)* (a job that ended only with such games has no result
push, the push below says it all). The first of them also gets a push of its own, *Not
enough space on F: (SSD)* (*No space for the update* for an installed game), with what it needs and
what is free. It is a problem push, so it comes also when result pushes are not chosen. Its buttons:

- **Free up space**, when removing one installed game on that drive frees enough (no combinations):
  a push with up to two such games follows, the largest first, games played in the last 14 days
  after them, games with an automatic update pending last; never the running game, nor one a job,
  the queue or a running game holds. Tapping a game asks first and names what follows ("Then
  Cyberpunk 2077 will be installed on F: (SSD)."); after *Yes, delete* the game is uninstalled, and
  once that went well and the drive has enough space, the game that did not fit goes out again by
  itself (a first install to the drive it was meant for, an update as it was). If the uninstall
  fails or the space is still short, a short push says so. If no single game frees enough, the push
  says so instead.
- **Move a game**, when one of those games also fits on another drive (its size and workshop
  content plus the reserve of an install, as the PC counts it for a move): a push with up to two
  such games follows (in the same order), tapping one shows up to two drives it fits on (drives
  with a Steam library first, an SSD before others; *E: HDD*, *G: SSD USB*), tapping a drive moves
  the game there at once (no question: a move deletes nothing before the game runs from its new
  place). Its result push says what follows: once the move went well and the drive has enough space,
  the game that did not fit goes out again by itself ("Cyberpunk 2077 is being installed now."); if
  the move fails, the push says so ("So Cyberpunk 2077 is not installed.") and comes also when result
  pushes are not chosen; if the space is still short, a short push says so.
- **Other drive**, only for a game that is not installed yet, when it fits on another drive of the
  PC (its install size known, by the rule of the PC's space check; drives without a Steam library
  count, the install creates one): a push with up to two such drives follows ("E: HDD", "G: SSD
  USB"), tapping one installs the game there.
- **View** when there is neither.

The pushes that follow have a **Back** button that brings the first push again; *Cancel* of the
question does the same.

At most three buttons (Android shows no more): *Free up space*, *Move a game*, *Other drive*.
What these buttons start goes out at once, as asked for by hand, also outside the active hours.
They are kept in memory only: a button whose game is no longer known (installed or sent again
meanwhile, or Home Assistant was restarted) only says so, *Yes, delete* then deletes nothing and a
drive of *Move a game* moves nothing.
An automatic update without space is not tried again by itself for the same update (it is
listed in the update push like any other) until room is made through the push.

**Moving a game to another drive** (the card's *Move* in a game's sheet, the push above, or the
action `steam_engine.move`): the PC copies the game folder (and its workshop content) to the Steam
library on the other drive while Steam runs, closes the Steam client for a moment to copy what
changed and to compare, puts the game's manifest in the new library and starts Steam again; Steam
then lists the game there by itself, nothing is downloaded or validated. Only then is the old folder
deleted; a failure before takes everything back, the old folder is never touched until the game runs
from its new place. A drive without a Steam library gets one (`<drive>:\SteamLibrary`). It needs
the space of the game plus the reserve of an install on the target drive. Not while a game runs
(Steam cannot be closed then), a program runs from the game's folder (e.g. a tool started with
Windows), Steam works on the game (download, queued or paused update) or another Steam job runs,
and not when another app uses the same folder (e.g. a game and its editor); meanwhile Steam counts
as busy (no shut down), installs wait in the queue and games are not started. Pushes: the start
and the progress (kind *progress*), the result (kind *result*); the event `steam_engine_moved`. How
it works on the PC: [PROTOCOL.md](PROTOCOL.md), *Move*.

Games with their own launcher or anti-cheat (e.g. the EA app for Battlefield) may have to be told
the new place in that launcher (or repair the game there) after a move; this is not tested.

**Overflow drive** (*Steam* section of the settings, *None* by default, set per PC): a drive the PC
reports (with its kind, free space and *new library* for a drive without a Steam library; the drive
of the default library is marked *default library, no effect*; a drive stored before stays in the
list while it is unplugged). It is only used for new purchases that are
installed by themselves (*Install new purchases automatically*) or with the button *Install on
<PC>* of the purchase push: when the PC reports no space for such a game on Steam's default drive,
or that drive is not there, the game is sent once more, at once, to the overflow drive, if that is
another drive, the PC reports it, and the game fits there (by the rule of the PC's space check; for
an unplugged default drive with the install size if known, otherwise the PC decides). The result
push of that job leaves the game out; the next one says "Cyberpunk 2077 is installed on E: (HDD),
there was no space on F: (SSD) (overflow drive)." If that try fails or waits for a running game,
the result says "Cyberpunk 2077 went to E: (HDD) (overflow drive) as there was no space on F:
(SSD)." Several games of one job share the free space of the overflow drive. It is tried once:
if there is no space on the
overflow drive either, the push *Not enough space* follows as for any game, saying it was meant for
the overflow drive. Games you install by hand (the card, the actions, the buttons of other pushes)
and updates never go there.

**Automatic updates quietly** (off by default): a job of only games with automatic updates pushes
nothing while it runs, when it goes well or when it is aborted (it goes on by itself). A push comes
when a game had too little space, or when its second try failed too; the first failure is retried
quietly. The update push then leaves the automatic updates out. A job with a game you asked for
yourself pushes as usual.

**Muted games** (a switch per installed game in the card, next to *Automatic updates*, or the action
`steam_engine.set_muted`): for a game you keep but hardly use. Its updates are not announced (no
update push, *Install all* of that push leaves it out) and not counted by
`sensor.steam_engine_updates`; the card still lists it under the updates (last, marked *muted*) so you
can update it by hand. With automatic updates on as well it updates quietly, as with *Automatic
updates quietly*: only a failure is pushed. An update you start yourself pushes as usual.

**Active hours** limit only what Steam Engine starts by itself: the automatic updates (with their
second try), the automatic install of new purchases, and switching the PC on for them. Everything
you start yourself (the dashboard card, the buttons, the *Install all* button of the update push,
the actions `steam_engine.install`, `uninstall`, `launch` and `pc_wake`) runs at once, also outside
them. The checks for updates and purchases go on at any time (the PC stays off), and the update push
still comes. What comes up outside the active hours is not lost: it waits in the queue (without
switching the PC on) and starts by itself when they begin, e.g. with 08:00 to 20:00 an update found
at 22:00 starts at 08:00 the next morning; the update and purchase pushes say when. A game that
needs nothing any more by then (updated or installed by hand meanwhile, automatic updates switched
off) is left out. This also holds over a restart of Home Assistant: inside the active hours, what
waited starts right away. A job that is running when they end is not stopped; automatic games
queued behind it wait for the next start. The time may cross midnight (22:00 to 06:00); the same
time for start and end means all day. The times follow the time zone of Home Assistant, also on the
days daylight saving time changes. `sensor.steam_engine_updates` shows the setting and what waits
(see [Entities](#entities)).

The Steam ID field takes the SteamID64, the address of your Steam profile or its custom name; it is
saved as SteamID64. When you change the key or the Steam ID, Steam Engine checks both with Steam
before it saves them and says what is wrong (key rejected, profile not found, game details not
public). If Steam cannot be reached, a SteamID64 is saved unchecked. With another Steam ID the list
of owned games starts anew: its first check reports no new purchases.

**Start games without the lock** (section PC, off by default) is a security trade-off. With the lock
at logon set up on the PC (see [The PC](#2-the-pc)), a game started from Home Assistant on a
switched-off PC would run behind the lock screen. With this option on, Home Assistant leaves a note
for the PC (valid for 10 minutes) before it wakes the PC for `steam_engine.launch` (also the *Start
game* button and the card); the lock script finds it after the automatic logon, leaves the lock out
once and the game starts. The desktop then stays open until you lock it or sign out: anyone at the PC
can use your Windows account, Steam and everything you are signed in to. Only a start that has to wake
the PC sets the note; a start while the PC is on, waking it by hand and install or uninstall jobs
never do. Whoever can publish to your MQTT broker can set the note too, so give the broker its own
users and an ACL that lets only Home Assistant and the PC write `steam/launch_pending`. Details in
[PROTOCOL.md](PROTOCOL.md).

Problems you have to fix yourself show up under *Settings → Repairs* and go away by themselves once
fixed: the PC side has not reported yet, Steam rejects the key, the profile shows no games, the Steam
client on the PC is not signed in (a job failed for that), and the agent does not run although the
PC is reachable (needs the "PC reachable" sensor).

## Entities

Entity names follow the language of Home Assistant (English and German so far). The integration
brings its own [dashboard card](#dashboard-card); you can also use the entities in any card you
like, or start with the [dashboard example](#dashboard-example). Its entities belong to the device
"Steam Engine", the agent's to the device "Steam PC". "Steam Engine" is shown as connected through
"Steam PC" from the first start of the integration after the agent has reported.

From the integration:

| Entity | Meaning |
|---|---|
| `switch.steam_engine_pc` | The PC: on while it is reachable or the agent reports. On wakes it (nothing while it is on), off shuts it down unless Steam or a plugin keeps it busy (then an error with the reason, and a push) |
| `sensor.steam_engine_pc_state` | The PC with what is on its way: `off`, `starting` (from the press of the wake entity until the PC is on, at most 3 min), `on`, `shutting_down` (from a press of the agent's shutdown button until the PC is off, at most 3 min), `restarting` (from a press of its restart button until the agent is back, at most 5 min). For dashboards: the PC switch only turns on once the PC reports |
| `switch.steam_engine_auto_off` | Main switch for auto off and the idle prompt |
| `switch.steam_engine_woken_by_automation` | Set when the integration woke the PC; only then auto off applies. Reset when the PC is off for two minutes or someone uses it |
| `switch.steam_engine_install_new_purchases` | Install new purchases at once |
| `binary_sensor.steam_engine_safe_to_shut_down` | PC is idle and nothing needs it (attributes `safe_since`, `off_at`: planned shutdown) |
| `binary_sensor.steam_engine_steam_busy` | Steam downloads or installs: a job of the integration runs (also between two of its games) or is being handed over, or the Steam client downloads by itself |
| `binary_sensor.steam_engine_busy_by_plugin` | A plugin keeps the PC busy (attribute `busy_by`) |
| `sensor.steam_engine_updates` | Number of pending updates without the muted games (attributes `games`: all of them, `check_running`, `auto_update`: games updated automatically; `muted`: games whose updates are not announced; `active_hours`: `{start, end}` as `HH:MM` while they are switched on, else null; `deferred`: app IDs of automatic work waiting for the active hours; `deferred_until`: when they start, local ISO time, null while nothing waits) |
| `sensor.steam_engine_not_installed` | Owned games that are not installed (attributes `games`, `owned`, `new`) |
| `sensor.steam_engine_download_progress` | Download progress in percent (attributes `job`, `queue`) |
| `sensor.steam_engine_current_game` | Name of the Steam game running on the PC, unknown when none runs (attributes `appid`, `started`; picture: the game's header image, see below) |
| `sensor.steam_engine_last_answer`, `sensor.steam_engine_query` | Details of the last update check |
| `select.steam_engine_game_to_install` | Pick an owned game that is not installed (needs the Steam Web API key and SteamID64) |
| `button.steam_engine_install_game` | Install the game picked in `select.steam_engine_game_to_install` |
| `select.steam_engine_installed_game` | Pick an installed game |
| `button.steam_engine_uninstall_game` | Uninstall the game picked in `select.steam_engine_installed_game` |
| `switch.steam_engine_auto_update_picked_game` | Automatic updates of the game picked in `select.steam_engine_installed_game` (off while none is picked) |
| `button.steam_engine_launch_game` | Start the game picked in `select.steam_engine_installed_game` on the PC (wakes it; not while a game runs) |

A pick lasts 10 minutes; switching auto update starts that time again. It also ends when its button
starts the job, when the game leaves the list, and when Home Assistant restarts or the integration
reloads (e.g. after changing its options), so a pick made and forgotten is not acted on later. The
pick in "Installed game" is the same for auto update and uninstall. The device page of "Steam Engine" (*Settings →
Devices & services → Devices → Steam Engine*) shows all of them, so you can install a game there
without any dashboard; its Press button does not ask before it uninstalls, the dashboard card and
the dashboard example below do.

Entity IDs can be renamed. A custom card or script that needs the current ones gets them from the
WebSocket command `steam_engine/entities`: `{"entities": {"<key>": "<entity ID>"}, "agent": {...}}`,
the keys being those of the unique IDs (`pc`, `updates`, `not_installed`, `download_progress`,
`current_game`, ...) and, for the agent, `library`, `install_status`, `history`, `agent_online`,
`steam_downloading`, `steam_running`, `game_running`, `last_input`, `network`, `shutdown_button`,
`library_free`, `restart_button`, `lock_button` and `close_games_button`.

Game images: Steam's old fixed image paths
(`https://cdn.cloudflare.steamstatic.com/steam/apps/<appid>/header.jpg`) give many newer games a
grey placeholder. The real URLs carry a hash that only the Steam store knows, so the integration
asks the store's public API (`store.steampowered.com/api/appdetails`, no key) for the games it knows: installed, with an update, owned and not installed, and the running one. It asks
one game every 2 seconds, starting a minute after Home Assistant started, a new game about half a
minute after it appeared, and waits (5 minutes, doubling up to 2 hours) when the store refuses. The
answers are kept in `.storage/steam_engine.images` and asked again after 30 days; a game the store
has no images for (removed, not sold in your region) is asked again after 7 days. The picture of
`sensor.steam_engine_current_game` uses them, and the old path while a game has no answer yet. A
custom card gets them from the WebSocket command `steam_engine/images`:
`{"images": {"<appid>": {"header": "<url>", "capsule": "<url>"}}}`, only for games with an answer.
The subscription `steam_engine/subscribe_images` sends the same map as an event message right after
its result and again whenever answers were added or changed (and after a reload of the integration);
like any subscription it ends with `unsubscribe_events` or when the connection closes, and while
the integration is not loaded it answers `not_loaded`. Unlike the event below it is open to users
who are not administrators. The event `steam_engine_images_updated` (no data) says that answers
were added or changed; Home Assistant lets only administrators subscribe to it, so it is meant for
automations.

Install size: the WebSocket command `steam_engine/game_size` (`{"appid": "<appid>"}`, plus `pc` as
for the other commands) answers `{"appid": "<appid>", "size_mb": <MB>, "download_mb": <MB>}` for
one game, each `null` when unknown. The size is what a first install without DLC writes to disk,
counted as the install job counts it for its space check: the public manifests of the game's
Windows depots from `api.steamcmd.net` (the source of the update check), leaving out DLC depots,
depots of other languages than English and low-violence depots. `download_mb` is the download of
the same depots, `null` when the API gives none or an implausible one (some old games). The
integration asks only when a card asks (no polling of all owned games), one game at a time with a
pause of a second between two requests, and waits at most 10 seconds; no answer (an error, a
timeout) is `null` at once and is not asked again for 10 minutes. Answers are kept in
`.storage/steam_engine.game_sizes` for 7 days (a day when the API knew no size), for all PCs
together, and two cards asking for the same game at once share one request.

From the agent on the PC (device "Steam PC"; these are the entity IDs of a new install, entities
that already exist keep theirs):

| Entity | Meaning |
|---|---|
| `sensor.steam_pc_install_status` | Steam job: state and progress of a job |
| `sensor.steam_pc_library` | Installed games (attribute `games`: each with its size, last played and drive, see [PROTOCOL.md](PROTOCOL.md)) |
| `sensor.steam_pc_history` | Update history |
| `sensor.steam_pc_library_free` | Most free space of all libraries (attribute `drives`: each drive with its free space, its Steam library or none, which one new games go to, how the disk is attached (`bus`, e.g. `usb`) and whether it is an SSD or an HDD (`media`), see [PROTOCOL.md](PROTOCOL.md)); keeps the last known value while the PC is off |
| `binary_sensor.steam_pc_steam_running` | Steam client running |
| `binary_sensor.steam_pc_steam_downloading` | Steam downloading (attribute `appids`; attribute `moving`: a move job runs) |
| `binary_sensor.steam_pc_game_running` | A game runs (attribute `appid`) |
| `sensor.steam_pc_last_input` | Last keyboard or mouse input at the PC |
| `sensor.steam_pc_network` | Network throughput |
| `binary_sensor.steam_pc_agent_online` | The agent is connected |
| `button.steam_pc_shutdown`, `button.steam_pc_restart`, `button.steam_pc_lock` | Shut down, restart or lock the PC |
| `button.steam_pc_close_games` | Close all running games (asks them first, stops them after 10 s) |

The MQTT details are in [PROTOCOL.md](PROTOCOL.md).

`sensor.steam_pc_network` and `sensor.steam_pc_last_input` change about every 20 seconds while the
PC is on. Home Assistant keeps their history for its usual 10 days (no long-term statistics). To
leave them out of the history altogether, add to `configuration.yaml` (the integration reads only
their current values, nothing stops working):

```yaml
recorder:
  exclude:
    entities:
      - sensor.steam_pc_network
      - sensor.steam_pc_last_input
```

## Dashboard card

The integration brings a card for the dashboard, **Steam Engine** (`custom:steam-engine-card`). It
comes in two styles (option `style`): **library**, the default, which puts the game artwork first
(see [Style library](#style-library)), and **tiles**, described first below. Both show the same parts
and do the same actions.

The tiles style (`style: tiles`) looks like Home Assistant's own tile cards: chips, coloured tags and pill buttons,
with colour only for state (green running or on, blue a job or something to do, amber attention, red
danger). From top to bottom:

- **Status** (always shown): the game running on the PC with its header image and since when (a
  compact tile instead when `show_images` is off), chips for PC on or off, a download running (with
  percent), the number of updates and the free space, and the Steam job as a tile with game,
  percent, progress bar, phase and the queue. *Sync now* is here while the *Updates* tab is
  not shown. While automatic work waits for the active hours (see [Settings](#settings)), a calm
  row says so and when it starts, e.g. "Waiting for active hours – starts at 08:00" (with
  "tomorrow" or the date when it is not today), and names the waiting games.
- **PC** (always shown): PC on or off and the agent online or offline, buttons to switch the PC on,
  shut it down, restart and lock it and to close the running games (see *PC buttons* below), the
  switch *Auto off when idle* (`switch.steam_engine_auto_off`) and, set apart at the bottom, *Shut
  down anyway*. While a Steam Engine job, a download of Steam or a plugin keeps the PC from shutting
  down, one short line under the PC state names it. *Details* opens a short status list as small
  tiles (Steam running, Steam busy, last input at the PC, network throughput, safe to shut down, the
  planned auto off time while one is set, the active hours while they are on, a plugin that keeps
  the PC busy; a line whose sensor has
  no value is left out, what keeps the PC on is marked); it is closed at first, and whether it is
  open is remembered in this browser.
- **Tabs** for the lists below, *Space* first so the card does not open on the long list of
  installed games; the tab you pick is remembered in this browser. The rows have no icon: their
  state shows as small coloured tags.
  - **Space** (`free_space`): free space and use of each drive with a Steam library on the PC, with a
    star after the drive new games go to (Steam's default library), then muted the drives a new
    library could go to, with the hint *no Steam library*. A USB drive has a *USB* badge, the icon
    shows an SSD (a chip) or an HDD (a hard disk, also when the kind is unknown). A drive with a
    library also shows how many games are on it; tap it to open the list of its games below it,
    each with its size, the largest first (tap again to close; a PC agent older than 0.9.0 does not
    send where its games are, then there is no list). While the PC is off, the last known state.
  - **Updates** (only while there are any, with their number): each game with its size and an
    *Update* button, a mark for games with automatic updates, *Install all* and *Sync now*.
  - **Installed**: all installed games sorted by name, with size and drive (*31 GB on F:*), last played, a switch for
    automatic updates, *Start* and *Uninstall*. Tags show the state: running, downloading in the
    job, being moved, waiting for the active hours, queued, update available. The running game has no *Start* and *Uninstall*; a game on
    a drive that is not there is greyed out without buttons. *Move* is in the library style's sheet.
  - **History** (`history`): the last 10 installs, updates and uninstalls the PC recorded, newest
    first, each with what happened (a coloured tag), date and time, the build change of an update and
    whether Steam Engine or the Steam client did it.
  - **Owned** (`not_installed`): each game with *Install*, new purchases marked, and the switch
    *Install new purchases automatically* at the top. Without a Steam Web API key and Steam ID it
    shows a hint to the settings instead.

Texts follow the language of your Home Assistant user (English and German), numbers and dates its
locale. Colours come from the theme, so light and dark themes both work.

### Style library

With `style: library` (the default) the card looks like a game library:

- **Hero**: the running game as a large header image with its name and since when. While no game
  runs but a job downloads, the hero shows that game with the percent and a progress bar along its
  bottom edge; otherwise the job is a row with the game's small image, percent, progress bar, phase
  and the queue. Chips below show the updates and the free space, and, while automatic work waits for
  the active hours, the same row as in the tiles style.
- **PC**: a slim bar with PC on or off and the agent, the line about what keeps the PC awake and
  *Details* (the status values as small chips, closed at first and remembered in this browser), the
  PC buttons (switch on, shut down, restart, lock, close games), *Auto off when idle* and, set apart,
  *Shut down anyway*.
- **Segments** for the lists, in the same order and with the same remembered choice as the tabs
  (*Space* first). *Installed*, *Updates* and *Owned* are a grid of the games' Steam capsule images,
  two columns on a phone and more on a wide card, each with its name, size and last played, small
  badges for the state (running, downloading, waiting for the active hours, queued, update, new
  purchase, drive missing) and a mark
  for automatic updates; games with an update have a coloured frame. *Updates* keeps *Install all*
  and *Sync now* above the grid, *Owned* the switch for new purchases. *History* and
  *Space* are simple lists as in the tiles style.
- A tap on a game opens its **sheet** from the bottom (a centred dialog on a wide screen): the
  header image, the state, size and drive (*31 GB on F:*), last played and the size of a pending update, and every action
  the game has right now: *Start*, *Update*, the switch for automatic updates, *Move* and
  *Uninstall*, or *Install* for an owned game. *Move* shows where *Uninstall* does, when the PC
  reports another drive and no Steam job or move runs; while a game is being moved no game is
  started, and the game being moved has only its switch. For an owned game that is not installed the install size (what a
  first install without DLC writes to disk, see [Entities](#entities), `steam_engine/game_size`)
  stands right of its name: asked once when the sheet opens, `…` while it comes, nothing when it is
  unknown (asked again when the game is opened after 10 minutes). The sheet keeps the game that was tapped, checks again against the
  current state when you tap an action, and closes with *Close*, Escape, a tap beside it or when you
  leave the page. *Uninstall*, *Install* and *Move* ask first, as in the tiles style; *Start* and *Update*
  close the sheet so the card's message about the action is visible.

The images come straight from Steam's CDN
(`https://cdn.cloudflare.steamstatic.com/steam/apps/<appid>/capsule_231x87.jpg`, about 10 KB each,
and `header.jpg` for the hero and the sheet); they load only when they scroll into view, and the
browser caches them. Your browser fetches them, Home Assistant does not. With `show_images: false`
no image is loaded: the grid then shows a tinted tile with the game's name instead, as it does for a
game Steam has no image for.

Newer games (for example Battlefield 6) have no picture at these old paths: Steam answers them with
a small grey placeholder. For those the card uses the image URLs the integration looked up in the
Steam store: it subscribes to them (WebSocket subscription `steam_engine/subscribe_images`, open to
all users), so a newly looked-up image shows without reloading the page and the card asks nothing
on a timer. A game the integration has not looked up yet keeps the old path until then. While Home
Assistant starts and the integration is not loaded yet, the card tries again after 5, 15 and 60
seconds, and at once when the integration's entities appear (it was loaded) or the connection is
back after a break; until then it keeps the old paths.

### Adding the card

Nothing to install: the integration serves the card itself and loads it into the frontend
(`/steam_engine/steam-engine-card.js`), so no entry under *Settings → Dashboards → Resources* and
no HACS frontend repository are needed, in storage and YAML dashboards alike. After installing or
updating the integration, restart Home Assistant and reload the browser page (close and open the
Companion App, or reset its frontend cache in the app settings).

In a dashboard: *Edit dashboard → Add card*, search for **Steam Engine**. Or add it in YAML:

```yaml
type: custom:steam-engine-card
```

Without options it shows all sections. The card also works inside a pop-up (e.g. a Bubble Card
pop-up): add it to the pop-up's cards like any other card.

### Options

| Option | Default | Meaning |
|---|---|---|
| `title` | none | Title above the card |
| `style` | `library` | `library` (game artwork, see [Style library](#style-library)) or `tiles` (rows and tiles). Another value shows the library style |
| `sections` | all, in this order: `status`, `pc`, `free_space`, `updates`, `installed`, `history`, `not_installed` | Which parts the card shows. `status` and `pc` are the top area (in their order here; left out, they are hidden), the others are the tabs, in this order |
| `show_images` | `true` | Images of the games from the Steam CDN: in the tiles style the header image of the running game and in the confirmations (the lists have no images); in the library style also the hero, the capsules of the grid and the sheet. Off, the library style shows tinted tiles with the names |
| `actions_when_pc_off` | `true` | Show *Start*, *Uninstall*, *Install*, *Update* and *Install all* while the PC is off (see below); the PC area's *Switch the PC on* always shows while the PC is off |
| `pc` | the only PC, else the PC without a name | With [more PCs](#more-pcs): the name of the PC this card shows. One card per PC |

The card editor sets `title`, `style`, `show_images`, `actions_when_pc_off` and, with more than one PC, `pc`; `sections` is set in YAML
(*Show code editor*). An example:

```yaml
type: custom:steam-engine-card
title: Steam
sections:
  - status
  - installed
  - updates
  - not_installed
show_images: true
actions_when_pc_off: false
```

### Actions

Every game button carries the app ID of the game in its row and calls the integration's action
(`steam_engine.install`, `uninstall`, `move`, `launch`, `set_auto_update`, `set_muted`, `refresh`) for exactly that
game, so a list that changes in between (e.g. right after another uninstall) cannot hit the game
next to it. A button stays locked until the action answers. In the library style the buttons are in
the game's sheet, which keeps the app ID of the game tapped in the same way.

- **Uninstall** always asks first, in a dialog with the game's name, its size and picture. The
  dialog keeps the game that was tapped, whatever the list does meanwhile. **Install** asks too,
  with the install size when it is known ("About 2.7 GB will be installed on the PC.", asked from
  the integration when the dialog or the game's sheet opens), otherwise with the hint that a first
  install can be many GB. With two or more drives to pick from the install dialog
  offers the drive, each with its kind (*SSD*/*HDD*), its free space and a star after Steam's
  default: the drives with a Steam library, then the drives without one with the hint *new Steam
  library* (the install creates it there, see above), a USB drive with a *USB* badge. With a known
  size each drive also says whether the game fits ("fits", "does not fit (31 GB missing)", by the
  rule of the PC's space check: size plus 10 %, at least 5 GB). Steam's default is picked at first
  if the game fits there, otherwise the first drive it fits on (libraries first, an SSD before an
  HDD) with the hint "F: is too full, E: preselected."; if it fits nowhere the default stays. A
  warning shows when the game probably does not fit on the drive picked, also with a single drive;
  installing stays possible, the PC makes the final check. The drive picked is always sent with
  the install, also the default. *Update*, *Install all*, *Start* and the auto-update switch do
  not ask.
- **Move** (library style, in the sheet of an installed game) asks in a dialog like the install
  dialog: the game's size with its workshop content, then the other drives of the PC (not the one
  it is on), each with its kind, free space, a *USB* badge, *new Steam library* for a drive without
  one, and whether the game fits (size plus workshop content plus the reserve of an install, as the
  PC counts it). The first drive it fits on is picked (libraries first, an SSD before an HDD),
  otherwise the first one, with a warning when it probably does not fit. While the move runs the
  card shows it like a job (*Moving Cyberpunk 2077*, the drives, the step and the percent of the
  copy, a chip *Moving 42 %*), and its end as a message ("moved to E:", or why not).
- The card says what happened, with the game's name: started or queued, rejected with the reason
  (e.g. a game is running, the agent cannot be reached, already in the job or the queue), and done
  (uninstalled once the game has left the library; installed or updated once the job has ended for
  that game; moved, or why not, once the move has ended). These messages last only while the page stays open; failures the PC reports later
  still come as a push.
- **Active hours** hold back only what Steam Engine starts by itself (automatic updates, new
  purchases, the retry). A game that waits for them is marked *waiting* and leaves the queue line;
  its *Update* or *Install* button (and *Install all*) stays and starts it at once, also outside the
  active hours. The card takes the window and the waiting games from the attributes `active_hours`,
  `deferred_until` and `deferred` of `sensor.steam_engine_updates`. The window and its next start
  are both shown in the time zone of Home Assistant (the one the active hours are set in), also when
  the browser is in another zone.
- **PC off**: with `actions_when_pc_off: true` (the default) the buttons stay, and a tap switches
  the PC on and then runs the action, as the actions do anyway. The card shows "Switching the PC on
  …" meanwhile, and the button stays locked until the action answers, which can take a few minutes
  while the PC starts. With `false` these buttons are hidden while the PC is off; the lists, the auto-update
  switches, *Install new purchases automatically* and *Sync now* stay.

### PC buttons

The PC area shows only the buttons that fit the PC's state:

| Button | Shown | Does |
|---|---|---|
| *Switch the PC on* | PC off | `switch.turn_on` on the integration's PC switch (wakes the PC with the wake entity of the settings). No question. |
| *Shut down the PC* | agent online | `switch.turn_off` on the PC switch, after a confirmation. While Steam downloads or runs a job, or a plugin keeps the PC busy, the PC stays on: the card shows the reason and the integration sends a push. |
| *Restart the PC* | agent online | Presses the agent's restart button, after a confirmation. |
| *Lock the PC* | agent online | Presses the agent's lock button. No question. |
| *Close the running games* | agent online and a game runs | Presses the agent's close games button, after a confirmation that names the running game (or says "all running games" when the integration does not know its name). The agent closes every game that runs. |
| *Shut down anyway* | agent online | Presses the agent's shut down button, after a confirmation. Set apart below the other buttons: it does not go through the PC switch, so the PC shuts down at once even while Steam downloads, a job runs, a plugin keeps the PC busy or a game runs. |

The confirmations warn when a game is running (it is closed), and for a restart also when Steam is
downloading or running a job (the restart interrupts it; Steam resumes the download later). The
confirmation of *Shut down anyway* lists what applies right now: a running game, a job of Steam
Engine, a download of Steam and a busy plugin (by name). The card says whether the PC took the
command. The restart, lock and close games buttons come from `steam_engine/entities`; a button that
is missing or unavailable is not shown.

### Renamed entities

The card has no entity IDs of its own: it asks the integration for the current ones with the
WebSocket command `steam_engine/entities` (see [Entities](#entities)), when it starts and again
when one of them is missing. So it keeps working after you renamed entities of Steam Engine or of
the agent, without any change to the card.

## Dashboard example

Instead of the card you can build your own cards from the entities. This view uses built-in cards
only (no HACS cards): the PC, the running game, updates, and installing or uninstalling a game
through the pick entities. Create a new dashboard (*Settings → Dashboards → Add dashboard → New
dashboard from scratch*), open it, choose *Edit dashboard → ⋮ → Raw configuration editor* and paste
it. It uses the entity IDs of a new install; if yours differ, change them.

```yaml
views:
  - title: Steam
    type: sections
    max_columns: 3
    sections:
      - type: grid
        cards:
          - type: heading
            heading: PC
          - type: tile
            entity: switch.steam_engine_pc
            icon_tap_action:
              action: more-info
          - type: tile
            entity: switch.steam_engine_auto_off
          - type: tile
            entity: sensor.steam_engine_current_game
            grid_options:
              columns: full
          - type: tile
            entity: sensor.steam_engine_download_progress
            grid_options:
              columns: full
      - type: grid
        cards:
          - type: heading
            heading: Updates
          - type: tile
            entity: sensor.steam_engine_updates
            name: Updates (tap to check now)
            tap_action:
              action: perform-action
              perform_action: steam_engine.check_updates
            grid_options:
              columns: full
          - type: markdown
            content: |
              {% for g in state_attr('sensor.steam_engine_updates', 'games') or [] -%}
              - {{ g.name }} ({{ g.size_mb }} MB installed)
              {% else -%}
              No updates.
              {%- endfor %}
          - type: tile
            entity: switch.steam_engine_install_new_purchases
            grid_options:
              columns: full
      - type: grid
        cards:
          - type: heading
            heading: Library
          - type: tile
            entity: select.steam_engine_game_to_install
            features:
              - type: select-options
            grid_options:
              columns: full
          - type: tile
            entity: button.steam_engine_install_game
            tap_action:
              action: perform-action
              perform_action: button.press
              target:
                entity_id: button.steam_engine_install_game
            icon_tap_action:
              action: perform-action
              perform_action: button.press
              target:
                entity_id: button.steam_engine_install_game
            grid_options:
              columns: full
          - type: tile
            entity: select.steam_engine_installed_game
            features:
              - type: select-options
            grid_options:
              columns: full
          - type: tile
            entity: switch.steam_engine_auto_update_picked_game
            grid_options:
              columns: full
          - type: tile
            entity: button.steam_engine_launch_game
            tap_action:
              action: perform-action
              perform_action: button.press
              target:
                entity_id: button.steam_engine_launch_game
            icon_tap_action:
              action: perform-action
              perform_action: button.press
              target:
                entity_id: button.steam_engine_launch_game
            grid_options:
              columns: full
          - type: markdown
            content: |
              {% set auto = state_attr('sensor.steam_engine_updates', 'auto_update') or [] -%}
              {% set names = (state_attr('sensor.steam_pc_library', 'games') or [])
                 | selectattr('appid', 'in', auto) | map(attribute='name') | sort | list -%}
              Auto-update: {{ names | join(', ') if names else 'none' }}
          - type: tile
            entity: button.steam_engine_uninstall_game
            tap_action:
              action: perform-action
              perform_action: button.press
              target:
                entity_id: button.steam_engine_uninstall_game
              confirmation:
                text: Uninstall the picked game?
            # without this, a tap on the icon would press the button without asking
            icon_tap_action:
              action: perform-action
              perform_action: button.press
              target:
                entity_id: button.steam_engine_uninstall_game
              confirmation:
                text: Uninstall the picked game?
            grid_options:
              columns: full
```

## Services

| Service | Purpose |
|---|---|
| `steam_engine.install` | Install or update games (`appid`, several allowed); queued if a job runs, a game the job or the queue has already is not added again. Optional `drive`: the letter of the drive for a first install, with a Steam library or without (the install creates one there); without or empty a drive stored for the game before stays (a queued game), otherwise Steam's default library: to go back to the default library, give its letter; a queued game takes a new drive until the PC has started its job, an installed game stays where it is |
| `steam_engine.uninstall` | Uninstall a game (`appid`, one) |
| `steam_engine.move` | Move an installed game to another drive (`appid`, one; `drive`, a letter like `E`): to its Steam library, or a new one on a drive without one. Not while a game runs or another Steam job or move runs (the answer says why); see *Moving a game to another drive* under [Settings](#settings) |
| `steam_engine.check_updates` | Check for updates now |
| `steam_engine.check_owned` | Check for new purchases now |
| `steam_engine.refresh` | Sync now: ask the PC for its game library (when the agent is online, up to 1 min), then check for updates and new purchases (the card's *Sync now*) |
| `steam_engine.set_auto_update` | Switch automatic updates for one game on or off (`appid`, `enabled`; without `enabled` it toggles) |
| `steam_engine.set_muted` | Mute the updates of one game or unmute them (`appid`, `enabled`; without `enabled` it toggles): no update push, not counted, its automatic updates quiet |
| `steam_engine.launch` | Start an installed game on the PC (`appid`), waking it first; Steam starts first if it does not run. Not while a game runs. A locked screen stays locked, the game runs behind it (e.g. for Steam Remote Play); with *Start games without the lock* a PC woken for the start is not locked (see [Settings](#settings)) |
| `steam_engine.pc_wake` | Wake the PC and wait until it is ready (for your own automations) |
| `steam_engine.pc_shutdown_when_idle` | Shut down a woken PC once nothing needs it any more |

Every action has an optional field `pc` for several PCs on one Home Assistant: the
name of the PC as set on the PC. Without it the action goes to the only PC, else to the PC without a
name; with only one PC you never need it.

## Events

For automations of your own (trigger *Event*, or *Manual event* in the editor), fired whether
pushes are set up or not. Every game is `{appid, name}`, and every event has `pc`: the PC's name
(empty for the PC without a name).

| Event | Data |
|---|---|
| `steam_engine_job_done` | `result` (`done`, `done_with_errors`, `aborted`), `installed`, `updated`, `failed` (each with `reason`), `no_space` (each with `needed_gb`, `free_gb`, `drive`), `in_client` (left to the Steam client), `game_running` (skipped, the game ran), `open` (aborted: the games still to do; the job goes on by itself) |
| `steam_engine_uninstalled` | `appid`, `name`, `success`, `reason` (on failure), `free_gb` |
| `steam_engine_moved` | `appid`, `name`, `success`, `reason` (on failure, the `reason_code` of [PROTOCOL.md](PROTOCOL.md)), `warning` (`source_not_deleted`: the old folder is partly left, or with `reason` `not_installed` stays after a move cut off whose game was uninstalled meanwhile), `from`, `drive` (like `F:`), `new_library`, `source_free_gb`, `target_free_gb` |
| `steam_engine_updates_found` | `games` (updates new since the last check), `total` (all open updates) |
| `steam_engine_purchased` | `games` (new purchases, not installed), `auto_install` |
| `steam_engine_game_started` | `appid`, `name` |
| `steam_engine_game_ended` | `appid`, `name`, `minutes` (playing time) |
| `steam_engine_check_done` | none (an update check ended) |
| `steam_engine_images_updated` | none (new game images) |

A job end that Home Assistant reads again after a restart or an MQTT reconnect is not fired again,
nor is a restart of Home Assistant a game end. Example:

```yaml
triggers:
  - trigger: event
    event_type: steam_engine_job_done
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.failed | count > 0 }}"
actions:
  - action: persistent_notification.create
    data:
      message: "Failed: {{ trigger.event.data.failed | map(attribute='name') | join(', ') }}"
```

## Plugins

Your own automations can keep the PC on: give any entity the label **PC busy** (label ID `pc_busy`).
While it is on (automations and scripts: while they run), the PC is not shut down. Use
`steam_engine.pc_wake` before and `steam_engine.pc_shutdown_when_idle` after the work.

Scripts on the PC can be started from Home Assistant too. List them in
`%LOCALAPPDATA%\SteamEngine\agent\plugins.psd1` and restart the agent (it reads the file only at its
start): double-click `Install.cmd` again or run its `-Update` command (see *Updating*; it also shows
the agent's warnings about `plugins.psd1`), or sign out of Windows and in again. *Run* on the task
"Steam Engine Agent" in the Task Scheduler does nothing while the agent runs.

```powershell
@{
    Commands = @{ my_backup = 'C:\Scripts\backup.ps1' }
    Busy     = @{ my_backup = @{ Processes = @('robocopy'); AfterSec = 60 } }
}
```

The agent then offers a button "Run my_backup" (`button.steam_pc_run_my_backup`, MQTT command
`run:my_backup`, an optional argument goes to the script as `-Job`; command and argument are
lower-cased, and the argument may only contain a-z, 0-9 and `_ . , = ; -`, otherwise the command is
ignored) and a sensor "Busy my_backup" (`binary_sensor.steam_pc_busy_my_backup`) that is on while one
of the processes runs (and `AfterSec` seconds longer). Give that sensor the label **PC busy**.

Names may only contain letters, digits and `_`. A busy entry needs `Processes` (process names as in
the Task Manager's details, `.exe` may be left out). The agent leaves out an entry that cannot work
and says why in `agent\steam-agent.log` (the setup shows it at its end). When you remove a name from
`plugins.psd1`, the agent removes its entity in Home Assistant at its next start (restart it as
above).

A plugin script may dot-source `%LOCALAPPDATA%\SteamEngine\mqtt_publish.ps1` to use the settings
(`Get-SteamEngineConfig`), the MQTT login (`Get-MqttCredentials`), `Publish-MqttRetained`, a log that
never throws (`Write-SteamEngineLog <file> <text>`) and the other helpers there. That file defines
only names with `Steam` or `Mqtt` in them (plus `Get-AcfValue`, `Read-FileTail`,
`Read-SharedTextFile`), so a plugin should not use such names for its own functions.

## Files on the PC

Everything lives in `%LOCALAPPDATA%\SteamEngine`:

| Path | Content |
|---|---|
| `settings.psd1` | MQTT address and port, the PC's name (all keys: `pc/setup/settings.example.psd1`) |
| `mqtt_credentials.txt` | MQTT user and password, the password encrypted for your Windows user (DPAPI); use an MQTT user just for this |
| `agent\` | `steam-agent.ps1`, `plugins.psd1`, `announced_plugins.txt` (plugin entities announced to Home Assistant), `steam-agent.log`, `lock-at-logon.ps1` and `lock-at-logon.log` (task "Steam Engine Lock", only with `-LockAtLogon on`), `task_backup\` (definitions of replaced or removed scheduled tasks, the newest three per task) |
| `jobs\` | `install.ps1`, `uninstall.ps1`, `move.ps1`, `install.log`, `uninstall.log`, `move.log` and `move_robocopy.log`, `move.lock` (names the running move), `move_pending_<appid>_<time>.json` (one per move between its switch and its end, or with its old folder not fully deleted yet; finished or taken back by the next start), `install.lock` (names the running install job; left behind by an interrupted one), `install_last.txt` (the games of the last job, so a command sent again does not run them twice), `install_final.json` (an end state of a job that could not be sent yet), `move_final.json` (the same for a move), `pending_manifest_cleanup.txt` (manifests of failed first installs, removed by the next job) |
| `library\` | `report.ps1`, `history.jsonl` (update history), `history_state.json`, `library_cache.json` (last known data of games on a drive that is not there), `report.log` |

The logs are the first place to look when something does not work. They are UTF-8 and move to
`<name>.log.old` at 1 MB.

## Development

Every push runs the workflow *Validate*: hassfest, the HACS validation (the checks of the Marketplace
catalog; once the repository is public), the tests in `tests/`, the tests of the dashboard card (`tests/js`, Node without npm
packages: `node --test tests/js/*.test.mjs`), and checks of the PC side (every `.ps1`/`.psd1`
parses, `pc/` is ASCII only, the `.cmd` files have CRLF line endings). To run the Python tests
yourself (Python 3.14):

```
pip install -r requirements_test.txt
pytest
```

`requirements_test.txt` pins the Home Assistant version the tests run against; CI also runs them
against the oldest supported one (`requirements_test_min.txt`, the version in `hacs.json`).

## License

[PolyForm Noncommercial License 1.0.0](LICENSE): free for personal and other non-commercial use;
any commercial use is not allowed. Later versions may be released under different terms.
