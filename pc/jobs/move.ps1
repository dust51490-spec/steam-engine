param([string]$AppId = '', [string]$Drive = '', [switch]$Recover)
# Moves an installed Steam game to the Steam library on another drive. Started by the Steam agent
# (command move:<appid>@<letter>, see PROTOCOL.md); progress and result go to Home Assistant over MQTT
# (topic steam/move) with a reason code on errors. An end state that cannot be sent (MQTT down) is kept
# in jobs\move_final.json and sent later (retained) by the next move.ps1 or report.ps1.
# Steps: checks; copy the game folder (and its workshop content) into a staging folder on the target
# drive while Steam runs; close the Steam client (not while a game runs); copy what changed meanwhile;
# compare file count and bytes; a drive without a library gets a new one (New-SteamLibrary, only
# while Steam is closed); write the appmanifest (and appworkshop_<appid>.acf) to the target, rename the
# staging folder to the game's folder, remove the source manifest; start the client and wait until
# Steam loaded the library and shows the game as installed (StateFlags 4); only then delete the source.
# Any failure before that takes the target back (with the client closed again when it ran) and leaves
# the source as it was.
# From the switch on (manifest written, folder renamed) a journal (jobs\move_pending_<appid>_<time>.json,
# one per move) says what this move did: a move cut off there (PC off, job killed) is finished or taken
# back by the next move.ps1, or by "move.ps1 -Recover", which the agent starts at its start when a
# journal is there.
# One Steam job at a time: the job lock (mqtt_publish.ps1) is shared with install.ps1 and uninstall.ps1.
# While the job runs, jobs\move.lock names it (the agent reports steam_moving from it).
# Dot-sourced (tests), it only defines its functions; the caller loads mqtt_publish.ps1.
# ASCII only in this file (Windows PowerShell 5.1 reads .ps1 without BOM as ANSI).
$moveDotSourced = $MyInvocation.InvocationName -eq '.'
$ErrorActionPreference = 'Stop'

$seRoot        = Join-Path $env:LOCALAPPDATA 'SteamEngine'
$logFile       = Join-Path $PSScriptRoot 'move.log'
$installLock   = Join-Path $PSScriptRoot 'install.lock'
# The PID and app ID of the running move (Write-SteamInstallMarker), removed at its end
$moveLockFile  = Join-Path $PSScriptRoot 'move.lock'
# What a move did from the switch on, until it is finished or taken back (see Resume-InterruptedMove):
# one file per move, move_pending_<appid>_<UTC time of its switch>.json next to the jobs
$journalNameRe = '^move_pending_\d{1,10}_\d{14}\.json$'
# Test switch: this file next to the jobs pretends free space in GB (the same file as install.ps1 reads)
$spaceTestFile = Join-Path $PSScriptRoot 'space_test_free_gb.txt'
# robocopy writes its own log here (overwritten by every copy run)
$robocopyLog   = Join-Path $PSScriptRoot 'move_robocopy.log'
# The end state a move could not send (MQTT down): the next move.ps1 or report.ps1 sends it (retained)
$finalFile     = Join-Path $PSScriptRoot 'move_final.json'
$mqttClientId  = ($env:COMPUTERNAME.ToLower() + '-steam-move')
$mqttCredFile  = Join-Path $seRoot 'mqtt_credentials.txt'
# Waiting for the job lock while an uninstall or another move runs (an install job is answered at once)
$jobLockWaitSec = 180
# Progress on steam/move at most this often (also while Steam is closed or starting)
$progressEverySec = 5
# The copy is looked at this often (size of the staging folder, the game's state)
$copyCheckSec = 5
# The Steam client: asked to exit within this time, then waited for this much longer
$steamExitSec     = 90
$steamExitMoreSec = 60
# A pause after the client exited (it lets go of its files) and before it is started again
$settleSec = 3
# After the start: Steam must load the target library within $loadSec (it may update itself first),
# then show the game installed within $acceptSec
$loadSec = 180
$acceptSec = 90
# While Steam runs, an unreadable manifest stops the copy only after this many checks in a row (Steam
# rewrites manifests now and then)
$watchMisses = 3
# StateFlags must stay 4 this long after Steam loaded the library
$acceptStableSec = 5
# The staging folder: <target library>\steamapps\common\.se-move-<appid> (the workshop content in
# steamapps\workshop\content\.se-move-<appid>); on a drive without a library <letter>:\.se-move-<appid>
# (and ...-workshop), because the new library folder must be empty when New-SteamLibrary adds it
$stagingPrefix = '.se-move-'
# Names of staging folders: only these are deleted as leftovers of an interrupted move
$stagingNameRe     = '^\.se-move-\d{1,10}$'
$stagingRootNameRe = '^\.se-move-\d{1,10}(-workshop)?$'
$moveUnreadable = 'the manifest is gone or cannot be read'

function Write-Log([string]$message) { Write-SteamEngineLog $logFile $message }

# --- result and progress on steam/move -------------------------------------------------------------

$script:mqttReady = $false
$script:mqttCreds = $null
$script:mv = $null
$script:started = ''
$script:lastProgress = $null

# $true when the broker took the message. End states (ok, error) are retained.
function Send-MoveState($Body) {
    if (-not $script:mqttReady) { return $false }
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $Body -Compress -Depth 5))
        Publish-MqttRetained -BrokerHost $script:mqttHost -Port $script:mqttPort -ClientId $mqttClientId `
            -Username $script:mqttCreds.Username -Password $script:mqttCreds.Password -Topic $script:mqttTopic `
            -PayloadBytes $bytes -Qos 1 -Retain ($Body['state'] -ne 'running')
        return $true
    } catch {
        # Only the error message itself, never the CONNECT payload or credentials
        Write-Log "Warning: MQTT message on steam/move failed: $($_.Exception.Message)"
        return $false
    }
}

# The fields every message has: state, started (the job's start, identifies it), appid, name,
# from (the drive the game is on) and drive (the target drive), both like 'F:'
function New-MoveBody([string]$State) {
    $m = $script:mv
    return [ordered]@{
        state   = $State
        started = $script:started
        appid   = $AppId
        name    = $(if ($m -and $m.Name) { $m.Name } else { $null })
        from    = $(if ($m -and $m.SourceLetter) { $m.SourceLetter } else { $null })
        drive   = $(if ($Drive -match '^[A-Za-z]') { $Drive.Substring(0, 1).ToUpperInvariant() + ':' } else { $null })
    }
}

# state 'running' with phase, percent, copied_gb, total_gb, updated; at most every $progressEverySec
# unless -Force
function Send-MoveProgress([string]$Phase, [switch]$Force) {
    $m = $script:mv
    if (-not $m) { return }
    $m.Phase = $Phase
    if (-not $Force -and $script:lastProgress -and $script:lastProgress.Elapsed.TotalSeconds -lt $progressEverySec) { return }
    $script:lastProgress = [System.Diagnostics.Stopwatch]::StartNew()
    $body = New-MoveBody 'running'
    $body['phase'] = $Phase
    $body['percent'] = $m.Percent
    $body['copied_gb'] = [math]::Round($m.CopiedBytes / 1GB, 1)
    $body['total_gb'] = [math]::Round($m.TotalBytes / 1GB, 1)
    $body['updated'] = Format-SteamEngineUtc ([DateTime]::UtcNow)
    [void](Send-MoveState $body)
}

# The end state, tried for about 20 s (the result is retained, a later reconnect of HA still gets it).
# Not sent: kept in move_final.json as it would have gone out, marked late (it never answers a newer
# move command), for the next move.ps1 or report.ps1 (Home Assistant knows an end it had by started,
# state, reason_code and updated)
function Send-MoveEnd($Result) {
    if (-not $script:mqttReady) { return }
    $body = New-MoveBody $Result.State
    foreach ($k in @('reason_code', 'reason', 'warning', 'needed_gb', 'source_free_gb', 'target_free_gb', 'library', 'new_library')) {
        if ($Result.Contains($k)) { $body[$k] = $Result[$k] }
    }
    $body['updated'] = Format-SteamEngineUtc ([DateTime]::UtcNow)
    foreach ($pause in @(0, 5, 15)) {
        if ($pause) { Start-Sleep -Seconds $pause }
        if (Send-MoveState $body) { return }
    }
    $body['late'] = $true
    try {
        [System.IO.File]::WriteAllText($finalFile, (ConvertTo-Json -InputObject $body -Compress -Depth 5), (New-Object System.Text.UTF8Encoding $false))
        Write-Log "End state '$($body['state'])' not sent, kept for later ($finalFile)."
    } catch {
        Write-Log "Warning: end state '$($body['state'])' neither sent nor kept: $($_.Exception.Message)"
    }
}

# The end state an earlier move could not send (move_final.json), sent before anything of this job;
# kept when it cannot be sent now. Under the job lock, so no end of a newer move went out before it.
function Send-PendingMoveEnd {
    if (-not $script:mqttReady) { return }
    try {
        if (Send-SteamPendingEnd -Path $finalFile -BrokerHost $script:mqttHost -Port $script:mqttPort -ClientId $mqttClientId `
                -Username $script:mqttCreds.Username -Password $script:mqttCreds.Password -Topic $script:mqttTopic) {
            Write-Log 'End state of an earlier move sent late.'
        }
    } catch {
        Write-Log "Warning: end state of an earlier move not sent: $($_.Exception.Message)"
    }
}

# --- helpers ---------------------------------------------------------------------------------------

# Ends the move with a reason code; Invoke-Move catches it, takes back what was done and reports
function Stop-Move([string]$Code, [string]$Reason, $Extra = $null) {
    $ex = New-Object System.InvalidOperationException($Reason)
    $ex.Data['Code'] = $Code
    if ($Extra) { $ex.Data['Extra'] = $Extra }
    throw $ex
}

# Files and bytes under $Path, without following links. Links counts junctions and symbolic links
# (robocopy /XJ leaves them out, so a folder with links is not moved). Throws when a folder cannot be read.
# $OnWait runs every 500 entries (progress during long walks).
function Measure-SteamMoveFolder([string]$Path, [scriptblock]$OnWait = $null) {
    $r = [pscustomobject]@{ Files = [int64]0; Bytes = [int64]0; Links = 0 }
    if (-not [System.IO.Directory]::Exists($Path)) { return $r }
    $stack = New-Object System.Collections.Stack
    $stack.Push([System.IO.DirectoryInfo]::new($Path))
    $n = 0
    while ($stack.Count -gt 0) {
        $d = $stack.Pop()
        foreach ($e in $d.EnumerateFileSystemInfos()) {
            $n++
            if ($OnWait -and $n % 500 -eq 0) { & $OnWait }
            if (($e.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { $r.Links++; continue }
            if ($e -is [System.IO.DirectoryInfo]) { $stack.Push($e) } else { $r.Files++; $r.Bytes += $e.Length }
        }
    }
    return $r
}

# The bytes under $Path; 0 when it cannot be read right now (robocopy is writing)
function Get-SteamMoveBytes([string]$Path) {
    try { return (Measure-SteamMoveFolder $Path).Bytes } catch { return [int64]0 }
}

# Deletes the folder $Path without following links (Remove-SteamFolder, mqtt_publish.ps1); $true when
# it is gone, else a warning in the log. $OnWait runs every 500 entries (progress during long deletes).
function Remove-SteamMoveFolder([string]$Path, [scriptblock]$OnWait = $null) {
    $problem = Remove-SteamFolder $Path $OnWait
    if (-not $problem) { return $true }
    Write-Log "Warning: $Path not fully deleted: $problem"
    return $false
}

# StateFlags of a manifest, or -1 when it is missing or cannot be read
function Get-SteamMoveFlags([string]$Manifest) {
    $f = Get-AcfValue $Manifest 'StateFlags'
    if ($f -match '^\d+$') { return [int64]$f }
    return [int64]-1
}

# $true for StateFlags a game can be moved with: fully installed (4), maybe with an update Steam put
# off (6 = UpdateRequired | FullyInstalled); nothing else (running, queued, paused, validating, ...)
function Test-SteamMoveFlags([int64]$Flags) {
    if ($Flags -lt 0 -or ($Flags -band $SteamAppState.FullyInstalled) -eq 0) { return $false }
    $allowed = [int64]($SteamAppState.FullyInstalled -bor $SteamAppState.UpdateRequired)
    return (($Flags -band (-bnot $allowed)) -eq 0)
}

# Why Steam works on the game right now, or '': StateFlags other than 4 or 6 (Test-SteamMoveFlags),
# or files in steamapps\downloading\<appid> (an update in progress or paused)
function Get-SteamMoveBusyReason($m) {
    $flags = Get-SteamMoveFlags $m.SourceManifest
    if ($flags -eq -1) { return $moveUnreadable }
    if (-not (Test-SteamMoveFlags $flags)) { return "StateFlags=$flags" }
    $dl = [System.IO.Path]::Combine($m.SourceApps, 'downloading', $AppId)
    try {
        if ([System.IO.Directory]::Exists($dl) -and @([System.IO.Directory]::EnumerateFileSystemEntries($dl)).Count -gt 0) { return "files in $dl" }
    } catch { }
    return ''
}

# Starts robocopy for $Source -> $Target in the background (no window), mirroring: files only in
# $Target are deleted, so $Target must be a folder of this move. /XJ leaves links out, /R:2 /W:2 keeps
# retries short. Returns the process.
function Start-SteamMoveCopy([string]$Source, [string]$Target) {
    $argText = ('"{0}" "{1}" /MIR /COPY:DAT /DCOPY:T /XJ /R:2 /W:2 /NP /NFL /NDL /UNILOG:"{2}"' -f $Source, $Target, $robocopyLog)
    $p = Start-Process -FilePath 'robocopy.exe' -ArgumentList $argText -WindowStyle Hidden -PassThru
    # Keeps the handle: without it ExitCode can stay empty after the exit
    [void]$p.Handle
    return $p
}

# Copies $Source to $Target with robocopy and reports progress ($BaseBytes copied before this part).
# -Watch: Steam runs, so the copy stops when the game starts or Steam starts working on it.
function Invoke-SteamMoveCopy([string]$Source, [string]$Target, [int64]$BaseBytes, [string]$Phase, [switch]$Watch) {
    $m = $script:mv
    if (-not [System.IO.Directory]::Exists($Source)) { return }
    $p = Start-SteamMoveCopy $Source $Target
    $m.Process = $p
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $next = 0
    $misses = 0
    while (-not $p.WaitForExit(1000)) {
        if ($sw.Elapsed.TotalSeconds -lt $next) { continue }
        $next = $sw.Elapsed.TotalSeconds + $copyCheckSec
        if ($Watch) {
            $running = Get-SteamRunningAppId
            if ($running -eq [int64]$AppId) { Stop-Move 'game_running' 'the game was started during the copy' }
            $busy = Get-SteamMoveBusyReason $m
            # One unreadable manifest is Steam rewriting it; a few in a row count
            if ($busy -eq $moveUnreadable) {
                $misses++
                if ($misses -lt $watchMisses) { $busy = '' }
            } else { $misses = 0 }
            if ($busy) { Stop-Move 'steam_busy' "Steam started working on the game during the copy ($busy)" }
        }
        $m.CopiedBytes = $BaseBytes + (Get-SteamMoveBytes $Target)
        if ($m.TotalBytes -gt 0) { $m.Percent = [int][math]::Min(99, [math]::Floor($m.CopiedBytes * 100 / $m.TotalBytes)) }
        Send-MoveProgress $Phase
    }
    $p.WaitForExit()
    $code = $p.ExitCode
    $m.Process = $null
    # 0-7: copied, nothing to copy, extra files removed; 8 and more: files not copied or a fatal error
    if ($null -eq $code -or $code -ge 8) { Stop-Move 'copy_failed' "robocopy ended with exit code $code (see $robocopyLog)" }
}

# Asks the Steam client to exit (not while a game runs: Steam cannot exit then)
function Close-SteamForMove {
    $m = $script:mv
    $running = Get-SteamRunningAppId
    if ($running -ne 0) { Stop-Move 'game_running' "a game is running (appid=$running), the Steam client cannot be closed" }
    if (Test-SteamClientRunning) {
        Write-Log 'Stopping the Steam client.'
        # Set before asking: the client is started again at the end, also when it exits late
        $m.SteamStopped = $true
        if (-not (Stop-SteamClient $steamExitSec)) {
            Write-Log "The Steam client did not exit within $steamExitSec s, waiting $steamExitMoreSec s more."
            if (-not (Wait-SteamClientExit $steamExitMoreSec)) { Stop-Move 'steam_did_not_exit' "the Steam client did not exit within $($steamExitSec + $steamExitMoreSec) s" }
        }
        Start-Sleep -Seconds $settleSec
    } else {
        Write-Log 'The Steam client is not running; it is started after the move to check it.'
    }
}

# While the client is meant to be closed: nothing may have started it again (HA, the user, an
# autostart). Checked before a new library is added and before the game takes its place.
function Assert-SteamStillClosed {
    $m = $script:mv
    if (Test-SteamClientRunning) {
        # Not ours to start again at the end: it runs
        $m.SteamStopped = $false
        Stop-Move 'steam_busy' 'the Steam client was started again while it had to stay closed for the move'
    }
    $running = Get-SteamRunningAppId
    if ($running -ne 0) { Stop-Move 'game_running' "a game was started while the Steam client had to stay closed (appid=$running)" }
}

# The game folder moves as a whole (and is deleted afterwards): not when another app of the source
# library (e.g. an editor of the game) has it too (shared_folder), nor while that cannot be told
# because a manifest there cannot be read (steam_busy). $Then ends the reason.
function Assert-SteamMoveOwnFolder([string]$Then) {
    $m = $script:mv
    $other = Find-SteamSharedInstallDir $m.SourceApps $AppId $m.InstallDir
    if ($other -and $other.Unclear) { Stop-Move 'steam_busy' "$($other.Unclear), $Then" }
    if ($other) { Stop-Move 'shared_folder' "$($m.SourceGame) is also the install folder of $($other.Name) (appid=$($other.AppId)), $Then" }
}

# --- the steps -------------------------------------------------------------------------------------

function New-SteamMoveState {
    return [pscustomobject]@{
        Name = $null; InstallDir = $null; SourceLetter = $null; TargetLetter = $null
        SourceLib = $null; SourceApps = $null; SourceManifest = $null; SourceGame = $null
        SourceWorkshop = $null; SourceWorkshopAcf = $null
        TargetLib = $null; TargetApps = $null; TargetManifest = $null; TargetGame = $null
        TargetWorkshop = $null; TargetWorkshopAcf = $null
        StageGame = $null; StageWorkshop = $null
        NewLibrary = $false; Library = $null; LibraryState = ''; TargetLoaded = $false; JournalFile = $null
        GameBytes = [int64]0; WorkshopBytes = [int64]0; TotalBytes = [int64]0; CopiedBytes = [int64]0; Percent = 0; Phase = 'check'
        ManifestBytes = $null; WorkshopAcfBytes = $null
        Process = $null; SteamStopped = $false
        TargetPlaced = $false; WorkshopPlaced = $false; ManifestCopied = $false; WorkshopAcfCopied = $false
        SourceManifestRemoved = $false; SourceWorkshopAcfRemoved = $false
        KeepTarget = $false; RollbackFailed = $false
    }
}

# $true for an installdir that is a plain folder name (it is copied, renamed and deleted)
function Test-SteamMoveInstallDir([string]$Dir) {
    return (Test-SteamPlainFolderName $Dir) -and -not $Dir.StartsWith($stagingPrefix)
}

# All paths of a move from SourceLib, TargetLib, InstallDir and NewLibrary (also for a journal)
function Set-SteamMovePaths($m) {
    $m.SourceLetter = Get-SteamDriveLetter $m.SourceLib
    $m.TargetLetter = Get-SteamDriveLetter $m.TargetLib
    $m.SourceApps = [System.IO.Path]::Combine($m.SourceLib, 'steamapps')
    $m.SourceManifest = [System.IO.Path]::Combine($m.SourceApps, ('appmanifest_{0}.acf' -f $AppId))
    $m.SourceGame = [System.IO.Path]::Combine($m.SourceApps, 'common', $m.InstallDir)
    $m.TargetApps = [System.IO.Path]::Combine($m.TargetLib, 'steamapps')
    $m.TargetGame = [System.IO.Path]::Combine($m.TargetApps, 'common', $m.InstallDir)
    $m.TargetManifest = [System.IO.Path]::Combine($m.TargetApps, ('appmanifest_{0}.acf' -f $AppId))
    $m.SourceWorkshop = [System.IO.Path]::Combine($m.SourceApps, 'workshop', 'content', $AppId)
    $m.SourceWorkshopAcf = [System.IO.Path]::Combine($m.SourceApps, 'workshop', ('appworkshop_{0}.acf' -f $AppId))
    $m.TargetWorkshop = [System.IO.Path]::Combine($m.TargetApps, 'workshop', 'content', $AppId)
    $m.TargetWorkshopAcf = [System.IO.Path]::Combine($m.TargetApps, 'workshop', ('appworkshop_{0}.acf' -f $AppId))
    if ($m.NewLibrary) {
        $m.StageGame = $m.TargetLetter + '\' + $stagingPrefix + $AppId
        $m.StageWorkshop = $m.StageGame + '-workshop'
    } else {
        $m.StageGame = [System.IO.Path]::Combine($m.TargetApps, 'common', $stagingPrefix + $AppId)
        $m.StageWorkshop = [System.IO.Path]::Combine($m.TargetApps, 'workshop', 'content', $stagingPrefix + $AppId)
    }
}

# The drive letters whose root may hold a staging folder of a move to a new library: every fixed, ready
# drive (also one that has a library by now) and the drive of every ready library of $LibInfos. Other
# drives are only asked for their type, so no network or removable drive is waited for.
function Get-SteamMoveRootDrives($LibInfos) {
    $out = New-Object System.Collections.ArrayList
    $all = @()
    try { $all = @([System.IO.DriveInfo]::GetDrives()) } catch { }
    foreach ($d in $all) {
        # Every property can throw (a drive going away, a device error)
        try {
            if ($d.DriveType -ne [System.IO.DriveType]::Fixed -or -not $d.IsReady) { continue }
            $l = Get-SteamDriveLetter $d.Name
            if ($l -and -not $out.Contains($l)) { [void]$out.Add($l) }
        } catch { }
    }
    foreach ($li in @($LibInfos)) {
        if (-not $li.Ready) { continue }
        $l = Get-SteamDriveLetter $li.Path
        if ($l -and -not $out.Contains($l)) { [void]$out.Add($l) }
    }
    return @($out.ToArray())
}

# Staging folders an interrupted move left behind, except $Keep: in steamapps\common and
# steamapps\workshop\content of every library there, and on the root of the drives. Only exact
# staging names; nothing else is listed.
function Get-SteamMoveStaleFolders($LibInfos, [string[]]$Keep) {
    $out = New-Object System.Collections.ArrayList
    $look = {
        param([string]$Dir, [string]$Re)
        try {
            if (-not [System.IO.Directory]::Exists($Dir)) { return }
            foreach ($d in [System.IO.Directory]::EnumerateDirectories($Dir, '.se-move-*')) {
                $leaf = [System.IO.Path]::GetFileName($d)
                if ($leaf -notmatch $Re) { continue }
                if (@($Keep | Where-Object { $_ -and $_ -ieq $d }).Count -gt 0) { continue }
                [void]$out.Add($d)
            }
        } catch { }
    }
    foreach ($li in @($LibInfos)) {
        if (-not $li.Ready) { continue }
        & $look ([System.IO.Path]::Combine($li.Path, 'steamapps', 'common')) $stagingNameRe
        & $look ([System.IO.Path]::Combine($li.Path, 'steamapps', 'workshop', 'content')) $stagingNameRe
    }
    foreach ($letter in @(Get-SteamMoveRootDrives $LibInfos)) { & $look ($letter + '\') $stagingRootNameRe }
    return @($out | Select-Object -Unique)
}

# Checks everything that can be checked before copying and returns what the move needs
function Resolve-Move {
    $m = New-SteamMoveState
    $script:mv = $m
    $m.TargetLetter = $Drive.Substring(0, 1).ToUpperInvariant() + ':'

    $libInfos = $null
    try { $libInfos = @(Get-SteamLibraryInfo $seCfg.SteamRoot) } catch { Stop-Move 'library_unreadable' "libraryfolders.vdf cannot be read: $($_.Exception.Message)" }
    $where = Find-SteamGameLibrary $libInfos $AppId
    if (-not $where.Manifest) {
        if ($where.OfflineLibrary) {
            $m.SourceLetter = Get-SteamDriveLetter $where.OfflineLibrary
            Stop-Move 'drive_unavailable' "the drive of the game's library $($where.OfflineLibrary) is not available"
        }
        Stop-Move 'not_installed' 'the game is not installed'
    }
    $m.SourceLib = $where.Library
    $m.SourceManifest = $where.Manifest
    $m.SourceLetter = Get-SteamDriveLetter $m.SourceLib
    $m.SourceApps = [System.IO.Path]::Combine($m.SourceLib, 'steamapps')
    $c = Read-SharedTextFile $m.SourceManifest
    if (-not $c) { Stop-Move 'not_installed' 'the manifest cannot be read' }
    $m.Name = Get-AcfValue -Key 'name' -Text $c
    if (-not $m.Name) { $m.Name = $AppId }
    $m.InstallDir = [string](Get-AcfValue -Key 'installdir' -Text $c)
    $sizeText = Get-AcfValue -Key 'SizeOnDisk' -Text $c
    # installdir comes from the manifest: only a plain folder name is copied, renamed and deleted
    if (-not (Test-SteamMoveInstallDir $m.InstallDir)) {
        Stop-Move 'unexpected_folder' "unexpected install folder '$($m.InstallDir)'"
    }
    $m.SourceGame = [System.IO.Path]::Combine($m.SourceApps, 'common', $m.InstallDir)
    if (-not [System.IO.Directory]::Exists($m.SourceGame)) { Stop-Move 'not_installed' "the game folder $($m.SourceGame) is missing" }
    # A game or workshop folder that is a link would be copied from where it points to, and only the
    # link deleted afterwards: the files there would stay
    foreach ($p in @($m.SourceGame, [System.IO.Path]::Combine($m.SourceApps, 'workshop', 'content', $AppId))) {
        if (Test-SteamLink $p) { Stop-Move 'unexpected_folder' "$p is a junction or symbolic link, it is not moved" }
    }
    Assert-SteamMoveOwnFolder 'it is not moved'
    $busy = Get-SteamMoveBusyReason $m
    if ($busy) { Stop-Move 'steam_busy' "Steam works on the game ($busy)" }
    $running = Get-SteamRunningAppId
    if ($running -ne 0) { Stop-Move 'game_running' "a game is running (appid=$running), the Steam client cannot be closed for the move" }
    $why = Get-SteamGameRunningReason $AppId $m.SourceGame
    if ($why) { Stop-Move 'game_running' "$why, the game is not moved" }

    # The target: never the game's own drive, never a path, only the library there (or a new one)
    if ($m.TargetLetter -eq $m.SourceLetter) { Stop-Move 'same_drive' "the game is on $($m.SourceLetter) already" }
    $ready = $false
    try { $ready = ([System.IO.DriveInfo]::new($m.TargetLetter + '\')).IsReady } catch { }
    if (-not $ready) { Stop-Move 'drive_unavailable' "the target drive $($m.TargetLetter) is not available" }
    $defaultPath = ''
    try {
        $d = Get-SteamDefaultLibrary $seCfg.SteamRoot $libInfos
        if ($d.FromStar -and $d.Library) { $defaultPath = $d.Library.Path }
    } catch { }
    $lib = Get-SteamDriveLibrary $libInfos $m.TargetLetter $defaultPath
    if ($lib) {
        $m.TargetLib = $lib.Path
    } else {
        $dead = @($libInfos | Where-Object { (Get-SteamDriveLetter $_.Path) -eq $m.TargetLetter }) | Select-Object -First 1
        if ($dead) { Stop-Move 'drive_not_suitable' "the library $($dead.Path) on $($m.TargetLetter) has no steamapps folder (see libraryfolders.vdf)" }
        $m.TargetLib = $m.TargetLetter + '\SteamLibrary'
        $problem = Get-SteamLibraryFolderProblem $m.TargetLib
        if ($problem) { Stop-Move 'drive_not_suitable' "the drive $($m.TargetLetter) cannot take a Steam library: $problem" }
        if (-not (@(Get-SteamCandidateDrives 0) | Where-Object { $_.Drive -eq $m.TargetLetter })) {
            Stop-Move 'drive_not_suitable' "the drive $($m.TargetLetter) cannot take a Steam library: it must be a fixed, ready NTFS drive"
        }
        $m.NewLibrary = $true
    }
    Set-SteamMovePaths $m
    foreach ($p in @($m.TargetGame, $m.TargetWorkshop)) {
        if ([System.IO.Directory]::Exists($p) -or [System.IO.File]::Exists($p)) { Stop-Move 'target_exists' "$p exists already" }
    }
    foreach ($p in @($m.TargetManifest, $m.TargetWorkshopAcf)) {
        if ([System.IO.File]::Exists($p) -or [System.IO.Directory]::Exists($p)) { Stop-Move 'target_exists' "$p exists already" }
    }
    # robocopy /MIR deletes in its target what the source lacks: no folder on the way to the staging
    # folder may lead somewhere else
    foreach ($p in @($m.TargetLib, $m.TargetApps, [System.IO.Path]::Combine($m.TargetApps, 'common'), [System.IO.Path]::Combine($m.TargetApps, 'workshop'), [System.IO.Path]::Combine($m.TargetApps, 'workshop', 'content'))) {
        if (Test-SteamLink $p) { Stop-Move 'unexpected_folder' "$p is a junction or symbolic link" }
    }

    # Leftovers of interrupted moves: staging folders of other moves go; this move's own staging
    # folder is kept (mirrored again, its bytes need no new space), unless it is a link (only the
    # link itself is removed) or workshop content the game no longer has
    foreach ($p in @(Get-SteamMoveStaleFolders $libInfos @($m.StageGame, $m.StageWorkshop))) {
        if (Remove-SteamMoveFolder $p) { Write-Log "Old staging folder $p removed." }
    }
    foreach ($p in @($m.StageGame, $m.StageWorkshop)) {
        # only a staging folder that is there (most games have no workshop content at all)
        if ((Test-SteamLink $p) -or ($p -eq $m.StageWorkshop -and -not [System.IO.Directory]::Exists($m.SourceWorkshop) -and [System.IO.Directory]::Exists($p))) {
            if (-not (Remove-SteamMoveFolder $p)) { Stop-Move 'unexpected_folder' "the old staging folder $p cannot be removed" }
            Write-Log "Old staging folder $p removed."
        } elseif ([System.IO.File]::Exists($p)) {
            Stop-Move 'unexpected_folder' "$p is a file"
        }
    }

    # Size, as Home Assistant works it out from steam/library (size_mb + workshop_mb, see PROTOCOL.md),
    # but each part at least what the folder holds (the whole folder is copied, also files Steam does
    # not count, e.g. mods): the larger of SizeOnDisk of the appmanifest and the game folder, plus the
    # larger of SizeOnDisk of appworkshop_<appid>.acf and the workshop folder, plus the reserve of an
    # install
    $game = $null; $ws = $null
    try {
        $game = Measure-SteamMoveFolder $m.SourceGame
        $ws = Measure-SteamMoveFolder $m.SourceWorkshop
    } catch { Stop-Move 'copy_failed' "the game folder cannot be read: $($_.Exception.Message)" }
    if ($game.Links -gt 0 -or $ws.Links -gt 0) { Stop-Move 'copy_failed' 'the game folder holds links (junctions or symbolic links), it is not moved' }
    $m.GameBytes = $game.Bytes
    $m.WorkshopBytes = $ws.Bytes
    $m.TotalBytes = $m.GameBytes + $m.WorkshopBytes
    $size = $m.GameBytes
    if ($sizeText -match '^\d+$' -and [int64]$sizeText -gt $size) { $size = [int64]$sizeText }
    $wsSize = $m.WorkshopBytes
    if ([System.IO.File]::Exists($m.SourceWorkshopAcf)) {
        $wsText = Get-AcfValue $m.SourceWorkshopAcf 'SizeOnDisk'
        if ($wsText -match '^\d+$' -and [int64]$wsText -gt $wsSize) { $wsSize = [int64]$wsText }
    }
    $staged = (Get-SteamMoveBytes $m.StageGame) + (Get-SteamMoveBytes $m.StageWorkshop)
    $needed = [math]::Max([int64]0, $size + $wsSize + (Get-SteamSpaceReserve $size) - $staged)
    $free = Get-SteamFreeBytes $spaceTestFile ($m.TargetLetter + '\')
    $test = if ($null -ne (Get-SteamTestFreeGb $spaceTestFile ($m.TargetLetter + '\'))) { ' (TEST VALUE from space_test_free_gb.txt)' } else { '' }
    $neededGb = [math]::Round($needed / 1GB, 1)
    $freeGb = [math]::Round($free / 1GB, 1)
    Write-Log ("Space appid={0}: game {1:N1} GB, workshop {2:N1} GB + reserve {3:N1} GB - already copied {4:N1} GB = {5} GB, free on {6} {7} GB{8}." -f $AppId, ($size / 1GB), ($wsSize / 1GB), ((Get-SteamSpaceReserve $size) / 1GB), ($staged / 1GB), $neededGb, $m.TargetLetter, $freeGb, $test)
    if ($free -lt $needed) { Stop-Move 'no_space' "not enough space on $($m.TargetLetter)" ([ordered]@{ needed_gb = $neededGb; target_free_gb = $freeGb }) }
    Write-Log ("Moving appid={0} ({1}) from {2} to {3}{4}: {5} files, {6:N1} GB." -f $AppId, $m.Name, $m.SourceGame, $m.TargetGame, $(if ($m.NewLibrary) { ' (new library)' } else { '' }), $game.Files, ($m.TotalBytes / 1GB))
    return $m
}

# The copy while Steam runs: most of the data, the client is closed only for the rest
function Copy-MoveWhileSteamRuns {
    $m = $script:mv
    Send-MoveProgress 'copy' -Force
    Invoke-SteamMoveCopy $m.SourceGame $m.StageGame 0 'copy' -Watch
    Invoke-SteamMoveCopy $m.SourceWorkshop $m.StageWorkshop $m.GameBytes 'copy' -Watch
    Write-Log 'First copy done.'
}

# With the client closed: the rest, the comparison, a new library, then the game takes its place
function Complete-MoveWhileSteamClosed {
    $m = $script:mv
    $busy = Get-SteamMoveBusyReason $m
    if ($busy) { Stop-Move 'steam_busy' "Steam worked on the game ($busy)" }
    Send-MoveProgress 'sync' -Force
    Invoke-SteamMoveCopy $m.SourceGame $m.StageGame 0 'sync'
    Invoke-SteamMoveCopy $m.SourceWorkshop $m.StageWorkshop $m.GameBytes 'sync'
    foreach ($pair in @(@($m.SourceGame, $m.StageGame), @($m.SourceWorkshop, $m.StageWorkshop))) {
        if (-not [System.IO.Directory]::Exists($pair[0])) { continue }
        try {
            $a = Measure-SteamMoveFolder $pair[0] { Send-MoveProgress 'sync' }
            $b = Measure-SteamMoveFolder $pair[1] { Send-MoveProgress 'sync' }
        } catch { Stop-Move 'copy_failed' "the copy cannot be compared: $($_.Exception.Message)" }
        Write-Log ("Compared {0}: {1} files {2} bytes, copy {3} files {4} bytes." -f $pair[0], $a.Files, $a.Bytes, $b.Files, $b.Bytes)
        if ($a.Files -ne $b.Files -or $a.Bytes -ne $b.Bytes -or $b.Links -gt 0) { Stop-Move 'copy_failed' "the copy of $($pair[0]) is not complete" }
    }
    $m.CopiedBytes = $m.TotalBytes
    $m.Percent = 100
    Send-MoveProgress 'switch' -Force

    if ($m.NewLibrary) {
        Assert-SteamStillClosed
        try {
            $m.Library = New-SteamLibrary $seCfg.SteamRoot $m.TargetLetter
        } catch {
            $code = if ($_.Exception.Data['Code']) { [string]$_.Exception.Data['Code'] } else { 'library_not_created' }
            Stop-Move $code "no new Steam library on $($m.TargetLetter): $($_.Exception.Message)"
        }
        Write-Log "New Steam library $($m.Library.Path) added (block $($m.Library.Index), libraryfolders.vdf backed up)."
    }
    # The manifest as it is now, to put back on a failure (Steam may change the copy after its start)
    $m.ManifestBytes = [System.IO.File]::ReadAllBytes($m.SourceManifest)
    if ([System.IO.File]::Exists($m.SourceWorkshopAcf)) { $m.WorkshopAcfBytes = [System.IO.File]::ReadAllBytes($m.SourceWorkshopAcf) }
    Assert-SteamStillClosed
    # Nothing may have started from the game folder or taken it meanwhile (the source is deleted after
    # the switch)
    $why = Get-SteamGameRunningReason $AppId $m.SourceGame
    if ($why) { Stop-Move 'game_running' "$why, nothing is switched" }
    Assert-SteamMoveOwnFolder 'nothing is switched'
    # From here on a cut-off move is finished or taken back by the next start (Resume-InterruptedMove)
    try { Write-MoveJournal 'switch' } catch { Stop-Move 'unexpected_error' "the move journal $($m.JournalFile) cannot be written: $($_.Exception.Message)" }
    try {
        # The manifests first: a game folder at the target is this move's copy only while this move's
        # manifest is there (Resume-MoveFromJournal)
        [System.IO.File]::WriteAllBytes($m.TargetManifest, $m.ManifestBytes)
        $m.ManifestCopied = $true
        if ($null -ne $m.WorkshopAcfBytes) {
            [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($m.TargetWorkshopAcf))
            [System.IO.File]::WriteAllBytes($m.TargetWorkshopAcf, $m.WorkshopAcfBytes)
            $m.WorkshopAcfCopied = $true
        }
        [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($m.TargetGame))
        [System.IO.Directory]::Move($m.StageGame, $m.TargetGame)
        $m.TargetPlaced = $true
        if ([System.IO.Directory]::Exists($m.SourceWorkshop) -and [System.IO.Directory]::Exists($m.StageWorkshop)) {
            [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($m.TargetWorkshop))
            [System.IO.Directory]::Move($m.StageWorkshop, $m.TargetWorkshop)
            $m.WorkshopPlaced = $true
        }
        # The source manifest last: until here Steam would still find the game where it was
        [System.IO.File]::Delete($m.SourceManifest)
        $m.SourceManifestRemoved = $true
        if ($null -ne $m.WorkshopAcfBytes) {
            [System.IO.File]::Delete($m.SourceWorkshopAcf)
            $m.SourceWorkshopAcfRemoved = $true
        }
    } catch {
        Stop-Move 'copy_failed' "the game could not take its place in $($m.TargetLib): $($_.Exception.Message)"
    }
    Write-Log "Folder renamed to $($m.TargetGame), manifest moved to $($m.TargetApps)."
}

# Starts the client and waits until Steam loaded the target library and shows the game installed
function Start-SteamAndVerify {
    $m = $script:mv
    $offset = Get-SteamContentLogOffset $seCfg.SteamRoot
    Start-Sleep -Seconds $settleSec
    if (-not (Start-SteamClient -WaitExitSec 60)) { Stop-Move 'not_accepted' 'the Steam client could not be started (an old one still runs)' }
    $m.SteamStopped = $false
    Write-Log 'Steam client started, waiting for it to load the game.'
    Send-MoveProgress 'verify' -Force
    $loaded = $false
    if ($m.Library) {
        $acc = Wait-SteamLibraryAccepted $seCfg.SteamRoot $m.Library $offset $loadSec { Send-MoveProgress 'verify' }
        if ($acc.Note) { Write-Log $acc.Note }
        $m.LibraryState = $acc.State
        if ($acc.State -eq 'lost') { Stop-Move 'library_config_lost' 'Steam dropped libraries from before after the new library was added' }
        if ($acc.State -ne 'ok') { Stop-Move 'library_not_created' "Steam did not load the new library $($m.Library.Path)" }
        Remove-OldSteamLibraryBackups $m.Library
        $loaded = $true
        $m.TargetLoaded = $true
    }
    $log = [System.IO.Path]::Combine($seCfg.SteamRoot, 'logs', 'content_log.txt')
    $loadedRe = '(?i)Loaded \d+ apps from install folder "' + [regex]::Escape($m.TargetApps + '\')
    # $loadSec until Steam loaded the library (it may update itself first), then $acceptSec for the game
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $loadedAt = if ($loaded) { [System.Diagnostics.Stopwatch]::StartNew() } else { $null }
    $since = $null
    $flags = [int64]-1
    while ($true) {
        if (-not $loaded -and $sw.Elapsed.TotalSeconds -ge $loadSec) { break }
        if ($loaded -and $loadedAt.Elapsed.TotalSeconds -ge $acceptSec) { break }
        Start-Sleep -Seconds 1
        Send-MoveProgress 'verify'
        if (-not $loaded) {
            foreach ($line in (Read-SteamLogLines $log ([ref]$offset))) { if ($line -match $loadedRe) { $loaded = $true } }
            if (-not $loaded) { continue }
            $loadedAt = [System.Diagnostics.Stopwatch]::StartNew()
            $m.TargetLoaded = $true
        }
        $flags = Get-SteamMoveFlags $m.TargetManifest
        if (-not (Test-SteamMoveFlags $flags) -or [System.IO.File]::Exists($m.SourceManifest)) { $since = $null; continue }
        if (-not $since) { $since = [System.Diagnostics.Stopwatch]::StartNew() }
        if ($since.Elapsed.TotalSeconds -ge $acceptStableSec) {
            Write-Log "Steam loaded $($m.TargetApps) and shows the game installed (StateFlags $flags)."
            return
        }
    }
    if (-not $loaded) { Stop-Move 'not_accepted' "Steam did not load the library $($m.TargetLib) within $loadSec s" }
    Stop-Move 'not_accepted' "Steam does not show the game installed in $($m.TargetLib) (StateFlags=$flags)"
}

# The source goes only now; a failure here keeps the move (ok with a warning)
function Remove-MoveSource {
    $m = $script:mv
    Send-MoveProgress 'cleanup' -Force
    # A cut-off cleanup is only finished later, without a new message
    try { Write-MoveJournal 'cleanup' } catch { Write-Log "Warning: the move journal could not be updated: $($_.Exception.Message)" }
    $warning = $null
    $reason = $null
    $left = @(Remove-MoveSourceFolders $m)
    if ($left.Count -gt 0) {
        $warning = 'source_not_deleted'
        $reason = "moved, but not fully deleted on $($m.SourceLetter): $($left -join ', ')"
        Write-Log "Warning: $reason (tried again at the next start of the agent or a move)"
    } else {
        Remove-MoveJournal
    }
    $r = [ordered]@{ State = 'ok'; reason_code = $null; reason = $reason; warning = $warning }
    $r['source_free_gb'] = Get-SteamMoveFreeGb $m.SourceLetter
    $r['target_free_gb'] = Get-SteamMoveFreeGb $m.TargetLetter
    $r['library'] = $m.TargetLib
    $r['new_library'] = [bool]$m.Library
    Write-Log ("OK: appid={0} ({1}) moved to {2}; free on {3} {4} GB, on {5} {6} GB." -f $AppId, $m.Name, $m.TargetGame, $m.SourceLetter, $r['source_free_gb'], $m.TargetLetter, $r['target_free_gb'])
    return $r
}

# Deletes what is left of the game in the source library once the game lives in the target: its
# folder, its workshop content and appworkshop_<appid>.acf (only when the target has them, or with
# -Orphaned: the source library has the game no more, so they belong to nothing) and its shader cache
# (Steam builds it again). Only the game's own folders, never a library or common itself. The game
# folder stays when another app of the source library has it as its install folder (it is that app's
# now). Nothing is deleted while a program runs from the game folder, or while it cannot be told
# whether another app has it (Find-SteamSharedInstallDir): that is tried again later.
# Returns what could not be deleted.
function Remove-MoveSourceFolders($m, [scriptblock]$OnWait = { Send-MoveProgress 'cleanup' }, [switch]$Orphaned) {
    $other = Find-SteamSharedInstallDir $m.SourceApps $AppId $m.InstallDir
    if ($other -and $other.Unclear) { return @("$($m.SourceGame) ($($other.Unclear))") }
    if ([System.IO.Directory]::Exists($m.SourceGame)) {
        $p = $null
        try { $p = @(Get-SteamFolderProcesses @($m.SourceGame)) | Select-Object -First 1 } catch { return @("$($m.SourceGame) (the programs running cannot be listed: $($_.Exception.Message))") }
        if ($p) { return @("$($m.SourceGame) ($($p.Name) (PID $($p.Id)) runs from it)") }
    }
    $left = @()
    $paths = @()
    if ($other) { Write-Log "$($m.SourceGame) stays: it is the install folder of $($other.Name) (appid=$($other.AppId)) too." } else { $paths += $m.SourceGame }
    if ($Orphaned -or [System.IO.Directory]::Exists($m.TargetWorkshop)) { $paths += $m.SourceWorkshop }
    foreach ($p in $paths) {
        if (-not $p.StartsWith($m.SourceApps + '\', [System.StringComparison]::OrdinalIgnoreCase)) { $left += $p; continue }
        if (-not (Remove-SteamMoveFolder $p $OnWait)) { $left += $p }
    }
    if (($Orphaned -or [System.IO.File]::Exists($m.TargetWorkshopAcf)) -and [System.IO.File]::Exists($m.SourceWorkshopAcf)) {
        try { [System.IO.File]::Delete($m.SourceWorkshopAcf) } catch { $left += $m.SourceWorkshopAcf }
    }
    [void](Remove-SteamMoveFolder ([System.IO.Path]::Combine($m.SourceApps, 'shadercache', $AppId)) $OnWait)
    return $left
}

# --- the journal: a move cut off after the switch ----------------------------------------------------

# Writes what the running move needs to be finished or taken back later to its own journal file, named
# at the first write; phase 'switch' from the switch on, 'undo' once a take-back put the source
# manifest back (what is left at the target is only this move's copy), 'cleanup' once Steam took the
# game at the target. library_state: what Steam made of a new library (Wait-SteamLibraryAccepted,
# '' before the start). Written to a .tmp file first.
function Write-MoveJournal([string]$Phase) {
    $m = $script:mv
    if (-not $m.JournalFile) {
        $stamp = [DateTime]::UtcNow.ToString('yyyyMMddHHmmss', [System.Globalization.CultureInfo]::InvariantCulture)
        $m.JournalFile = [System.IO.Path]::Combine($PSScriptRoot, ('move_pending_{0}_{1}.json' -f $AppId, $stamp))
    }
    $j = [ordered]@{
        version       = 1
        phase         = $Phase
        started       = $script:started
        appid         = [string]$AppId
        name          = $m.Name
        installdir    = $m.InstallDir
        source_lib    = $m.SourceLib
        target_lib    = $m.TargetLib
        new_library   = [bool]$m.NewLibrary
        library_state = [string]$m.LibraryState
        manifest      = [Convert]::ToBase64String($m.ManifestBytes)
        workshop_acf  = $(if ($null -ne $m.WorkshopAcfBytes) { [Convert]::ToBase64String($m.WorkshopAcfBytes) } else { $null })
    }
    $tmp = $m.JournalFile + '.tmp'
    [System.IO.File]::WriteAllText($tmp, (ConvertTo-Json -InputObject $j -Depth 3), (New-Object System.Text.UTF8Encoding $false))
    if ([System.IO.File]::Exists($m.JournalFile)) { [System.IO.File]::Replace($tmp, $m.JournalFile, [NullString]::Value) }
    else { [System.IO.File]::Move($tmp, $m.JournalFile) }
}

# Removes the journal of the move in $script:mv (only one it wrote or was resumed from)
function Remove-MoveJournal {
    $f = if ($script:mv) { $script:mv.JournalFile } else { $null }
    if (-not $f) { return }
    try {
        if ([System.IO.File]::Exists($f)) { [System.IO.File]::Delete($f) }
    } catch { Write-Log "Warning: the move journal $f could not be deleted: $($_.Exception.Message)" }
}

# The journal files next to the jobs
function Get-MoveJournalFiles {
    $files = @()
    try { $files = @([System.IO.Directory]::GetFiles($PSScriptRoot, 'move_pending_*.json')) } catch { }
    return @($files | Where-Object { [System.IO.Path]::GetFileName($_) -match $journalNameRe } | Sort-Object)
}

# Deals with every journal (Resume-MoveJournal). Returns the result of the first one that keeps new
# moves out, else $null: a move in phase 'switch' that can be neither finished nor taken back yet. A
# journal in phase 'cleanup' or 'undo' only names the rest of a move whose library is decided (target
# or source) and never keeps them out.
function Resume-InterruptedMove {
    $open = $null
    foreach ($f in @(Get-MoveJournalFiles)) {
        $r = Resume-MoveJournal $f
        if (-not $open -and $r -and -not $r.Done -and $r.Phase -eq 'switch') { $open = $r }
    }
    return $open
}

# Finishes or takes back the move of the journal $File, cut off after the switch (PC off, job killed, a
# rollback that could not be done). $null for a journal that is not usable (it is removed); else Done
# ($false: the journal stays and the next start tries again), Phase, Code and Reason.
# Runs under the job lock; $AppId, $Drive, $script:mv and $script:started are the journal's meanwhile.
function Resume-MoveJournal([string]$File) {
    $j = $null
    try { $j = (Read-SharedTextFile $File) | ConvertFrom-Json } catch { $j = $null }
    if (-not $j -or [string]$j.phase -notmatch '^(switch|undo|cleanup)$' -or [string]$j.appid -notmatch '^\d{1,10}$' -or -not (Test-SteamMoveInstallDir ([string]$j.installdir)) -or
        -not ([string]$j.source_lib -match '^[A-Za-z]:\\') -or -not ([string]$j.target_lib -match '^[A-Za-z]:\\') -or -not $j.manifest) {
        Write-Log "Warning: the move journal $File is not usable, removed."
        try { [System.IO.File]::Delete($File) } catch { }
        return $null
    }
    $keep = @{ AppId = $script:AppId; Drive = $script:Drive; Mv = $script:mv; Started = $script:started }
    $script:AppId = [string]$j.appid
    $script:started = [string]$j.started
    $m = New-SteamMoveState
    $m.Name = [string]$j.name
    $m.InstallDir = [string]$j.installdir
    $m.SourceLib = [string]$j.source_lib
    $m.TargetLib = [string]$j.target_lib
    $m.NewLibrary = [bool]$j.new_library
    $m.LibraryState = [string]$j.library_state
    $m.JournalFile = $File
    Set-SteamMovePaths $m
    $script:Drive = $m.TargetLetter
    $script:mv = $m
    try {
        $m.ManifestBytes = [Convert]::FromBase64String([string]$j.manifest)
        if ($j.workshop_acf) { $m.WorkshopAcfBytes = [Convert]::FromBase64String([string]$j.workshop_acf) }
        Write-Log "Interrupted move found: appid=$($script:AppId) ($($m.Name)) from $($m.SourceGame) to $($m.TargetGame), phase $($j.phase)."
        $r = Resume-MoveFromJournal $m ([string]$j.phase)
        if ($r.Done) { Remove-MoveJournal } else { Write-Log "The interrupted move stays to do ($($r.Code): $($r.Reason))." }
        return $r
    } finally {
        $script:AppId = $keep.AppId; $script:Drive = $keep.Drive; $script:mv = $keep.Mv; $script:started = $keep.Started
    }
}

# The game lives where its manifest is:
# - phase 'cleanup' (Steam had taken the game at the target): never taken back. The rest of the source
#   goes (Remove-MoveSourceFolders), also when the game is gone from the target since, unless the game
#   is installed in the source library again. Only the source drive is needed.
# - phase 'switch', the target has the manifest and the game folder, the source no manifest, and Steam
#   lists the target library: the move counts, the rest of the source is deleted (end state 'ok'; a
#   rest that cannot be deleted gives warning source_not_deleted and turns the journal to phase
#   'cleanup'). Nothing is done while libraryfolders.vdf waits to be put back by hand after a new
#   library Steam did not take (Set-SteamLibraryRestorePending).
# - phase 'switch', neither library has the manifest (a move never leaves it so) and the target library
#   is there: the game was uninstalled meanwhile; nothing is written back or deleted (end state 'error'
#   with not_installed, warning source_not_deleted while the old folder stays).
# - else it is taken back like Undo-Move (the client closed while Steam could see the game in the
#   target): the source manifest is written back and the journal turns to phase 'undo', the target
#   manifest goes, then the copies at the target; end state 'error' with interrupted.
# - phase 'undo' (the source manifest is back): the rest of this move at the target goes, no end state;
#   nothing goes when the game is in the target and not in the source (installed or moved there since).
# The game folder at the target is this move's copy only while this move's manifest is there or in
# phase 'undo' (else Steam may have left it after an uninstall, with files made since); it is never
# deleted while it is the only copy of a game installed in the source.
function Resume-MoveFromJournal($m, [string]$Phase) {
    $pending = { param($c, $t) return [pscustomobject]@{ Done = $false; Phase = $Phase; Code = $c; Reason = $t } }
    $finished = [pscustomobject]@{ Done = $true; Phase = $Phase; Code = $null; Reason = $null }
    $letters = if ($Phase -eq 'cleanup') { @($m.SourceLetter) } else { @($m.SourceLetter, $m.TargetLetter) }
    foreach ($l in $letters) {
        $ready = $false
        try { $ready = ([System.IO.DriveInfo]::new($l + '\')).IsReady } catch { }
        if (-not $ready) { return (& $pending 'drive_unavailable' "drive $l is not available") }
    }
    $srcMan = [System.IO.File]::Exists($m.SourceManifest)
    $tgtMan = [System.IO.File]::Exists($m.TargetManifest)

    if ($Phase -eq 'cleanup') {
        if ($srcMan) {
            Write-Log "Rest of the move not deleted: the game is installed in $($m.SourceLib) again."
            return $finished
        }
        $left = @(Remove-MoveSourceFolders $m $null -Orphaned)
        if ($left.Count -gt 0) { return (& $pending 'source_not_deleted' "not fully deleted: $($left -join ', ')") }
        Write-Log "Rest of the move deleted from $($m.SourceLib)."
        return $finished
    }

    if ($Phase -eq 'switch' -and -not $srcMan -and $tgtMan -and [System.IO.Directory]::Exists($m.TargetGame)) {
        # The game lives in the target only when Steam lists that library: a new library Steam did not
        # take may wait for libraryfolders.vdf to be put back by hand, or be gone from it after that
        $marker = Get-SteamLibraryRestorePendingFile $seCfg.SteamRoot
        if ($m.NewLibrary -and $m.LibraryState -ne 'ok' -and [System.IO.File]::Exists($marker)) {
            return (& $pending 'library_not_restored' "libraryfolders.vdf waits to be put back by hand ($marker)")
        }
        $listed = $false
        try { $listed = @(Get-SteamLibraryInfo $seCfg.SteamRoot | Where-Object { $_.Path -ieq $m.TargetLib.TrimEnd('\') }).Count -gt 0 } catch {
            return (& $pending 'library_unreadable' "libraryfolders.vdf cannot be read: $($_.Exception.Message)")
        }
        if ($listed) {
            # The move counts, only the rest of the source goes
            $left = @(Remove-MoveSourceFolders $m $null)
            foreach ($p in @($m.StageGame, $m.StageWorkshop)) { [void](Remove-SteamMoveFolder $p) }
            $r = [ordered]@{ State = 'ok'; reason_code = $null; reason = 'finished after an interruption'; warning = $null }
            if ($left.Count -gt 0) {
                # Only a rest from now on: never taken back, keeps no move out
                $r['warning'] = 'source_not_deleted'
                $r['reason'] += "; not fully deleted on $($m.SourceLetter): $($left -join ', ')"
                try { Write-MoveJournal 'cleanup' } catch { Write-Log "Warning: the move journal could not be updated: $($_.Exception.Message)" }
                $Phase = 'cleanup'
                Write-Log "Interrupted move finished: the game is in $($m.TargetLib), the source is not fully deleted (tried again later)."
            } else {
                Write-Log "Interrupted move finished: the game is in $($m.TargetLib), the source is deleted."
            }
            $r['source_free_gb'] = Get-SteamMoveFreeGb $m.SourceLetter
            $r['target_free_gb'] = Get-SteamMoveFreeGb $m.TargetLetter
            $r['library'] = $m.TargetLib
            $r['new_library'] = [bool]$m.NewLibrary
            Send-MoveEnd $r
            if ($left.Count -gt 0) { return (& $pending 'source_not_deleted' "not fully deleted: $($left -join ', ')") }
            return $finished
        }
        Write-Log "Steam does not list the library $($m.TargetLib): the interrupted move is taken back."
    }

    if ($Phase -eq 'switch' -and -not $srcMan -and -not $tgtMan) {
        # Uninstalled meanwhile in the Steam client (uninstall.ps1 refuses while this journal is in
        # phase 'switch'). Only while the target library is there:
        # another disk that got the target's drive letter lacks the manifest as well.
        if (-not [System.IO.Directory]::Exists($m.TargetApps)) { return (& $pending 'drive_unavailable' "the library $($m.TargetLib) is not there") }
        $stay = @(@($m.SourceGame, $m.TargetGame) | Where-Object { [System.IO.Directory]::Exists($_) })
        $rest = if ($stay.Count -gt 0) { "; left as they are: $($stay -join ', ')" } else { '' }
        Write-Log "Interrupted move ended: the game is installed in neither library any more, nothing taken back$rest."
        # The old folder is a whole copy of the game: Home Assistant names it
        $warning = if ([System.IO.Directory]::Exists($m.SourceGame)) { 'source_not_deleted' } else { $null }
        Send-MoveEnd ([ordered]@{ State = 'error'; reason_code = 'not_installed'; reason = "the move was interrupted and the game was uninstalled meanwhile, nothing taken back$rest"; warning = $warning })
        return $finished
    }

    if ($Phase -eq 'undo' -and $tgtMan -and -not $srcMan) {
        # The take-back had put the game into the source; now it is in the target and not in the source:
        # installed there or moved there since, so what is there is Steam's game, not a rest of this move
        Write-Log "Rest of the take-back not deleted: the game is installed in $($m.TargetLib) since."
        return $finished
    }

    # Taken back (phase 'switch'), or the rest of a take-back (phase 'undo'). Steam must be closed while
    # it could see the game in the target.
    $ours = $tgtMan -or $Phase -eq 'undo'
    $stopped = $false
    if ($tgtMan -and (Test-SteamClientRunning)) {
        $running = Get-SteamRunningAppId
        if ($running -ne 0) { return (& $pending 'game_running' "a game runs (appid=$running), the Steam client cannot be closed") }
        Write-Log 'Stopping the Steam client to take the interrupted move back.'
        if (-not (Stop-SteamClient $steamExitSec) -and -not (Wait-SteamClientExit $steamExitMoreSec)) {
            return (& $pending 'steam_did_not_exit' 'the Steam client did not exit')
        }
        $stopped = $true
        Start-Sleep -Seconds $settleSec
    }
    $notes = @()
    $undo = $false
    try {
        if ($Phase -eq 'switch') {
            if (-not $srcMan) { [System.IO.File]::WriteAllBytes($m.SourceManifest, $m.ManifestBytes) }
            if ($null -ne $m.WorkshopAcfBytes -and -not [System.IO.File]::Exists($m.SourceWorkshopAcf)) { [System.IO.File]::WriteAllBytes($m.SourceWorkshopAcf, $m.WorkshopAcfBytes) }
            # The game is in the source again: what is left at the target is this move's
            if ($ours) { Write-MoveJournal 'undo'; $undo = $true }
        }
        if ($tgtMan) { [System.IO.File]::Delete($m.TargetManifest) }
        # The target's appworkshop file is this move's: the target had none before (target_exists)
        if ($ours -and $null -ne $m.WorkshopAcfBytes -and [System.IO.File]::Exists($m.TargetWorkshopAcf)) { [System.IO.File]::Delete($m.TargetWorkshopAcf) }
    } catch {
        $notes += "the manifests could not be put back: $($_.Exception.Message)"
    }
    $kept = ''
    if ($notes.Count -eq 0) {
        $installed = [System.IO.File]::Exists($m.SourceManifest)
        if (-not $ours) {
            if ([System.IO.Directory]::Exists($m.TargetGame)) { $kept = ", $($m.TargetGame) stays (no manifest of this move there)" }
        } elseif ($installed -and -not [System.IO.Directory]::Exists($m.SourceGame)) {
            if ([System.IO.Directory]::Exists($m.TargetGame)) { $notes += "$($m.SourceGame) is missing, $($m.TargetGame) kept" }
        } elseif (-not (Remove-SteamMoveFolder $m.TargetGame)) {
            $notes += "$($m.TargetGame) not fully deleted"
        }
        if ($ours -and ([System.IO.Directory]::Exists($m.SourceWorkshop) -or -not $installed) -and -not (Remove-SteamMoveFolder $m.TargetWorkshop)) { $notes += "$($m.TargetWorkshop) not fully deleted" }
        foreach ($p in @($m.StageGame, $m.StageWorkshop)) { if (-not (Remove-SteamMoveFolder $p)) { $notes += "$p not fully deleted" } }
    }
    if ($stopped) {
        try { if (Start-SteamClient -WaitExitSec 60) { Write-Log 'Steam client started again.' } } catch { Write-Log "Warning: the Steam client could not be started again: $($_.Exception.Message)" }
    }
    if ($Phase -eq 'switch' -and ($undo -or $notes.Count -eq 0)) {
        # The game is in the source again; a rest at the target goes later (phase 'undo')
        $rest = if ($notes.Count -gt 0) { "; not fully deleted yet (tried again later): $($notes -join '; ')" } else { '' }
        Write-Log "Interrupted move taken back: the game is in $($m.SourceLib)$kept$rest."
        if ($m.NewLibrary) { Write-Log "The library $($m.TargetLib) the move added stays (empty)." }
        Send-MoveEnd ([ordered]@{ State = 'error'; reason_code = 'interrupted'; reason = "the move was interrupted and taken back, the game stays in $($m.SourceLib)$kept$rest"; warning = $null })
    }
    if ($undo) { $Phase = 'undo' }
    if ($notes.Count -gt 0) { return (& $pending 'unexpected_error' ($notes -join '; ')) }
    if ($Phase -eq 'undo' -and -not $undo) { Write-Log "Rest of the taken back move deleted from $($m.TargetLib)." }
    return $finished
}

function Get-SteamMoveFreeGb([string]$Letter) {
    if (-not $Letter) { return $null }
    try { return [math]::Round(([System.IO.DriveInfo]::new($Letter + '\')).AvailableFreeSpace / 1GB, 1) } catch { return $null }
}

# Takes back what this move did, so the game is only where it was. Files at the target are removed
# only once the source manifest is back. Returns a note for the reason ('' when all is back).
function Undo-Move {
    $m = $script:mv
    if (-not $m) { return '' }
    $notes = @()
    if ($m.Process) {
        try { if (-not $m.Process.HasExited) { $m.Process.Kill(); [void]$m.Process.WaitForExit(15000) } } catch { }
        $m.Process = $null
    }
    $placed = $m.TargetPlaced -or $m.WorkshopPlaced -or $m.ManifestCopied -or $m.WorkshopAcfCopied -or $m.SourceManifestRemoved -or $m.Library
    $backups = if ($m.Library) { @($m.Library.Files | Where-Object { $_.Backup } | ForEach-Object { $_.Backup }) -join ', ' } else { '' }
    $wait = { Send-MoveProgress 'rollback' }
    if ($placed -or [System.IO.Directory]::Exists($m.StageGame) -or [System.IO.Directory]::Exists($m.StageWorkshop)) { Send-MoveProgress 'rollback' -Force }
    if ($placed -and (Test-SteamClientRunning)) {
        # The client must be closed: it would see the game twice, and it writes its libraries back
        Write-Log 'Stopping the Steam client to take the move back.'
        $m.SteamStopped = $true
        if (-not (Stop-SteamClient $steamExitSec) -and -not (Wait-SteamClientExit $steamExitMoreSec)) {
            # Steam runs with the game in the target: the move counts after all (Invoke-Move deletes
            # the source) only when Steam loaded the target library (its log said so; a new library
            # also kept every library from before) and shows the game installed there now. The
            # target manifest alone proves nothing: it is this move's copy.
            $loadedBySteam = $m.TargetLoaded -and (-not $m.Library -or $m.LibraryState -eq 'ok')
            if ($loadedBySteam -and $m.SourceManifestRemoved -and -not [System.IO.File]::Exists($m.SourceManifest) -and [System.IO.Directory]::Exists($m.TargetGame) -and (Test-SteamMoveFlags (Get-SteamMoveFlags $m.TargetManifest))) {
                Write-Log "The Steam client did not exit, but it shows the game installed in $($m.TargetLib) now: the move is finished."
                $m.KeepTarget = $true
                $m.SteamStopped = $false
                return '; Steam took the game late, the move was finished'
            }
            $m.RollbackFailed = $true
            $where = if ($m.SourceManifestRemoved) { "the game stays in $($m.TargetLib), its old folder $($m.SourceGame) too (finished or taken back at the next start of the agent or a move)" } else { "the copy $($m.TargetGame) stays" }
            if ($m.Library -and $m.LibraryState -ne 'ok') {
                # Steam writes its libraries back when it exits: nothing is restored under a running
                # client. Until the backups are put back the journal does not count the move.
                Set-SteamLibraryRestorePending $m.Library 'the Steam client did not exit'
                $where += "; libraryfolders.vdf NOT restored, put it back by hand from $backups first"
            }
            # The journal (written at the switch) keeps what Steam made of the new library
            if ($m.JournalFile -and $m.Library) {
                try { Write-MoveJournal 'switch' } catch { Write-Log "Warning: the move journal could not be updated: $($_.Exception.Message)" }
            }
            Write-Log "Warning: the Steam client did not exit, nothing taken back: $where."
            return "; the Steam client did not exit, nothing taken back: $where"
        }
        Start-Sleep -Seconds $settleSec
    }
    $sourceBack = $true
    try {
        if ($m.SourceManifestRemoved -and -not [System.IO.File]::Exists($m.SourceManifest)) { [System.IO.File]::WriteAllBytes($m.SourceManifest, $m.ManifestBytes) }
        if ($m.SourceWorkshopAcfRemoved -and -not [System.IO.File]::Exists($m.SourceWorkshopAcf)) { [System.IO.File]::WriteAllBytes($m.SourceWorkshopAcf, $m.WorkshopAcfBytes) }
        $m.SourceManifestRemoved = $false
        $m.SourceWorkshopAcfRemoved = $false
    } catch {
        $sourceBack = $false
        $notes += "the manifest could not be put back to $($m.SourceApps), the game stays in $($m.TargetLib)"
    }
    if ($sourceBack) {
        # The game is in the source again: what is left at the target is only this move's copy, deleted
        # by the next start when it cannot be now (Resume-MoveFromJournal, phase 'undo')
        if ($m.JournalFile -and ($m.TargetPlaced -or $m.WorkshopPlaced)) {
            try { Write-MoveJournal 'undo' } catch { Write-Log "Warning: the move journal could not be updated: $($_.Exception.Message)" }
        }
        foreach ($f in @(@($m.ManifestCopied, $m.TargetManifest), @($m.WorkshopAcfCopied, $m.TargetWorkshopAcf))) {
            if ($f[0] -and [System.IO.File]::Exists($f[1])) {
                try { [System.IO.File]::Delete($f[1]) } catch { $notes += "$($f[1]) not deleted" }
            }
        }
        $m.ManifestCopied = $false
        $m.WorkshopAcfCopied = $false
        if ($m.TargetPlaced -and -not (Remove-SteamMoveFolder $m.TargetGame $wait)) { $notes += "$($m.TargetGame) not fully deleted" }
        if ($m.WorkshopPlaced -and -not (Remove-SteamMoveFolder $m.TargetWorkshop $wait)) { $notes += "$($m.TargetWorkshop) not fully deleted" }
        $m.TargetPlaced = $false
        $m.WorkshopPlaced = $false
    }
    foreach ($p in @($m.StageGame, $m.StageWorkshop)) {
        if ($p -and -not (Remove-SteamMoveFolder $p $wait)) { $notes += "$p not fully deleted" }
    }
    if ($m.Library -and $m.LibraryState -ne 'ok') {
        # A library Steam loaded stays (empty); one it did not load, or after which it lost others, goes
        if (Restore-SteamLibraryConfig $m.Library) {
            Write-Log "libraryfolders.vdf restored from the backups ($backups)."
            if ($m.LibraryState -eq 'lost') {
                Write-Log "The new library folder $($m.Library.Path) stays (not registered after the restore)."
            } elseif (Remove-SteamLibraryFolder $m.Library) {
                Write-Log "New library folder $($m.Library.Path) removed."
            } else {
                $notes += "$($m.Library.Path) not fully removed"
            }
        } else {
            Set-SteamLibraryRestorePending $m.Library 'the backups could not be copied back'
            $notes += "libraryfolders.vdf NOT restored, put back by hand from $backups"
        }
    }
    if ($notes.Count -gt 0) {
        # A journal written at the switch stays: the next start takes the rest back
        Write-Log "Warning: taking the move back: $($notes -join '; ')."
        return '; ' + ($notes -join '; ')
    }
    Remove-MoveJournal
    Write-Log 'Move taken back, the game is where it was.'
    return ''
}

# The whole move; returns the result for Send-MoveEnd (State ok or error, reason_code, reason, ...)
function Invoke-Move {
    $script:mv = $null
    $result = $null
    try {
        [void](Resolve-Move)
        Copy-MoveWhileSteamRuns
        Close-SteamForMove
        Complete-MoveWhileSteamClosed
        Start-SteamAndVerify
        $result = Remove-MoveSource
    } catch {
        $code = [string]$_.Exception.Data['Code']
        $reason = $_.Exception.Message
        if (-not $code) { $code = 'unexpected_error'; $reason = "unexpected error: $reason" }
        Write-Log "ERROR: appid=$AppId - $code ($reason)"
        $extra = $_.Exception.Data['Extra']
        $note = ''
        try { $note = Undo-Move } catch { $note = "; taking the move back failed: $($_.Exception.Message)"; Write-Log "Warning: taking the move back failed: $($_.Exception.Message)" }
        if ($script:mv -and $script:mv.KeepTarget) {
            # Steam took the game in the target after all
            $result = Remove-MoveSource
            $result['reason'] = (@("finished although $code ($reason)", $result['reason']) | Where-Object { $_ }) -join '; '
        } else {
            if ($script:mv -and $script:mv.RollbackFailed) {
                # Neither moved nor taken back: HA must not treat it as an ordinary failure
                $reason = "$code ($reason)"
                $code = 'rollback_failed'
            }
            $result = [ordered]@{ State = 'error'; reason_code = $code; reason = $reason + $note; warning = $null }
            if ($extra) { foreach ($k in $extra.Keys) { $result[$k] = $extra[$k] } }
        }
    } finally {
        if ($script:mv -and $script:mv.SteamStopped) {
            # Also when it exited only late: the Steam client must not stay closed
            try {
                if (Start-SteamClient -WaitExitSec 60) { Write-Log 'Steam client started again.' }
                else { Write-Log 'Warning: the Steam client did not exit, it keeps running.' }
            } catch {
                Write-Log "Warning: the Steam client could not be started again: $($_.Exception.Message)"
            }
            $script:mv.SteamStopped = $false
        }
    }
    return $result
}

if ($moveDotSourced) { return }

# --- main --------------------------------------------------------------------------------------------

try {
    . (Join-Path $seRoot 'mqtt_publish.ps1')
} catch {
    # Without the shared library there is no log helper and no MQTT: one plain line, then stop
    try { [System.IO.File]::AppendAllText($logFile, ([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture) + "  Stopped: mqtt_publish.ps1 not loaded: $($_.Exception.Message)`r`n")) } catch { }
    exit 1
}
if ($SteamEngineNamesProblem) { Write-Log "Stopped: $SteamEngineNamesProblem"; exit 1 }
$script:mqttTopic = $SteamEngineTopic.Move
$seCfg = Get-SteamEngineConfig
$script:mqttHost = $seCfg.MqttHost
$script:mqttPort = $seCfg.MqttPort
$script:started = Format-SteamEngineUtc ([DateTime]::UtcNow)
$script:mqttCreds = Get-MqttCredentials $mqttCredFile
if ($script:mqttCreds.Problem) {
    Write-Log "Warning: progress and result cannot go to HA, MQTT login not usable: $($script:mqttCreds.Problem)"
} else {
    $script:mqttReady = -not ([string]::IsNullOrWhiteSpace($script:mqttCreds.Username) -or [string]::IsNullOrWhiteSpace($script:mqttCreds.Password))
}

if ($Recover) {
    # Started by the agent at its start: only interrupted moves, nothing new
    if (@(Get-MoveJournalFiles).Count -eq 0) { exit 0 }
    $jobLock = Enter-SteamEngineJobLock ($jobLockWaitSec * 1000)
    if (-not $jobLock) { Write-Log 'Interrupted move not looked at: another Steam job runs (the next move does it).'; exit 1 }
    $exitCode = 1
    try {
        Write-SteamInstallMarker $moveLockFile @()
        Send-PendingMoveEnd
        if (-not (Resume-InterruptedMove)) { $exitCode = 0 }
    } catch {
        Write-Log "ERROR: the interrupted move could not be looked at: $($_.Exception.Message)"
    } finally {
        try { [System.IO.File]::Delete($moveLockFile) } catch { }
        Exit-SteamEngineJobLock $jobLock
    }
    exit $exitCode
}

# The agent checks the command; checked again here (a hand start)
if ($AppId -notmatch '^\d{1,10}$' -or $Drive -notmatch '^[A-Za-z]:?$') {
    Write-Log "ERROR: invalid_request (appid '$AppId', drive '$Drive')"
    Send-MoveEnd ([ordered]@{ State = 'error'; reason_code = 'invalid_request'; reason = 'invalid app ID or drive'; warning = $null })
    exit 1
}
Write-Log "Move requested: appid=$AppId to $($Drive.Substring(0, 1).ToUpperInvariant()):"
if ($seCfg.ConfigProblem) { Write-Log "Warning: $($seCfg.ConfigProblem)." }

$jobLock = Enter-SteamEngineJobLock 0
if (-not $jobLock) {
    $holder = Read-SteamInstallMarker $installLock
    $why = $null
    if ($holder.Running) {
        $why = "an install job runs (PID $($holder.Pid))"
    } else {
        Write-Log 'Waiting for another Steam job to end.'
        $jobLock = Enter-SteamEngineJobLock ($jobLockWaitSec * 1000)
        if (-not $jobLock) { $why = "another Steam job still runs after $jobLockWaitSec s" }
    }
    if ($why) {
        Write-Log "ERROR: appid=$AppId - job_running ($why)"
        Send-MoveEnd ([ordered]@{ State = 'error'; reason_code = 'job_running'; reason = $why; warning = $null })
        exit 1
    }
}

$exitCode = 1
try {
    # The agent reports steam_moving while this file names a running move.ps1
    try { Write-SteamInstallMarker $moveLockFile @($AppId) } catch { Write-Log "Warning: $moveLockFile not written: $($_.Exception.Message)" }
    # An end an earlier move could not send goes out before this move's messages
    Send-PendingMoveEnd
    # Moves cut off earlier come first; one in phase 'switch' that cannot be finished or taken back yet
    # keeps new moves out (a rest left in phase 'cleanup' does not)
    $rec = Resume-InterruptedMove
    if ($rec) {
        $why = "an interrupted move is not finished yet ($($rec.Code): $($rec.Reason))"
        Write-Log "ERROR: appid=$AppId - job_running ($why)"
        Send-MoveEnd ([ordered]@{ State = 'error'; reason_code = 'job_running'; reason = $why; warning = $null })
    } else {
        $result = Invoke-Move
        if ($result.State -eq 'ok') { $exitCode = 0 }
        Send-MoveEnd $result
    }
} catch {
    Write-Log "ERROR: appid=$AppId - unexpected_error ($($_.Exception.Message))"
    Send-MoveEnd ([ordered]@{ State = 'error'; reason_code = 'unexpected_error'; reason = $_.Exception.Message; warning = $null })
} finally {
    try { [System.IO.File]::Delete($moveLockFile) } catch { }
    Exit-SteamEngineJobLock $jobLock
}
exit $exitCode
