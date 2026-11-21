# Library report: sends the installed depots per game (steam/depots), the installed Steam games
# (steam/library) and the update history (steam/history) to Home Assistant, all retained (see
# PROTOCOL.md). Also notices a Steam job that was interrupted and reports it as 'aborted' on
# steam/job (or sends the end state the job could not send), and sends the end state of a move that
# could not be sent (move_final.json, on steam/move). Started by the agent (a minute after its
# start, every 30 minutes, when a manifest is gone, on its command 'library') and by install.ps1 at
# the end of a job that installed or updated a game; one run at a time (mutex below).
# ASCII only in this file (Windows PowerShell 5.1 reads .ps1 without BOM as ANSI).
$ErrorActionPreference = 'Stop'

$mqttClientId = ($env:COMPUTERNAME.ToLower() + '-steam-library')
$seRoot = Join-Path $env:LOCALAPPDATA 'SteamEngine'
$credFile = Join-Path $seRoot 'mqtt_credentials.txt'
$logFile = Join-Path $PSScriptRoot 'report.log'
# The folder of install.ps1 and uninstall.ps1 (their logs and install.lock)
$jobsDir = Join-Path $seRoot 'jobs'
$inv = [System.Globalization.CultureInfo]::InvariantCulture

try {
    . (Join-Path $seRoot 'mqtt_publish.ps1')
} catch {
    # Without the shared library there is no log helper and no MQTT: one plain line, then stop
    try { [System.IO.File]::AppendAllText($logFile, ([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss', $inv) + "  Stopped: mqtt_publish.ps1 not loaded: $($_.Exception.Message)`r`n")) } catch { }
    exit 1
}

function Write-Log([string]$message) { Write-SteamEngineLog $logFile $message }
if ($SteamEngineNamesProblem) { Write-Log "Stopped: $SteamEngineNamesProblem"; exit 1 }

$mqttTopic = $SteamEngineTopic.Library

# One report at a time: install.ps1 runs it at the end of a job, the agent on its timer and on HA's
# command, often in the same second. Two at once added history entries twice and shared the MQTT
# client IDs.
# The second one waits here and then reads what the first one wrote. Released in the finally block
# below; abandoned only when a report was stopped (the agent stops one that hangs).
$reportMutex = New-Object System.Threading.Mutex($false, 'Local\SteamEngineReport')
$owned = $false
try {
    $owned = $reportMutex.WaitOne(120000)
} catch {
    if ($_.Exception -is [System.Threading.AbandonedMutexException] -or $_.Exception.InnerException -is [System.Threading.AbandonedMutexException]) {
        $owned = $true
        Write-Log 'The last library report was stopped before its end.'
    } else { throw }
}
if (-not $owned) {
    Write-Log 'Stopped: another library report still runs after 2 minutes.'
    exit 1
}

# Times of the OK lines per appid in the end of a job log (and of its rotated .old part); the last
# 256 KB hold many weeks of jobs
function Get-OkTimes([string]$path) {
    $map = @{}
    $text = [string](Read-FileTail ($path + '.old') 65536) + "`n" + [string](Read-FileTail $path 262144)
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+OK: appid=(\d+) ') {
            $t = [datetime]::ParseExact($matches[1], 'yyyy-MM-dd HH:mm:ss', $inv)
            if (-not $map.ContainsKey($matches[2])) { $map[$matches[2]] = New-Object System.Collections.ArrayList }
            [void]$map[$matches[2]].Add($t)
        }
    }
    return $map
}

# 'ha' when a Steam Engine job did it within 20 minutes of $time (OK line in install.log or, for a
# removal, in uninstall.log), otherwise 'steam' (the Steam client by itself)
function New-HistoryEntry($okTimes, [datetime]$time, $appid, $name, $kind, $oldBuild, $newBuild) {
    $source = 'steam'
    $ok = if ($kind -eq 'removed') { $okTimes.Removed } else { $okTimes.Installed }
    if ($ok.ContainsKey([string]$appid)) {
        foreach ($t in $ok[[string]$appid]) {
            if ([math]::Abs(($t - $time).TotalMinutes) -le 20) { $source = 'ha'; break }
        }
    }
    return [ordered]@{
        time      = $time.ToString('yyyy-MM-ddTHH:mm:sszzz', $inv)
        appid     = [string]$appid
        name      = $name
        kind      = $kind
        source    = $source
        old_build = [string]$oldBuild
        new_build = [string]$newBuild
    }
}

# Sort key for history times (local time with offset; across a change of the offset the text alone
# would sort wrong)
function Get-HistoryTimeKey($entry) {
    $t = [DateTimeOffset]::MinValue
    [void][DateTimeOffset]::TryParse([string]$entry.time, $inv, [System.Globalization.DateTimeStyles]::None, [ref]$t)
    return $t.UtcDateTime
}

$exitCode = 0
try {
    $seCfg = Get-SteamEngineConfig
    $mqttHost = $seCfg.MqttHost
    $mqttPort = $seCfg.MqttPort
    if ($seCfg.ConfigProblem) { Write-Log "Warning: $($seCfg.ConfigProblem)." }

    $mqttCreds = Get-MqttCredentials $credFile
    if ([string]::IsNullOrWhiteSpace($mqttCreds.Username) -or [string]::IsNullOrWhiteSpace($mqttCreds.Password)) {
        throw "MQTT login not usable: $($mqttCreds.Problem) ($credFile)"
    }

    # An interrupted Steam job: install.lock is still there, but no job holds the job lock (the PC
    # was switched off in the middle of install.ps1, or it was stopped). HA would otherwise keep the
    # last (not retained) 'downloading' and report a stuck download. A job that could not send its
    # end state left it in install_final.json: that one is sent instead of 'aborted'.
    $installLock = Join-Path $jobsDir 'install.lock'
    $finalFile = Join-Path $jobsDir 'install_final.json'
    if (Test-Path -LiteralPath $installLock) {
        # Held = a job runs (install.ps1 also starts this report while it holds the lock)
        $jobLock = Enter-SteamEngineJobLock 0
        # Looked at again with the lock: a job that ended cleanly just now removed install.lock before
        # it freed the lock.
        if ($jobLock -and -not (Test-Path -LiteralPath $installLock)) {
            Exit-SteamEngineJobLock $jobLock
            $jobLock = $null
        }
        if ($jobLock) {
            try {
                $stateBytes = $null
                $pending = Read-SharedTextFile $finalFile
                if ($pending) {
                    try {
                        if ((ConvertFrom-Json $pending).state -in @('done', 'done_with_errors')) { $stateBytes = [System.Text.Encoding]::UTF8.GetBytes($pending.Trim()) }
                    } catch { }
                }
                $what = 'end state of the last job sent late'
                if (-not $stateBytes) {
                    $stateBody = [ordered]@{ state = 'aborted' }
                    # The job ID from install.lock: Home Assistant knows which job ended
                    $lockJob = (Read-SteamInstallMarker $installLock).Job
                    if ($lockJob) { $stateBody['job'] = $lockJob }
                    $stateBody['reason'] = 'The job was interrupted (PC off or script stopped). Start it again.'
                    $stateBytes = [System.Text.Encoding]::UTF8.GetBytes(($stateBody | ConvertTo-Json -Compress))
                    $what = "state 'aborted' sent"
                }
                Publish-MqttRetained -BrokerHost $mqttHost -Port $mqttPort -ClientId ($env:COMPUTERNAME.ToLower() + '-steam-job-check') `
                    -Username $mqttCreds.Username -Password $mqttCreds.Password -Topic $SteamEngineTopic.Job -PayloadBytes $stateBytes -Qos 1 -Retain $true
                Remove-Item -LiteralPath $finalFile -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $installLock -Force -ErrorAction SilentlyContinue
                Write-SteamEngineLog (Join-Path $jobsDir 'install.log') "Interrupted job found (lock file without a running install.ps1) - $what."
                Write-Log "Interrupted Steam job found - $what."
            } catch {
                Write-Log "Warning: check for an interrupted Steam job failed: $($_.Exception.Message)"
            } finally {
                Exit-SteamEngineJobLock $jobLock
            }
        }
    }

    # The end state of a move that could not be sent (MQTT down at its end), kept in move_final.json:
    # sent now (retained) while no Steam job runs; a move that starts sends it first by itself
    $moveFinal = Join-Path $jobsDir 'move_final.json'
    if ([System.IO.File]::Exists($moveFinal)) {
        $jobLock = Enter-SteamEngineJobLock 0
        if ($jobLock) {
            try {
                if (Send-SteamPendingEnd -Path $moveFinal -BrokerHost $mqttHost -Port $mqttPort -ClientId ($env:COMPUTERNAME.ToLower() + '-steam-move-check') `
                        -Username $mqttCreds.Username -Password $mqttCreds.Password -Topic $SteamEngineTopic.Move) {
                    Write-SteamEngineLog (Join-Path $jobsDir 'move.log') 'End state of the last move sent late (report.ps1).'
                    Write-Log 'End state of the last move sent late.'
                }
            } catch {
                Write-Log "Warning: end state of the last move not sent: $($_.Exception.Message)"
            } finally {
                Exit-SteamEngineJobLock $jobLock
            }
        }
    }

    # Apps that have an appmanifest but are no games (never listed in the Steam library)
    $excludedAppIds = @('228980')  # Steamworks Common Redistributables

    $steamRoot = $seCfg.SteamRoot
    # Libraries without duplicates (the main folder is in the registry and in the vdf, in other case).
    # Throws while libraryfolders.vdf stays unreadable: a list without the other libraries would make
    # their games look uninstalled (the catch at the end logs it).
    $libraries = @(Get-SteamLibraryInfo $steamRoot)
    # Games on a library that is not there right now (USB disk unplugged): listed with their last known
    # data from this cache and not reported as removed, so they do not look uninstalled in HA
    $cacheFile = Join-Path $PSScriptRoot 'library_cache.json'
    $unavailableIds = New-Object System.Collections.ArrayList
    # Drive of each unavailable game's library (its cache entry may predate the drive field)
    $unavailableDrive = @{}
    $depotsByApp = [ordered]@{}

    $games = @()
    $lastUpdated = @{}

    # Last played (for the deletion suggestions when space runs out): reliable in the Steam account's
    # localconfig.vdf, in the appmanifest often only 0. Several accounts: the latest value counts.
    $lastPlayed = @{}
    foreach ($lc in (Get-ChildItem (Join-Path $steamRoot 'userdata\*\config\localconfig.vdf') -ErrorAction SilentlyContinue)) {
        $lcText = Read-SharedTextFile $lc.FullName
        if ($null -eq $lcText) {
            Write-Log "Warning: localconfig.vdf not readable ($($lc.FullName))."
            continue
        }
        foreach ($m in [regex]::Matches($lcText, '"(\d+)"\s*\{[^{}]*?"LastPlayed"\s+"(\d+)"')) {
            $v = [int64]$m.Groups[2].Value
            if ($v -gt 0 -and (-not $lastPlayed.ContainsKey($m.Groups[1].Value) -or $lastPlayed[$m.Groups[1].Value] -lt $v)) {
                $lastPlayed[$m.Groups[1].Value] = $v
            }
        }
    }
    foreach ($lib in $libraries) {
        # Drive letter of the library ('F:'), $null for a path without one (a network share)
        $libDrive = Get-SteamDriveLetter $lib.Path
        if (-not $libDrive) { $libDrive = $null }
        $acfFiles = $null
        if ($lib.Ready) {
            # -File throws for a drive that went away just now: then like an unplugged one
            try {
                $acfFiles = @(Get-ChildItem -LiteralPath ([System.IO.Path]::Combine($lib.Path, 'steamapps')) -Filter 'appmanifest_*.acf' -File -ErrorAction SilentlyContinue)
            } catch { $acfFiles = $null }
        }
        if ($null -eq $acfFiles) {
            if ($lib.Ready -or -not $lib.DriveReady) {
                # Drive not there (USB disk unplugged): the games stay with their last known state
                foreach ($id in $lib.AppIds) {
                    if (-not $unavailableIds.Contains([string]$id)) {
                        [void]$unavailableIds.Add([string]$id)
                        $unavailableDrive[[string]$id] = $libDrive
                    }
                }
                Write-Log "Library not available right now: $($lib.Path) ($($lib.AppIds.Count) games keep their last known state)."
            } else {
                # Drive there, library folder gone (deleted): its games are gone too
                Write-Log "Library folder missing: $($lib.Path) (still listed by Steam, games counted as removed)."
            }
            continue
        }
        foreach ($acf in $acfFiles) {
            # Read shared; a manifest deleted between listing and reading is left out
            $c = Read-SharedTextFile $acf.FullName
            if (-not $c) { continue }
            $appid = if ($c -match '"appid"\s+"(\d+)"') { $matches[1] } else { $null }
            if (-not $appid -or $excludedAppIds -contains $appid) { continue }
            $name = if ($c -match '"name"\s+"([^"]*)"') { $matches[1] } else { '' }
            $buildid = if ($c -match '"buildid"\s+"(\d+)"') { $matches[1] } else { '' }
            $sizeOnDisk = if ($c -match '"SizeOnDisk"\s+"(\d+)"') { [int64]$matches[1] } else { 0 }
            # Workshop content: SizeOnDisk of appworkshop_<appid>.acf (moving a game needs this space too)
            $workshopMb = 0
            $wsAcf = [System.IO.Path]::Combine($acf.DirectoryName, 'workshop', ('appworkshop_{0}.acf' -f $appid))
            if ([System.IO.File]::Exists($wsAcf)) {
                $wc = Read-SharedTextFile $wsAcf
                if ($wc -and $wc -match '"SizeOnDisk"\s+"(\d+)"') { $workshopMb = [math]::Round([int64]$matches[1] / 1MB) }
            }
            # the first manifest of a game wins, like for the library entry and the depots
            if ($c -match '"LastUpdated"\s+"(\d+)"' -and [int64]$matches[1] -gt 0 -and -not $lastUpdated.ContainsKey([string]$appid)) {
                $lastUpdated[[string]$appid] = [DateTimeOffset]::FromUnixTimeSeconds([int64]$matches[1]).LocalDateTime
            }
            $lp = if ($lastPlayed.ContainsKey([string]$appid)) { $lastPlayed[[string]$appid] } elseif ($c -match '"LastPlayed"\s+"(\d+)"') { [int64]$matches[1] } else { 0 }
            # Installed depots with their manifest IDs ("InstalledDepots" { "<depot>" { "manifest" "<id>" ... } }):
            # HA compares them with the public ones, an update of one depot can keep the build ID.
            # Sent on their own topic (steam/depots), not in the library sensor's attributes.
            # Only the InstalledDepots block: "StagedDepots" (an update half done) has the same shape
            # with the new IDs and would hide the update
            $depots = [ordered]@{}
            $im = [regex]::Match($c, '"InstalledDepots"\s*\{((?:[^{}]|\{[^{}]*\})*)\}')
            if ($im.Success) {
                foreach ($dm in [regex]::Matches($im.Groups[1].Value, '"(\d+)"\s*\{\s*"manifest"\s+"(\d+)"')) { $depots[$dm.Groups[1].Value] = $dm.Groups[2].Value }
            }
            # the first manifest of a game wins, like for the library entry (a stale copy elsewhere)
            if (-not $depotsByApp.Contains([string]$appid)) { $depotsByApp[[string]$appid] = $depots }
            $games += [ordered]@{
                appid       = [string]$appid
                name        = $name
                buildid     = [string]$buildid
                size_mb     = [math]::Round($sizeOnDisk / 1MB)
                workshop_mb = $workshopMb
                drive       = $libDrive
                last_played = if ($lp -gt 0) { [DateTimeOffset]::FromUnixTimeSeconds($lp).LocalDateTime.ToString('yyyy-MM-dd', $inv) } else { '' }
            }
        }
    }

    # One entry per game, also if a folder is reachable under two names
    $games = @($games | Group-Object { $_.appid } | ForEach-Object { $_.Group[0] })

    # Games of an unavailable library: last known data from the cache
    $cache = @{}
    if (Test-Path -LiteralPath $cacheFile) {
        try {
            $raw = [System.IO.File]::ReadAllText($cacheFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
            foreach ($g in @($raw)) {
                $cache[[string]$g.appid] = [ordered]@{
                    appid = [string]$g.appid; name = [string]$g.name; buildid = [string]$g.buildid
                    size_mb = $g.size_mb; workshop_mb = $(if ($null -ne $g.workshop_mb) { $g.workshop_mb } else { 0 })
                    drive = $null; last_played = [string]$g.last_played
                }
            }
        } catch {
            Write-Log "Warning: library cache not readable: $($_.Exception.Message)"
        }
    }
    $present = @{}
    foreach ($g in $games) { $present[$g.appid] = $true }
    foreach ($id in $unavailableIds) {
        if (-not $present.ContainsKey($id) -and $cache.ContainsKey($id) -and $excludedAppIds -notcontains $id) {
            # marked, so HA leaves it out of update checks and deletion suggestions until the drive is back
            $entry = $cache[$id]
            $entry['drive'] = $unavailableDrive[$id]
            $entry['unavailable'] = $true
            $games += $entry
            $present[$id] = $true
        }
    }

    $games = @($games | Sort-Object { $_.name })
    try {
        [System.IO.File]::WriteAllText($cacheFile, (ConvertTo-Json -InputObject @($games) -Depth 4), (New-Object System.Text.UTF8Encoding $false))
    } catch {
        Write-Log "Warning: library cache not written: $($_.Exception.Message)"
    }

    # --- Update history ---
    # Compares the build IDs with the last known state and writes every change to history.jsonl
    # (one JSON line per event). The latest entries go retained to steam/history. Source: see
    # New-HistoryEntry.
    $historyTopic  = $SteamEngineTopic.History
    $stateFile     = Join-Path $PSScriptRoot 'history_state.json'
    $historyFile   = Join-Path $PSScriptRoot 'history.jsonl'
    $historyShown  = 30
    # Only the end of history.jsonl is parsed for the newest entries (it only grows)
    $historyTail   = 200
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)

    $historyChanged = $false
    try {
        $okTimes = @{
            Installed = Get-OkTimes (Join-Path $jobsDir 'install.log')
            Removed   = Get-OkTimes (Join-Path $jobsDir 'uninstall.log')
        }
        $now = Get-Date
        $entries = New-Object System.Collections.ArrayList
        $known = @{}
        $knownExists = Test-Path -LiteralPath $stateFile
        $knownTime = $now
        if ($knownExists) {
            $knownTime = (Get-Item -LiteralPath $stateFile).LastWriteTime
            $raw = [System.IO.File]::ReadAllText($stateFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
            foreach ($p in $raw.PSObject.Properties) { $known[$p.Name] = $p.Value }
        }

        $current = @{}
        foreach ($g in $games) { $current[$g.appid] = $g }

        foreach ($g in $games) {
            if ([string]::IsNullOrEmpty($g.buildid) -or $g.buildid -eq '0') { continue }
            $time = $now
            if ($lastUpdated.ContainsKey($g.appid)) { $time = $lastUpdated[$g.appid] }
            if (-not $knownExists) {
                # First run: rebuild the history from LastUpdated of the manifests (old build ID unknown).
                if ($lastUpdated.ContainsKey($g.appid)) {
                    [void]$entries.Add((New-HistoryEntry $okTimes $time $g.appid $g.name 'updated' '' $g.buildid))
                }
            } elseif (-not $known.ContainsKey($g.appid)) {
                if ($time -lt $knownTime.AddMinutes(-5)) { $time = $now }
                [void]$entries.Add((New-HistoryEntry $okTimes $time $g.appid $g.name 'installed' '' $g.buildid))
            } elseif ([string]$known[$g.appid].buildid -ne $g.buildid) {
                if ($time -lt $knownTime.AddMinutes(-5)) { $time = $now }
                [void]$entries.Add((New-HistoryEntry $okTimes $time $g.appid $g.name 'updated' $known[$g.appid].buildid $g.buildid))
            }
        }
        if ($knownExists) {
            foreach ($id in @($known.Keys)) {
                # A game on a library that is not there right now is not removed
                if (-not $current.ContainsKey($id) -and -not $unavailableIds.Contains([string]$id)) {
                    [void]$entries.Add((New-HistoryEntry $okTimes $now $id $known[$id].name 'removed' $known[$id].buildid ''))
                }
            }
        }

        if ($entries.Count -gt 0) {
            $lines = @($entries | Sort-Object { Get-HistoryTimeKey $_ } | ForEach-Object { ConvertTo-Json -InputObject $_ -Compress })
            [System.IO.File]::AppendAllText($historyFile, (($lines -join "`n") + "`n"), $utf8NoBom)
            $historyChanged = $true
            foreach ($e in $entries) { Write-Log "History: $($e.kind) appid=$($e.appid) ($($e.source)) $($e.old_build) -> $($e.new_build)" }
        }

        # Write the new state; games without a valid build ID keep their old state.
        $newKnown = [ordered]@{}
        foreach ($g in $games) {
            if ([string]::IsNullOrEmpty($g.buildid) -or $g.buildid -eq '0') {
                if ($known.ContainsKey($g.appid)) { $newKnown[$g.appid] = $known[$g.appid] }
            } else {
                $newKnown[$g.appid] = [ordered]@{ buildid = $g.buildid; name = $g.name }
            }
        }
        foreach ($id in $unavailableIds) {
            if (-not $newKnown.Contains($id) -and $known.ContainsKey($id)) { $newKnown[$id] = $known[$id] }
        }
        if (-not $knownExists -or $entries.Count -gt 0) {
            [System.IO.File]::WriteAllText($stateFile, (ConvertTo-Json -InputObject $newKnown -Depth 4), $utf8NoBom)
        }
    } catch {
        Write-Log "Warning: update history failed: $($_.Exception.Message)"
    }

    $payload = [ordered]@{
        count = $games.Count
        games = $games
    }
    $bodyText = $payload | ConvertTo-Json -Depth 6
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($bodyText)

    try {
        # The depots first: HA checks for updates as soon as the library changes, and must then
        # already compare with the depots after the download
        $depotBytes = [System.Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $depotsByApp -Depth 4 -Compress))
        Publish-MqttRetained -BrokerHost $mqttHost -Port $mqttPort -ClientId ($mqttClientId + '-depots') `
            -Username $mqttCreds.Username -Password $mqttCreds.Password -Topic $SteamEngineTopic.Depots -PayloadBytes $depotBytes -Qos 1 -Retain $true
        Publish-MqttRetained -BrokerHost $mqttHost -Port $mqttPort -ClientId $mqttClientId `
            -Username $mqttCreds.Username -Password $mqttCreds.Password -Topic $mqttTopic -PayloadBytes $bodyBytes -Qos 1 -Retain $true
        Write-Log "OK: $($games.Count) installed games sent over MQTT (retained, topic $mqttTopic)."
    } catch {
        # Only log the error message itself (return code or generic text) - never the CONNECT payload
        # or credentials. See the security note in mqtt_publish.ps1.
        Write-Log "MQTT publish failed: $($_.Exception.Message)"
        exit 1
    }

    try {
        # All lines are counted, only the last ones are parsed
        $lines = @()
        $historyText = Read-SharedTextFile $historyFile
        if ($historyText) { $lines = @($historyText -split "`r?`n" | Where-Object { $_.Trim() }) }
        $recent = @($lines | Select-Object -Last $historyTail | ForEach-Object { try { $_ | ConvertFrom-Json } catch { } })
        $latest = @($recent | Sort-Object { Get-HistoryTimeKey $_ } -Descending | Select-Object -First $historyShown)
        $historyPayload = [ordered]@{
            latest  = if ($latest.Count -gt 0) { $latest[0].time } else { $null }
            total   = $lines.Count
            entries = $latest
        }
        $historyBytes = [System.Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $historyPayload -Depth 5 -Compress))
        Publish-MqttRetained -BrokerHost $mqttHost -Port $mqttPort -ClientId ($mqttClientId + '-history') `
            -Username $mqttCreds.Username -Password $mqttCreds.Password -Topic $historyTopic -PayloadBytes $historyBytes -Qos 1 -Retain $true
        if ($historyChanged) { Write-Log "OK: update history sent ($($latest.Count) of $($lines.Count) entries, topic $historyTopic)." }
    } catch {
        # Only log the error message itself (return code or generic text) - never the CONNECT payload
        # or credentials. See the security note in mqtt_publish.ps1.
        Write-Log "MQTT publish failed: $($_.Exception.Message)"
        exit 1
    }
} catch {
    Write-Log "Stopped: $($_.Exception.Message)"
    $exitCode = 1
} finally {
    try { $reportMutex.ReleaseMutex() } catch { }
    $reportMutex.Dispose()
}
exit $exitCode
