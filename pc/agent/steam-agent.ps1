# Steam agent: reports the state of the Steam client and the PC to Home Assistant over MQTT and takes
# commands from it (see PROTOCOL.md): install:[<job>:]<appids>, uninstall:<appid>, move:<appid>@<letter>,
# launch:<appid>, library, shutdown, restart, lock, close_games, ping and run:<plugin>[:<arg>].
# Runs in the background, started by the scheduled task "Steam Engine Agent" at logon. Also starts the
# library report (library\report.ps1) regularly.
# Lives in %LOCALAPPDATA%\SteamEngine\agent (see pc/setup/Install-SteamEngine.ps1).
# Only a deliberate exit ends it: errors while collecting data are logged, errors of the MQTT
# connection lead to a new connection.
# ASCII only in this file (Windows PowerShell 5.1 reads .ps1 without BOM as ANSI).
$ErrorActionPreference = 'Stop'

$seRoot          = Join-Path $env:LOCALAPPDATA 'SteamEngine'
$credFile        = Join-Path $seRoot 'mqtt_credentials.txt'
$logFile         = Join-Path $PSScriptRoot 'steam-agent.log'
$reportScript    = Join-Path $seRoot 'library\report.ps1'
$installScript   = Join-Path $seRoot 'jobs\install.ps1'
$uninstallScript = Join-Path $seRoot 'jobs\uninstall.ps1'
$moveScript      = Join-Path $seRoot 'jobs\move.ps1'
# move.ps1 names itself here while it runs (steam_moving), and leaves a journal move_pending_*.json in
# the same folder for each move cut off after its switch or with a rest of its old folder (finished or
# taken back by "move.ps1 -Recover" at the agent's start)
$moveLockFile    = Join-Path $seRoot 'jobs\move.lock'
$moveJournalDir  = Join-Path $seRoot 'jobs'
$intervalSec     = 20
# Library report: a minute after the agent's start, then every 30 minutes; one still running after
# 5 minutes hangs and is stopped (checked every round)
$reportFirstSec  = 60
$reportEverySec  = 1800
$reportMaxSec    = 300
# Plugins: a local file next to the agent, not in the repository, e.g.
# @{ Commands = @{ my_backup = 'C:\Scripts\backup.ps1' }
#    Busy     = @{ my_backup = @{ Processes = @('robocopy'); AfterSec = 60 } } }
# Commands: a button in HA sends 'run:<name>' and starts the script. Busy: a sensor per entry, on while
# one of the processes runs (plus AfterSec); give it the label "PC busy" in HA. Names: A-Z, a-z, 0-9, _.
$pluginsFile = Join-Path $PSScriptRoot 'plugins.psd1'
# The discovery topics of plugin entities announced so far, to clear those of removed plugins
$announcedFile = Join-Path $PSScriptRoot 'announced_plugins.txt'
$keepAliveSec = 60     # the broker disconnects after 1.5 x this much silence; the agent pings every round
$refreshSec   = 300    # send the state regularly even without a change (HA: unavailable after 900 s)
# Waiting before the next connect: a refused login is tried less and less often, up to this
$loginRetryMaxSec = 300
# A game counts as downloading while its manifest shows that Steam works on it right now, or while
# Steam wrote to downloading\<appid> within this time. For a game whose update is only paused or
# queued (StateFlags), a longer time: the check and commit after a download write little there.
$downloadActiveSec = 180
$queuedActiveSec   = 900
# Drives without a library (for a new library there) are looked for at most this often
$drivesEverySec    = 300

# Until the shared library is loaded: a plain line that never throws (replaced below)
function Write-Log([string]$message) {
    try { [System.IO.File]::AppendAllText($logFile, ([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture) + '  ' + $message + "`r`n")) } catch { }
}

# One instance per user
$mutex = New-Object System.Threading.Mutex($false, 'Local\SteamAgentHA')
$owned = $false
try {
    $owned = $mutex.WaitOne(0)
} catch {
    # Abandoned: the last agent was stopped without releasing it; it is ours now
    $e = $_.Exception
    $owned = ($e -is [System.Threading.AbandonedMutexException] -or $e.InnerException -is [System.Threading.AbandonedMutexException])
    if (-not $owned) { Write-Log "Stopped: single-instance check failed: $($e.Message)"; exit 1 }
}
if (-not $owned) { exit 0 }

Write-Log "Start (PID $PID)."
try {
    . (Join-Path $seRoot 'mqtt_publish.ps1')
    if ($SteamEngineNamesProblem) { throw $SteamEngineNamesProblem }
    $seCfg = Get-SteamEngineConfig
    # The paths of processes and folders for close_games (mqtt_publish.ps1)
    Initialize-SteamProcessType
    # Milliseconds since the last keyboard or mouse input in this session
    if (-not ('SteamAgentInput' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SteamAgentInput {
    [StructLayout(LayoutKind.Sequential)]
    struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    public static uint IdleMilliseconds() {
        LASTINPUTINFO l = new LASTINPUTINFO();
        l.cbSize = (uint)Marshal.SizeOf(l);
        if (!GetLastInputInfo(ref l)) { return 0; }
        return unchecked((uint)Environment.TickCount - l.dwTime);
    }
}
'@
    }
} catch {
    Write-Log "Stopped: $($_.Exception.Message)"
    exit 1
}

# From here on with rotation (mqtt_publish.ps1); never throws
function Write-Log([string]$message) { Write-SteamEngineLog $logFile $message }

$mqttClientId   = ($env:COMPUTERNAME.ToLowerInvariant() + '-steam-agent')
$stateTopic     = $SteamEngineTopic.AgentState
$statusTopic    = $SteamEngineTopic.AgentStatus
$commandTopic   = $SteamEngineTopic.AgentCommand
$telemetryTopic = $SteamEngineTopic.AgentTelemetry
$mqttHost       = $seCfg.MqttHost
$mqttPort       = $seCfg.MqttPort

# A problem that comes back every round is logged once, and again only when its text changes. An
# empty text ends it ($fixedText is logged then, if given).
$script:problems = @{}
function Write-LogProblem([string]$key, [string]$message, [string]$fixedText = '') {
    $last = $script:problems[$key]
    if ($message) {
        if ($message -ne $last) {
            $script:problems[$key] = $message
            Write-Log "Warning: $message"
        }
    } elseif ($last) {
        $script:problems.Remove($key)
        if ($fixedText) { Write-Log $fixedText }
    }
}

# --- Plugins ---------------------------------------------------------------------------------------

# Commands: name -> script path; Busy: name -> @{ Processes; AfterSec }. Entries that cannot work are
# left out with one warning each. Readable: the file was read (or is not there); only then entities
# of removed plugins are cleared. Declared: the discovery topics of all names in the file, also of
# entries left out (a typo must not delete an entity together with its label "PC busy").
function Get-Plugins {
    $result = @{ Commands = @{}; Busy = @{}; Readable = $true; Declared = @() }
    if (-not (Test-Path -LiteralPath $pluginsFile)) { return $result }
    try {
        $p = Import-PowerShellDataFile -LiteralPath $pluginsFile
    } catch {
        Write-Log "Warning: plugin file not readable, no plugins: $($_.Exception.Message)"
        $result.Readable = $false
        return $result
    }
    foreach ($section in @('Commands', 'Busy')) {
        $entries = $p[$section]
        if ($null -eq $entries) { continue }
        if ($entries -isnot [System.Collections.IDictionary]) {
            Write-Log "Warning: plugins.psd1: $section is not a table @{ ... }, left out."
            continue
        }
        $kind = if ($section -eq 'Commands') { 'command' } else { 'busy' }
        foreach ($key in @($entries.Keys)) {
            $name = "$key"
            $result.Declared += Get-SteamEnginePluginTopic $kind $name
            # The name goes into topics, entity IDs and templates: no '-', ' ' or other characters
            if ($name -cnotmatch '^[A-Za-z0-9_]+$') {
                Write-Log "Warning: plugins.psd1: $section name '$name' may only contain A-Z, a-z, 0-9 and _, left out."
                continue
            }
            $value = $entries[$key]
            if ($kind -eq 'command') {
                if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value)) {
                    Write-Log "Warning: plugins.psd1: command '$name' needs the path of a script, left out."
                    continue
                }
                if (-not (Test-Path -LiteralPath $value)) { Write-Log "Warning: plugins.psd1: the script of command '$name' does not exist (yet): $value" }
                $result.Commands[$name] = $value
                continue
            }
            if ($value -isnot [System.Collections.IDictionary]) {
                Write-Log "Warning: plugins.psd1: busy entry '$name' is not a table @{ Processes = @(...) }, left out."
                continue
            }
            # Process names as Get-Process takes them: without '.exe'
            $procs = @(@($value['Processes']) | Where-Object { $_ -is [string] -and $_.Trim() } | ForEach-Object { $_.Trim() -replace '\.exe$', '' })
            if ($procs.Count -eq 0) {
                Write-Log "Warning: plugins.psd1: busy entry '$name' has no Processes, left out."
                continue
            }
            $after = 0
            if ($null -ne $value['AfterSec'] -and -not [int]::TryParse("$($value['AfterSec'])", [ref]$after)) {
                Write-Log "Warning: plugins.psd1: AfterSec of busy entry '$name' is no number, 0 used."
                $after = 0
            }
            $result.Busy[$name] = @{ Processes = $procs; AfterSec = [math]::Max(0, $after) }
        }
    }
    return $result
}

$script:busySeen = @{}

function Get-Busy {
    # Per Busy entry ON while one of the processes runs, and AfterSec longer
    $r = [ordered]@{}
    $now = [datetime]::UtcNow
    foreach ($name in ($script:plugins.Busy.Keys | Sort-Object)) {
        $cfg = $script:plugins.Busy[$name]
        if (Get-Process -Name $cfg.Processes -ErrorAction SilentlyContinue) { $script:busySeen[$name] = $now }
        $on = $script:busySeen.ContainsKey($name) -and ($now - $script:busySeen[$name]).TotalSeconds -lt [math]::Max($cfg.AfterSec, 1)
        $r[$name.ToLowerInvariant()] = if ($on) { 'ON' } else { 'OFF' }
    }
    return $r
}

# --- Telemetry: last input and network rate ---------------------------------------------------------

$script:netLast = $null
$script:netRoute = $null

function Get-Telemetry {
    $idleMs = [SteamAgentInput]::IdleMilliseconds()
    $lastInput = [datetime]::UtcNow.AddMilliseconds(-[double]$idleMs)
    # Smooth out rounding noise: less than 3 s apart = the same input, otherwise a new state in HA
    if ($script:lastInput -and [math]::Abs(($lastInput - $script:lastInput).TotalSeconds) -lt 3) { $lastInput = $script:lastInput }
    elseif ($script:lastInput -and $lastInput -gt $script:lastInput -and (Test-SteamEngineOwnCallInput $lastInput)) {
        # Steam's window after a job's own steam:// call or client start, not a person (mqtt_publish.ps1):
        # keep reporting the input before it. A later real input is newer and is reported again.
        if (-not $script:ownCallInput -or [math]::Abs(($lastInput - $script:ownCallInput).TotalSeconds) -ge 3) {
            Write-Log ("Input at {0} came with an own Steam call, not reported." -f (Format-SteamEngineUtc $lastInput))
        }
        $script:ownCallInput = $lastInput
        $lastInput = $script:lastInput
    }
    $script:lastInput = $lastInput
    # Rate at the adapter with the default route (Wi-Fi or Ethernet, whatever the PC uses). The route
    # is looked up again every 5 minutes and after an error (two CIM queries cost time every round).
    $rate = $null
    try {
        $nowUtc = [datetime]::UtcNow
        if (-not $script:netRoute -or $nowUtc -ge $script:netRoute.Until) {
            $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop | Sort-Object { $_.RouteMetric + $_.InterfaceMetric } | Select-Object -First 1
            $script:netRoute = @{ Alias = $route.InterfaceAlias; Until = $nowUtc.AddMinutes(5) }
        }
        $st = Get-NetAdapterStatistics -Name $script:netRoute.Alias -ErrorAction Stop
        if (-not $st) { throw "no statistics for the adapter '$($script:netRoute.Alias)'" }
        $bytes = [int64]$st.ReceivedBytes + [int64]$st.SentBytes
        if ($script:netLast -and $script:netLast.Name -eq $st.Name) {
            $dt = ($nowUtc - $script:netLast.Time).TotalSeconds
            if ($dt -gt 0.5) { $rate = [math]::Max(0, [math]::Round(($bytes - $script:netLast.Bytes) / $dt)) }
        }
        $script:netLast = @{ Bytes = $bytes; Time = $nowUtc; Name = $st.Name }
        Write-LogProblem 'network' '' 'Network rate measured again.'
    } catch {
        $script:netRoute = $null
        $script:netLast = $null
        Write-LogProblem 'network' "network rate not measured: $($_.Exception.Message)"
    }
    return [ordered]@{
        last_input      = Format-SteamEngineUtc $lastInput
        network_bytes_s = $rate
        network_adapter = if ($script:netLast) { $script:netLast.Name } else { $null }
    }
}

# --- Steam state ---------------------------------------------------------------------------------

# Manifest data per file, read again only when the file changed (Steam rewrites a manifest on every
# change of its state)
$script:manifestCache = @{}
# Drives without a library (Get-SteamCandidateDrives) and when they were listed (UTC)
$script:candidateDrives = @()
$script:candidateDrivesUtc = $null
# Bus and media per drive letter (Get-SteamDriveHardware) and the letters it was read for
$script:driveHardware = @{}
$script:driveHardwareLetters = $null

function Get-ManifestInfo($file) {
    $stamp = '{0}/{1}' -f $file.LastWriteTimeUtc.Ticks, $file.Length
    $hit = $script:manifestCache[$file.FullName]
    if ($hit -and $hit.Stamp -eq $stamp) { return $hit }
    # Shared read: Steam must be able to write the manifest meanwhile
    $text = Read-SharedTextFile $file.FullName
    if (-not $text) { return $null }
    $flagsText = Get-AcfValue -Key 'StateFlags' -Text $text
    $appId = Get-AcfValue -Key 'appid' -Text $text
    if ($appId -notmatch '^\d+$') { $appId = $file.BaseName -replace 'appmanifest_', '' }
    $info = @{ Stamp = $stamp; AppId = $appId; Flags = $(if ($flagsText -match '^\d+$') { [int64]$flagsText } else { [int64]0 }) }
    # A manifest Steam is just writing can be cut off: only a complete one is kept
    if ($flagsText -match '^\d+$') { $script:manifestCache[$file.FullName] = $info }
    return $info
}

# The drives with their free space, the games Steam works on, and the manifests per library that
# could be read (for noticing uninstalls). A library that is not there (USB disk unplugged) is left
# out: DriveInfo would only give empty values, and Get-ChildItem throws for a missing drive.
function Get-LibraryScan([string]$steamRoot) {
    $downloading = New-Object System.Collections.ArrayList
    $idsByLib = @{}
    $seen = @{}
    $nowUtc = [datetime]::UtcNow
    # Get-SteamLibraryInfo (mqtt_publish.ps1) throws while libraryfolders.vdf is not readable (Steam
    # writing it): then the last list is used
    try {
        $libInfos = @(Get-SteamLibraryInfo $steamRoot)
        $script:lastLibInfos = $libInfos
        Write-LogProblem 'libraries' '' 'libraryfolders.vdf readable again.'
    } catch {
        Write-LogProblem 'libraries' "$($_.Exception.Message) - using the last known libraries."
        $libInfos = if ($script:lastLibInfos) { $script:lastLibInfos } else { @() }
    }
    # The drives to install to: the libraries every round, drives without a library from a list made
    # at most every $drivesEverySec (it may wake sleeping hard disks). The star is read every round.
    $default = Get-SteamDefaultLibrary $steamRoot $libInfos
    Write-LogProblem 'default_library' $(if ($default.Problem) { "$($default.Problem) - the main Steam folder counts as default library." } else { '' }) 'Steam default library readable again.'
    $relist = $null -eq $script:candidateDrivesUtc -or ($nowUtc - $script:candidateDrivesUtc).TotalSeconds -ge $drivesEverySec
    if ($relist) {
        $script:candidateDrives = @(Get-SteamCandidateDrives)
        $script:candidateDrivesUtc = $nowUtc
    }
    # Free space per drive letter of a library, measured once per round (for the drives list)
    $space = @{}
    foreach ($libInfo in $libInfos) {
        if (-not $libInfo.Ready) { continue }
        $lib = $libInfo.Path
        try {
            # Free space in whole GB, so a download does not report every round
            try {
                $letter = Get-SteamDriveLetter $lib
                if ($letter -and -not $space.ContainsKey($letter)) {
                    $drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($lib))
                    $space[$letter] = @{
                        FreeGb  = [int][math]::Floor($drive.AvailableFreeSpace / 1GB)
                        TotalGb = [int][math]::Round($drive.TotalSize / 1GB)
                    }
                }
            } catch { }
            # -LiteralPath: a library path with [ ] is no wildcard
            $apps = [System.IO.Path]::Combine($lib, 'steamapps')
            $ids = New-Object System.Collections.ArrayList
            $queued = @{}
            foreach ($acf in (Get-ChildItem -LiteralPath $apps -Filter 'appmanifest_*.acf' -File -ErrorAction SilentlyContinue)) {
                $seen[$acf.FullName] = $true
                $m = Get-ManifestInfo $acf
                if (-not $m) { continue }
                [void]$ids.Add($m.AppId)
                if (($m.Flags -band $SteamAppState.Active) -ne 0) {
                    if (-not $downloading.Contains($m.AppId)) { [void]$downloading.Add($m.AppId) }
                } elseif (($m.Flags -band $SteamAppState.Queued) -ne 0) {
                    $queued[$m.AppId] = $true
                }
            }
            $idsByLib[$lib] = @($ids | Sort-Object -Unique)
            # Is Steam writing to downloading\<appid> right now? (the manifest progress is not reliable;
            # a manifest written for a first install shows only 'queued' until the download ends)
            $dl = [System.IO.Path]::Combine($apps, 'downloading')
            foreach ($dir in (Get-ChildItem -LiteralPath $dl -Directory -ErrorAction SilentlyContinue)) {
                if ($dir.Name -notmatch '^\d+$' -or $downloading.Contains($dir.Name)) { continue }
                $window = if ($queued.ContainsKey($dir.Name)) { $queuedActiveSec } else { $downloadActiveSec }
                $recent = Get-ChildItem -LiteralPath $dir.FullName -Recurse -File -ErrorAction SilentlyContinue |
                    Where-Object { ($nowUtc - $_.LastWriteTimeUtc).TotalSeconds -lt $window } | Select-Object -First 1
                if ($recent) { [void]$downloading.Add($dir.Name) }
            }
        } catch {
            # The drive went away while it was read: no list for this library this round (no conclusion)
            [void]$idsByLib.Remove($lib)
        }
    }
    # The kind of disk (bus, SSD/HDD) with the drives list, and as soon as a letter comes or goes
    $letters = (@(@($space.Keys) + @($script:candidateDrives | ForEach-Object { $_.Drive })) | Sort-Object -Unique) -join ','
    if ($relist -or $letters -ne $script:driveHardwareLetters) {
        try { $script:driveHardware = Get-SteamDriveHardware } catch { $script:driveHardware = @{} }
        $script:driveHardwareLetters = $letters
    }
    $drives = @(Get-SteamDriveList $libInfos $default $script:candidateDrives $space $script:driveHardware)
    # Forget manifests that are gone
    foreach ($k in @($script:manifestCache.Keys)) { if (-not $seen.ContainsKey($k)) { $script:manifestCache.Remove($k) } }
    # Listed: every library in libraryfolders.vdf (also one whose drive is not there); $null when not known
    $listed = if ($libInfos.Count -gt 0) { @($libInfos | ForEach-Object { $_.Path }) } else { $null }
    return [pscustomobject]@{ Drives = $drives; Downloading = @($downloading.ToArray()); IdsByLib = $idsByLib; Listed = $listed }
}

# The message for steam/agent/state (field order as before: the JSON is compared with the last one).
# $reg: the registry key of the Steam client, read once per round.
function Get-SteamState($scan, $reg) {
    $runningAppId = [int64]0
    if ($reg -and -not [int64]::TryParse("$($reg.RunningAppID)", [ref]$runningAppId)) { $runningAppId = [int64]0 }
    $running = Test-SteamClientRunning
    $libDrives = @($scan.Drives | Where-Object { $_['library'] })
    return [ordered]@{
        steam_running      = if ($running) { 'ON' } else { 'OFF' }
        steam_downloading  = if ($scan.Downloading.Count -gt 0) { 'ON' } else { 'OFF' }
        downloading_appids = @($scan.Downloading)
        # A move job runs (copying, Steam closed, deleting the source): the shutdown waits for it
        steam_moving       = if ((Read-SteamInstallMarker $moveLockFile).Running) { 'ON' } else { 'OFF' }
        game_running       = if ($runningAppId -ne 0) { 'ON' } else { 'OFF' }
        game_appid         = $runningAppId
        # Most free space of all drives with a library
        free_gb            = if ($libDrives.Count -gt 0) { ($libDrives | ForEach-Object { $_['free_gb'] } | Measure-Object -Maximum).Maximum } else { $null }
        # Per drive letter: free space, the library there (or none), the default for first installs
        drives             = @($scan.Drives)
        busy               = Get-Busy
    }
}

# Manifests that disappeared since the last round, compared per library: a library that cannot be read
# right now (USB disk unplugged) keeps its last list and allows no conclusion. A library no longer in
# $listed at all (libraryfolders.vdf; e.g. a move or first install took a new library back) is gone:
# its known IDs count as gone and it is forgotten. Gone: the app IDs; ByLib: the lists to remember.
function Test-ManifestsGone($known, $now, $listed) {
    $byLib = @{}
    foreach ($lib in @($now.Keys)) { $byLib[$lib] = $now[$lib] }
    if ($null -eq $known) { return [pscustomobject]@{ Gone = @(); ByLib = $byLib } }
    foreach ($lib in @($known.Keys)) {
        if ($byLib.ContainsKey($lib) -or ($null -ne $listed -and $listed -notcontains $lib)) { continue }
        $byLib[$lib] = $known[$lib]
    }
    # A library left out of ByLib has no list: all its known IDs are gone
    $gone = @(foreach ($lib in @($known.Keys)) { $known[$lib] | Where-Object { $byLib[$lib] -notcontains $_ } })
    return [pscustomobject]@{ Gone = $gone; ByLib = $byLib }
}

# --- Permanent MQTT connection (send, and receive commands) ----------------------------------------
# Helpers from mqtt_publish.ps1. Never log credentials or put them into error messages.
$script:mqtt = $null
$script:mqttStream = $null
$script:lastRx = [datetime]::UtcNow

function Write-MqttPacket([byte]$header, [byte[]]$body) {
    $p = [System.Collections.Generic.List[byte]]::new()
    $p.Add($header)
    $p.AddRange((Get-MqttRemainingLengthBytes $body.Length))
    $p.AddRange($body)
    $b = $p.ToArray()
    $script:mqttStream.Write($b, 0, $b.Length)
}

function Read-MqttPacket {
    $h = (Read-MqttExactBytes $script:mqttStream 1)[0]
    $len = 0; $mult = 1
    do {
        $d = (Read-MqttExactBytes $script:mqttStream 1)[0]
        $len += ($d -band 0x7F) * $mult
        $mult *= 128
    } while (($d -band 0x80) -ne 0)
    $body = if ($len -gt 0) { Read-MqttExactBytes $script:mqttStream $len } else { [byte[]]@() }
    $script:lastRx = [datetime]::UtcNow
    return @{ Type = ($h -band 0xF0); Flags = ($h -band 0x0F); Body = $body }
}

function Close-Mqtt {
    if ($script:mqtt) { try { $script:mqtt.Close() } catch { } }
    $script:mqtt = $null
    $script:mqttStream = $null
}

function Connect-Mqtt {
    Close-Mqtt
    $client = [System.Net.Sockets.TcpClient]::new()
    $connected = $false
    try {
        $connected = $client.ConnectAsync($mqttHost, $mqttPort).Wait(10000)
    } catch {
        # A failed connect arrives wrapped twice; the innermost message names the cause
        $client.Close()
        throw "Cannot connect to ${mqttHost}:${mqttPort}: $($_.Exception.GetBaseException().Message)"
    }
    if (-not $connected) {
        $client.Close()
        throw "Timeout connecting to ${mqttHost}:${mqttPort}."
    }
    $script:mqtt = $client
    $script:mqttStream = $client.GetStream()
    $script:mqttStream.ReadTimeout = 10000
    $script:mqttStream.WriteTimeout = 10000

    # CONNECT: clean session, last will 'offline' (retained, QoS 1), user and password
    Write-MqttPacket 0x10 (New-MqttConnectBody $mqttClientId $creds.Username $creds.Password $keepAliveSec $statusTopic 'offline')
    $ack = Read-MqttPacket
    if ($ack.Type -ne 0x20 -or $ack.Body.Length -ne 2) { throw 'Unexpected CONNACK from the broker.' }
    if ($ack.Body[1] -ne 0) { throw "MQTT connection refused by the broker, return code $($ack.Body[1])." }

    # SUBSCRIBE to the command topic (QoS 1); the SUBACK arrives in Wait-Commands
    $sub = [System.Collections.Generic.List[byte]]::new()
    $sub.Add(0); $sub.Add(1)
    $sub.AddRange((Get-MqttStringBytes $commandTopic))
    $sub.Add(1)
    Write-MqttPacket 0x82 $sub.ToArray()

    Send-MqttText $statusTopic 'online' $true
    Send-MqttText $SteamEngineTopic.AgentInfo (Get-SteamEngineAgentInfo | ConvertTo-Json -Compress) $true
}

function Send-MqttText($topic, [string]$text, [bool]$retain) {
    # QoS 0: the connection itself is watched by PINGREQ/PINGRESP
    $body = [System.Collections.Generic.List[byte]]::new()
    $body.AddRange((Get-MqttStringBytes $topic))
    $body.AddRange([System.Text.Encoding]::UTF8.GetBytes($text))
    $header = 0x30
    if ($retain) { $header = $header -bor 0x01 }
    Write-MqttPacket ([byte]$header) $body.ToArray()
}

# Announces the entities, and clears those of plugins no longer in plugins.psd1 (an empty retained
# config removes the entity in HA). Nothing is cleared while plugins.psd1 is not readable.
function Send-Discovery {
    $sent = New-Object System.Collections.ArrayList
    foreach ($d in @(Get-SteamEngineDiscovery)) {
        Send-MqttText $d.Topic ($d.Config | ConvertTo-Json -Compress -Depth 6) $true
    }
    foreach ($d in @(Get-SteamEnginePluginDiscovery @($script:plugins.Commands.Keys) @($script:plugins.Busy.Keys))) {
        Send-MqttText $d.Topic ($d.Config | ConvertTo-Json -Compress -Depth 6) $true
        [void]$sent.Add($d.Topic)
    }
    $announced = @()
    $text = Read-SharedTextFile $announcedFile
    if ($text) { $announced = @($text -split "`r?`n" | Where-Object { $_ -match '^homeassistant/' }) }
    $keep = New-Object System.Collections.ArrayList
    foreach ($t in $announced) {
        if ($sent -contains $t -or $keep -contains $t) { continue }
        if (-not $script:plugins.Readable -or $script:plugins.Declared -contains $t) {
            [void]$keep.Add($t)
        } else {
            Send-MqttText $t '' $true
            Write-Log "Plugin entity removed from Home Assistant: $t"
        }
    }
    $now = @($sent) + @($keep)
    if ((($now | Sort-Object) -join "`n") -ne (($announced | Select-Object -Unique | Sort-Object) -join "`n")) {
        try {
            [System.IO.File]::WriteAllLines($announcedFile, [string[]]$now)
        } catch {
            Write-Log "Warning: $announcedFile not written: $($_.Exception.Message)"
        }
    }
}

function Start-Script([string]$path, [string]$arguments) {
    Start-Process powershell.exe -WindowStyle Hidden -PassThru `
        -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + $path + '"' + $arguments)
}

# Stops a report that runs longer than $reportMaxSec (it hangs); called every round
function Stop-HungReport {
    if ($script:reportProc -and -not $script:reportProc.HasExited -and ([datetime]::UtcNow - $script:reportStarted).TotalSeconds -ge $reportMaxSec) {
        Write-Log "Library report still running after $reportMaxSec s, stopped."
        try { $script:reportProc.Kill(); [void]$script:reportProc.WaitForExit(5000) } catch { }
    }
}

# Starts report.ps1 unless it runs already: 'started', 'running' or 'failed'
# (UTC: the change from summer time must not move the next report by an hour)
function Start-Report {
    Stop-HungReport
    if ($script:reportProc -and -not $script:reportProc.HasExited) { return 'running' }
    $script:reportStarted = [datetime]::UtcNow
    $script:nextReport = $script:reportStarted.AddSeconds($reportEverySec)
    if (-not (Test-Path -LiteralPath $reportScript)) {
        # powershell.exe would start and end at once without a trace
        $script:reportProc = $null
        Write-Log "Warning: library report not started, $reportScript is missing."
        return 'failed'
    }
    try {
        $script:reportProc = Start-Script $reportScript ''
        return 'started'
    } catch {
        $script:reportProc = $null
        Write-Log "Warning: library report not started: $($_.Exception.Message)"
        return 'failed'
    }
}

# --- Closing the running games (command close_games) -----------------------------------------------
# A game process is any process whose executable lies in the install folder
# (<library>\steamapps\common\<installdir>\) of an app Steam runs right now: RunningAppID and every app
# with Running = 1 under HKCU\Software\Valve\Steam\Apps (several games at once included). A tool that
# lies in steamapps\common too but was not started by Steam (e.g. Wallpaper Engine started with
# Windows) is left alone. The Steam client and its helpers live outside steamapps\common; their names
# are left out as well, to be safe. The games are first asked to close (their main window gets
# WM_CLOSE), the ones still running after $closeGamesWaitSec are stopped by force. Nothing here waits:
# Wait-Commands calls Step-CloseGames about once a second, so the agent keeps taking commands.
$closeGamesWaitSec = 10
$closeGamesNeverNames = @('steam', 'steamwebhelper', 'steamservice', 'steamerrorreporter', 'gameoverlayui')
$script:closeGames = $null
# When set, Wait-Commands ends early at this time (UTC), so the next round sends the state at once
$script:stateDueAt = $null

# The install folders of the running apps (Get-RunningSteamAppIds; full paths with a trailing
# backslash), from their manifests in all known libraries
function Get-GameFolders {
    $appIds = @(Get-RunningSteamAppIds)
    if ($appIds.Count -eq 0) { return @() }
    $reg = Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue
    $steamRoot = if ($reg -and $reg.SteamPath) { $reg.SteamPath -replace '/', '\' } else { $seCfg.SteamRoot }
    try {
        $libInfos = @(Get-SteamLibraryInfo $steamRoot)
    } catch {
        $libInfos = if ($script:lastLibInfos) { @($script:lastLibInfos) } else { @() }
    }
    $folders = New-Object System.Collections.ArrayList
    foreach ($li in $libInfos) {
        if (-not $li.Ready) { continue }
        try {
            $common = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($li.Path, 'steamapps', 'common')).TrimEnd('\') + '\'
            foreach ($appId in $appIds) {
                $acf = [System.IO.Path]::Combine($li.Path, 'steamapps', ('appmanifest_{0}.acf' -f $appId))
                if (-not (Test-Path -LiteralPath $acf)) { continue }
                $dir = Get-AcfValue -Path $acf -Key 'installdir'
                if (-not $dir -or -not $dir.Trim()) { continue }
                $f = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($common, $dir)).TrimEnd('\') + '\'
                # Only a folder inside steamapps\common (an odd installdir must not widen the match)
                if ($f.Length -le $common.Length -or -not $f.StartsWith($common, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
                if (-not ($folders | Where-Object { $_ -ieq $f })) { [void]$folders.Add($f) }
            }
        } catch { }
    }
    return @($folders.ToArray())
}

# The running game processes (Get-SteamFolderProcesses, which also matches a game folder or library
# behind a junction or symbolic link by its target path): Id, Name, Path, SessionId and the Process
function Get-GameProcesses {
    $folders = @(Get-GameFolders)
    if ($folders.Count -eq 0) { return @() }
    return @(Get-SteamFolderProcesses $folders | Where-Object { $closeGamesNeverNames -notcontains $_.Process.ProcessName })
}

# $true when the process has ended (or its ID now belongs to another program)
function Test-GameExited($game) {
    return ((Get-SteamProcessImagePath $game.Id) -ne $game.Path)
}

function Start-CloseGames {
    if ($script:closeGames) { Write-Log 'Command close_games: the games are being closed already.'; return }
    $mySession = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
    $games = New-Object System.Collections.ArrayList
    foreach ($g in @(Get-GameProcesses)) {
        # Games of another user (another session) are not ours to close
        if ($g.SessionId -ne $mySession) { Write-Log "close_games: $($g.Name) (PID $($g.Id)) runs in another session, skipped."; continue }
        [void]$games.Add($g)
    }
    if ($games.Count -eq 0) { Write-Log 'Command close_games: no game running.'; return }
    Write-Log ('Command close_games: asking to close: ' + ((@($games) | ForEach-Object { "$($_.Name) (PID $($_.Id))" }) -join ', ') + '.')
    foreach ($g in $games) {
        try {
            if ($g.Process.MainWindowHandle -ne [IntPtr]::Zero) { [void]$g.Process.CloseMainWindow() }
        } catch { }
    }
    $script:closeGames = @{ Games = @($games.ToArray()); Deadline = [datetime]::UtcNow.AddSeconds($closeGamesWaitSec) }
}

# One step of a running close: done when all games ended, or after the wait stops the rest by force.
# A process that cannot be stopped (elevated, a service of an anti-cheat) is logged and skipped.
function Step-CloseGames {
    $c = $script:closeGames
    if (-not $c) { return }
    $left = @($c.Games | Where-Object { -not (Test-GameExited $_) })
    if ($left.Count -gt 0 -and [datetime]::UtcNow -lt $c.Deadline) { return }
    $script:closeGames = $null
    if ($left.Count -eq 0) {
        Write-Log 'close_games: all games closed.'
    } else {
        foreach ($g in $left) {
            try {
                Stop-Process -Id $g.Id -Force -ErrorAction Stop
                Write-Log "close_games: $($g.Name) (PID $($g.Id)) did not close within $closeGamesWaitSec s, stopped by force."
            } catch {
                if (Test-GameExited $g) { continue }
                Write-Log "Warning: close_games: $($g.Name) (PID $($g.Id)) not stopped, skipped: $($_.Exception.Message)"
            }
        }
    }
    # Steam clears RunningAppID shortly after the game ended: send the state then, not a round later
    $script:stateDueAt = [datetime]::UtcNow.AddSeconds(3)
}

function Invoke-AgentCommand([string]$command) {
    if ($command -match '^run:([a-z0-9_]+)(?::([a-z0-9_.,=;-]+))?$') {
        # Only commands from the plugin file (allow list), never any path from MQTT. The optional
        # argument (simple characters only) goes to the script as -Job.
        $name = $matches[1]
        $argument = $matches[2]
        $key = @($script:plugins.Commands.Keys | Where-Object { $_.ToLowerInvariant() -eq $name }) | Select-Object -First 1
        if (-not $key) { Write-Log "Command run:$name is not in the plugin file, ignored."; return }
        $scriptPath = $script:plugins.Commands[$key]
        if (-not (Test-Path -LiteralPath $scriptPath)) { Write-Log "Command run:$name ignored, its script does not exist: $scriptPath"; return }
        $extra = if ($argument) { ' -Job "' + $argument + '"' } else { '' }
        Write-Log "Command run:$name -> $scriptPath$extra"
        Start-Script $scriptPath $extra | Out-Null
        return
    }
    if ($command -match '^install:(?:([a-z0-9]{1,16}):)?([0-9]+(?:@[a-z])?(?:,[0-9]+(?:@[a-z])?)*),?$') {
        # install.ps1 runs one job at a time by itself (job lock) and ignores games its running job has.
        # The job ID (optional) goes back to HA in every state of the job. Each game may name a target
        # drive (<appid>@<letter>, only for a first install), never a path.
        # No nested quantifiers in this pattern: it stays linear for any text from MQTT.
        $jobTag = $matches[1]
        $ids = $matches[2]
        $extra = if ($jobTag) { ' -JobId ' + $jobTag } else { '' }
        Write-Log "Command install: install.ps1 started for $ids$(if ($jobTag) { " (job $jobTag)" })."
        Start-Script $installScript (' -JobAppIds ' + $ids + $extra) | Out-Null
        return
    }
    if ($command -match '^uninstall:(\d+)$') {
        # uninstall.ps1 checks by itself for a running game or job. The library goes out once this
        # agent notices the manifest is gone.
        $appId = $matches[1]
        Write-Log "Command uninstall: uninstall.ps1 started for appid=$appId."
        Start-Script $uninstallScript (' -AppId ' + $appId) | Out-Null
        return
    }
    if ($command -match '^move:(\d{1,10})@([a-z])$') {
        # move.ps1 checks everything by itself (job lock, running game, space) and reports on
        # steam/move. Only a drive letter, never a path. The library goes out once this agent
        # notices the manifest is gone from the old library.
        $appId = $matches[1]
        $letter = $matches[2]
        Write-Log "Command move: move.ps1 started for appid=$appId to ${letter}:."
        Start-Script $moveScript (' -AppId ' + $appId + ' -Drive ' + $letter) | Out-Null
        return
    }
    if ($command -match '^launch:(\d+)$') {
        # Steam starts the game, and itself first if it does not run; HA sends this only while no
        # game runs. A locked screen stays locked (the game runs behind it, e.g. for Steam Remote Play).
        $appId = $matches[1]
        Write-Log "Command launch: steam://rungameid/$appId."
        Start-Process ('steam://rungameid/' + $appId)
        return
    }
    switch ($command) {
        'library' {
            $r = Start-Report
            if ($r -eq 'started') { Write-Log 'Command library: report.ps1 started.' }
            elseif ($r -eq 'running') { Write-Log 'Command library: report is running already.' }
        }
        # The shutdown lock (Steam downloading, plugin busy) is checked by HA before it sends this.
        'shutdown' {
            Write-Log 'Command shutdown: shutdown /s.'
            Start-Process shutdown.exe -ArgumentList '/s /t 10 /c "Steam Engine: shutting down the PC"' -WindowStyle Hidden
        }
        'restart' {
            Write-Log 'Command restart: shutdown /r.'
            Start-Process shutdown.exe -ArgumentList '/r /t 10 /c "Steam Engine: restarting the PC"' -WindowStyle Hidden
        }
        'lock' {
            Write-Log 'Command lock.'
            Start-Process rundll32.exe -ArgumentList 'user32.dll,LockWorkStation'
        }
        'close_games' {
            # Asks the games to close and returns; Step-CloseGames (in Wait-Commands) finishes it
            Start-CloseGames
        }
        'ping' {
            # Only to test the command path, does nothing
            Write-Log 'Command ping received.'
        }
        default {
            Write-Log "Unknown command ignored: '$(Get-ShortText $command)'"
        }
    }
}

function Get-ShortText([string]$text) {
    if ($text.Length -gt 40) { return $text.Substring(0, 40) }
    return $text
}

function Wait-Commands([int]$seconds) {
    # Replaces a sleep in the main loop: waits for commands and keeps the connection alive
    Write-MqttPacket 0xC0 ([byte[]]@())   # PINGREQ
    $deadline = [datetime]::UtcNow.AddSeconds($seconds)
    while ([datetime]::UtcNow -lt $deadline) {
        if ($script:closeGames) {
            try { Step-CloseGames } catch { $script:closeGames = $null; Write-Log "Warning: close_games: $($_.Exception.Message)" }
        }
        if ($script:stateDueAt -and [datetime]::UtcNow -ge $script:stateDueAt) {
            # A command changed the state: end the wait, the next round collects and sends it
            $script:stateDueAt = $null
            break
        }
        if (-not $script:mqtt.Client.Poll(1000000, [System.Net.Sockets.SelectMode]::SelectRead)) { continue }
        if ($script:mqtt.Available -eq 0) { throw 'MQTT connection closed by the broker.' }
        $pkt = Read-MqttPacket
        if ($pkt.Type -eq 0x90) {
            # SUBACK: 0x80 = the broker refused the subscription (ACL), no commands would arrive
            if ($pkt.Body.Length -ge 3 -and $pkt.Body[$pkt.Body.Length - 1] -eq 0x80) {
                Write-LogProblem 'subscribe' "the broker refused the subscription to $commandTopic, no commands arrive (check the MQTT user's rights)."
            } else {
                Write-LogProblem 'subscribe' '' "Subscription to $commandTopic accepted again."
            }
            continue
        }
        if ($pkt.Type -ne 0x30) { continue }   # PINGRESP, ... only count as a sign of life
        $b = $pkt.Body
        $tlen = ([int]$b[0] -shl 8) -bor [int]$b[1]
        $topic = [System.Text.Encoding]::UTF8.GetString($b, 2, $tlen)
        $pos = 2 + $tlen
        $qos = ($pkt.Flags -shr 1) -band 3
        if ($qos -gt 0) {
            Write-MqttPacket 0x40 ([byte[]]@($b[$pos], $b[$pos + 1]))   # PUBACK
            $pos += 2
        }
        if ($topic -ne $commandTopic) { continue }
        if (($pkt.Flags -band 0x01) -ne 0) {
            Write-Log 'Retained command ignored (only fresh commands count).'
            continue
        }
        $command = [System.Text.Encoding]::UTF8.GetString($b, $pos, $b.Length - $pos).Trim().ToLowerInvariant()
        # A command that fails (e.g. no handler for steam://) is no MQTT problem
        try {
            Invoke-AgentCommand $command
        } catch {
            Write-Log "Command '$(Get-ShortText $command)' failed: $($_.Exception.Message)"
        }
    }
    if (([datetime]::UtcNow - $script:lastRx).TotalSeconds -gt ($seconds * 3)) {
        throw 'No answer from the MQTT broker.'
    }
}

# --- Main loop -----------------------------------------------------------------------------------

try {
    $creds = Get-MqttCredentials $credFile
    if ([string]::IsNullOrWhiteSpace($creds.Username) -or [string]::IsNullOrWhiteSpace($creds.Password)) {
        Write-Log "Stopped: MQTT login not usable: $($creds.Problem) ($credFile)."
        exit 1
    }
    if ($seCfg.ConfigProblem) { Write-Log "Warning: $($seCfg.ConfigProblem)." }
    $script:plugins = Get-Plugins
    Write-Log ("Plugins: commands [{0}], busy [{1}]" -f (@($script:plugins.Commands.Keys) -join ', '), (@($script:plugins.Busy.Keys) -join ', '))
    if (@(Get-ChildItem -LiteralPath $moveJournalDir -Filter 'move_pending_*.json' -File -ErrorAction SilentlyContinue).Count -gt 0) {
        # A move was cut off after its switch (PC off) or left a rest: finish it or take it back now
        Write-Log 'An interrupted move is left: move.ps1 -Recover started.'
        Start-Script $moveScript ' -Recover' | Out-Null
    }
    $lastJson = ''
    $lastSent = [datetime]::MinValue
    $knownByLib = $null
    $connFailures = 0
    $connWaitSec = $intervalSec
    $script:reportProc = $null
    $script:reportStarted = [datetime]::MinValue
    $script:nextReport = [datetime]::UtcNow.AddSeconds($reportFirstSec)
    while ($true) {
        try {
            # 1. Connect and announce the entities; on errors wait and try again
            if (-not $script:mqtt) {
                try {
                    Connect-Mqtt
                    Send-Discovery
                } catch {
                    $msg = $_.Exception.Message
                    Close-Mqtt
                    $connFailures++
                    # A refused login does not get better by itself: wait longer each time
                    $connWaitSec = if ($msg -match 'return code [45]\b') { [math]::Min($loginRetryMaxSec, $connWaitSec * 2) } else { $intervalSec }
                    if ($connFailures -eq 1 -or $msg -ne $script:connLastError) {
                        Write-Log "Warning: $msg Next try in $connWaitSec s."
                    } elseif ($connFailures % 90 -eq 0) {
                        Write-Log "Warning: still no MQTT connection after $connFailures tries: $msg"
                    }
                    $script:connLastError = $msg
                    Start-Sleep -Seconds $connWaitSec
                    continue
                }
                Write-Log ("MQTT connected to {0}:{1}{2}." -f $mqttHost, $mqttPort, $(if ($connFailures -gt 0) { " after $connFailures failed tries" } else { '' }))
                $connFailures = 0
                $connWaitSec = $intervalSec
                $script:connLastError = ''
                $lastJson = ''
            }

            # 2. Collect the data; errors here are logged and never drop the connection
            $stateJson = $null
            try {
                # The Steam folder and the running game from one read of the Steam client's key
                $reg = Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue
                $steamRoot = if ($reg -and $reg.SteamPath) { $reg.SteamPath -replace '/', '\' } else { $seCfg.SteamRoot }
                $scan = Get-LibraryScan $steamRoot
                # Uninstall (manifest gone): send the library at once, otherwise only with the next
                # regular report. HA asks for the library itself after a download.
                $g = Test-ManifestsGone $knownByLib $scan.IdsByLib $scan.Listed
                $byLib = $g.ByLib
                if ($g.Gone.Count -gt 0) {
                    $r = Start-Report
                    if ($r -eq 'running') {
                        $byLib = $knownByLib   # still running, try again next round
                    } elseif ($r -eq 'started') {
                        Write-Log "Manifest removed (appid $($g.Gone -join ', ')), library report started."
                    }
                }
                $knownByLib = $byLib
                $stateJson = (Get-SteamState $scan $reg) | ConvertTo-Json -Compress -Depth 6
                Write-LogProblem 'state' '' 'State collected again.'
            } catch {
                Write-LogProblem 'state' "state not collected: $($_.Exception.Message)"
            }
            # Regular report (sizes, last played, updates Steam made by itself); while one runs, next round
            try {
                Stop-HungReport
                if ([datetime]::UtcNow -ge $script:nextReport) { [void](Start-Report) }
            } catch {
                Write-LogProblem 'report' "library report: $($_.Exception.Message)"
            }
            $telemetryJson = $null
            try {
                $telemetryJson = (Get-Telemetry) | ConvertTo-Json -Compress -Depth 6
            } catch {
                Write-LogProblem 'telemetry' "telemetry not collected: $($_.Exception.Message)"
            }

            # 3. Send and wait for commands; an error here is one of the connection
            try {
                if ($stateJson -and ($stateJson -cne $lastJson -or ([datetime]::UtcNow - $lastSent).TotalSeconds -ge $refreshSec)) {
                    Send-MqttText $stateTopic $stateJson $true
                    if ($stateJson -cne $lastJson) { Write-Log "State: $stateJson" }
                    $lastJson = $stateJson
                    $lastSent = [datetime]::UtcNow
                }
                # Telemetry every round (not retained, not logged); HA also takes it as the PC's sign of life
                if ($telemetryJson) { Send-MqttText $telemetryTopic $telemetryJson $false }
                Wait-Commands $intervalSec
            } catch {
                # Broker gone for a moment or similar: connect again, send discovery and state again
                Write-Log "Warning: MQTT connection lost: $($_.Exception.Message)"
                Close-Mqtt
                Start-Sleep -Seconds $intervalSec
            }
        } catch {
            # Last resort: nothing but a deliberate exit may end the agent
            Write-Log "Warning: unexpected error: $($_.Exception.Message)"
            Close-Mqtt
            Start-Sleep -Seconds $intervalSec
        }
    }
} finally {
    if ($script:mqtt) {
        # Stopped cleanly: report offline ourselves (on DISCONNECT the broker does not send the last will)
        try { Send-MqttText $statusTopic 'offline' $true; Write-MqttPacket 0xE0 ([byte[]]@()) } catch { }
        Close-Mqtt
    }
    try { $mutex.ReleaseMutex() } catch { }
}
