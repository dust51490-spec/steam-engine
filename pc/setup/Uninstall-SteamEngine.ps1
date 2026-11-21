<#
Removes the PC side of Steam Engine. Counterpart of Install-SteamEngine.ps1.

  Only show what would happen:  powershell -ExecutionPolicy Bypass -File Uninstall-SteamEngine.ps1
  Really remove:                powershell -ExecutionPolicy Bypass -File Uninstall-SteamEngine.ps1 -Yes
  Autologon off too:            powershell -ExecutionPolicy Bypass -File Uninstall-SteamEngine.ps1 -Yes -AutologonOff

What it does (with -Yes):
  1. Stops when a Steam job (install, uninstall or move of a game) runs right now, or a move is not
     finished (jobs\move_pending_*.json, finished by Steam Engine; the message says how, also for a
     drive that is gone for good); run it again afterwards. Names manifests a failed install left for
     the next job to remove (jobs\pending_manifest_cleanup.txt), which no job removes after the
     uninstall.
  2. Windows autologon: while it signs in this account it stays, with a warning (without the lock
     the PC starts with its desktop open, and the password stays stored by Windows), also in the dry
     run. With -AutologonOff it is turned off first with Set-Autologon.ps1 -State off (asks for
     administrator rights); if it still signs in this account then, the uninstall stops and nothing
     is removed, so the lock stays. From a folder other Windows accounts can change, Set-Autologon.ps1
     does not run as administrator; the dry run says so and how to turn it off.
  3. Removes the scheduled tasks "Steam Engine Agent" and "Steam Engine Lock" and stops the agent and the other scripts of Steam Engine that
     run from %LOCALAPPDATA%\SteamEngine (e.g. a library report).
  4. Cleans up Home Assistant: clears the retained MQTT discovery of the agent and of its plugin
     entities (the entities disappear) and the retained states (steam/agent/..., steam/job,
     steam/library, steam/depots, steam/history). After step 3, so the agent cannot send them again.
     After three failures with none cleared it stops trying (Home Assistant or its broker not
     reachable).
  5. Deletes %LOCALAPPDATA%\SteamEngine (with the credentials), unless not one topic of step 4 could
     be cleared: then the folder stays with the MQTT login, so a second run can clean up Home
     Assistant.
Games and the Steam client stay untouched. Plugins (e.g. a backup script) have their own parts
and are not removed here, but their entries in agent\plugins.psd1 go with the folder: keep a copy
to set Steam Engine up again.
Exit code: 0 done, 1 stopped; a dry run gives 3 while autologon signs in this account (Uninstall.cmd
then asks whether to turn it off), 4 while it does but cannot be turned off from this folder
(Uninstall.cmd then says how), else 0.
ASCII only in this file (Windows PowerShell 5.1 reads .ps1 without BOM as ANSI).
#>
param(
    [switch]$Yes,
    [switch]$AutologonOff
)
$ErrorActionPreference = 'Stop'

$root = Join-Path $env:LOCALAPPDATA 'SteamEngine'
$tasks = @('Steam Engine Agent', 'Steam Engine Lock')
# Stays in this package folder after the uninstall
$autologonScript = Join-Path $PSScriptRoot 'Set-Autologon.ps1'
# The shared library of this package (the same version as this script), else the installed one
$publish = Join-Path (Split-Path $PSScriptRoot -Parent) 'common\mqtt_publish.ps1'
if (-not (Test-Path -LiteralPath $publish)) { $publish = Join-Path $root 'mqtt_publish.ps1' }

function Info($text) { Write-Host "   $text" }
function Warn($text) { Write-Host "   $text" -ForegroundColor Yellow }
function Invoke-Step($text, [scriptblock]$action) {
    if ($Yes) { Info $text; & $action } else { Info "(would) $text" }
}
# This PC's device in Home Assistant, for a clean-up by hand: named as the PC ("Steam PC" without a
# name; $SteamEngineNames of mqtt_publish.ps1), unknown while that is not loaded
function Get-DeviceText {
    if ($SteamEngineNames) { return "device ""$($SteamEngineNames.DeviceName)""" }
    return 'the device of this PC: "Steam PC", or its PcName in settings.psd1'
}

if (-not $Yes) { Write-Host 'Dry run, nothing is changed. Use -Yes to really remove.' -ForegroundColor Yellow }

# --- 1. No Steam job may run (it would stay half done, maybe with the Steam client closed) -----------
# The job lock of install.ps1, uninstall.ps1 and move.ps1 (see mqtt_publish.ps1). Held until this
# script ends, so no job starts meanwhile.
$jobLock = New-Object System.Threading.Mutex($false, 'Local\SteamEngineJob')
$free = $false
try {
    $free = $jobLock.WaitOne(0)
} catch {
    # Abandoned: the last job was stopped without releasing it, none runs now
    $free = ($_.Exception -is [System.Threading.AbandonedMutexException] -or $_.Exception.InnerException -is [System.Threading.AbandonedMutexException])
}
if (-not $free) {
    Warn 'Steam Engine is installing, uninstalling or moving a game right now. Run this again when it is done.'
    if ($Yes) { exit 1 }
}
# Interrupted moves (move.ps1): their journal is the only record of the old folder or the copy left
# behind; the agent finishes them at its start ("jobs\move.ps1 -Recover")
$jobs = Join-Path $root 'jobs'
$journals = @(Get-ChildItem -LiteralPath $jobs -Filter 'move_pending_*.json' -File -ErrorAction SilentlyContinue)
foreach ($f in $journals) {
    $j = $null
    try { $j = [System.IO.File]::ReadAllText($f.FullName) | ConvertFrom-Json } catch { }
    if (-not $j) { Warn "A move is not finished ($($f.Name), not readable)."; continue }
    $what = switch ($j.phase) {
        'cleanup' { "its old folder is not fully deleted: $($j.source_lib)\steamapps\common\$($j.installdir)" }
        'undo' { "its copy is not fully deleted: $($j.target_lib)\steamapps\common\$($j.installdir)" }
        default { 'it is neither finished nor taken back' }
    }
    # The drives it needs (only the source for the rest of a finished move)
    $missing = @()
    foreach ($lib in $(if ($j.phase -eq 'cleanup') { @($j.source_lib) } else { @($j.source_lib, $j.target_lib) })) {
        $letter = "$lib".Substring(0, [Math]::Min(2, "$lib".Length))
        $there = $false
        try { $there = ([System.IO.DriveInfo]::new($letter + '\')).IsReady } catch { }
        if (-not $there) { $missing += $letter }
    }
    if ($missing.Count -gt 0) {
        $what += ". Drive $($missing -join ', ') is not there: connect it; if it is gone for good, delete $($f.FullName) by hand (the folder named stays then)"
    }
    Warn "A move is not finished: $($j.name) (appid $($j.appid)) from $($j.source_lib) to $($j.target_lib), $what."
}
# Manifests of first installs that failed, left for the next install job to remove (install.ps1)
$pendingText = $null
try { $pendingText = [System.IO.File]::ReadAllText((Join-Path $jobs 'pending_manifest_cleanup.txt')) } catch { }
$pendingManifests = @("$pendingText" -split "`r?`n" | Where-Object { $_ -match '^\d+\|' } | ForEach-Object { ($_ -split '\|', 2)[1] })
if ($pendingManifests.Count -gt 0) {
    Warn ('Manifests of failed installs wait for the next install job to remove them (jobs\pending_manifest_cleanup.txt); after the uninstall none does, and Steam may download these games. Delete them by hand if not wanted: ' + ($pendingManifests -join ', '))
}
if ($journals.Count -gt 0) {
    Warn ('Steam Engine finishes a move at the next start of its agent, or now with: powershell -ExecutionPolicy Bypass -File "' + (Join-Path $jobs 'move.ps1') + '" -Recover. A folder that stays because a program uses it: close that program (or delete the folder by hand), then run -Recover.')
    if ($Yes) { exit 1 }
}

# --- 2. Windows autologon ----------------------------------------------------------------------
# The lock goes with Steam Engine (step 3); autologon stays unless -AutologonOff, which comes first:
# nothing is removed while autologon still signs in this account
Write-Host ''; Write-Host '== Windows autologon' -ForegroundColor Cyan
$autologon = $null
try { $autologon = & $autologonScript -Query } catch { Warn "The state of Windows autologon could not be read: $($_.Exception.Message)" }
# Set-Autologon.ps1 does not run as administrator from a folder other Windows accounts can change
$offHint = if ($autologon.ForeignWriter) {
    "It cannot be turned off from this folder: other Windows accounts can change files here ($($autologon.ForeignWriter)), so Set-Autologon.ps1 does not run as administrator from it. To turn it off, extract the zip again into a folder under $env:USERPROFILE and run there: powershell -ExecutionPolicy Bypass -File setup\Set-Autologon.ps1 -State off"
} else {
    'To turn it off: powershell -ExecutionPolicy Bypass -File "' + $autologonScript + '" -State off'
}
if ($autologon.ThisAccount -and $AutologonOff -and -not $autologon.ForeignWriter) {
    Invoke-Step 'turn Windows autologon off (Windows asks for administrator rights)' {
        try { & $autologonScript -State off } catch { Warn "Failed: $($_.Exception.Message)" }
        $script:autologon = $null
        try { $script:autologon = & $autologonScript -Query } catch { Warn "The state of Windows autologon could not be read: $($_.Exception.Message)" }
    }
}
if ($Yes -and $AutologonOff -and (-not $autologon -or $autologon.ThisAccount)) {
    Warn 'Autologon was not turned off, so nothing was removed: the lock of Steam Engine stays.'
    Warn $offHint
    Warn 'Then run the uninstall again, or run it without turning autologon off.'
    exit 1
}
if ($autologon.ThisAccount) {
    # Not after the "(would)" of a dry run with -AutologonOff
    if (-not $AutologonOff -or $autologon.ForeignWriter) {
        Warn "Autologon stays on for $($autologon.User): Windows signs in by itself at every start, and without the lock of"
        Warn 'Steam Engine the PC then stands open. The Windows password stays stored (encrypted by Windows).'
        Warn $offHint
    }
} elseif ($autologon) {
    Info $(if ($autologon.On) { "Autologon signs in $($autologon.User), not this account; it stays." } else { 'Autologon is off.' })
}

# --- 3. Agent and tasks ------------------------------------------------------------------------------
Write-Host ''; Write-Host '== Agent and tasks' -ForegroundColor Cyan
foreach ($t in $tasks) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) { Invoke-Step "remove task $t" { Unregister-ScheduledTask -TaskName $t -Confirm:$false } }
    else { Info "There is no task $t." }
}
# Only the scripts of this Windows user (started from its own folder), not those of another user
$running = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($root + '\', [StringComparison]::OrdinalIgnoreCase) -ge 0 })
if ($running.Count -gt 0) {
    Invoke-Step "stop the Steam agent and the other scripts of Steam Engine ($($running.Count) running)" {
        foreach ($p in $running) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        # The broker sends the agent's last will (offline) when the connection drops; it is cleared below
        Start-Sleep -Seconds 3
    }
} else {
    Info 'The Steam agent does not run.'
}

# --- 4. Home Assistant (MQTT) --------------------------------------------------------------------
Write-Host ''; Write-Host '== Clean up Home Assistant (MQTT)' -ForegroundColor Cyan
$topics = @()
$cfg = $null
$cred = $null
try {
    . $publish
    if ($SteamEngineNamesProblem) { throw $SteamEngineNamesProblem }
    $cfg = Get-SteamEngineConfig
    $cred = Get-MqttCredentials (Join-Path $root 'mqtt_credentials.txt')
    $topics = @(Get-SteamEngineDiscovery | ForEach-Object { $_.Topic })
    # Plugin entities: those in plugins.psd1 and those the agent announced (also of removed plugins)
    $pluginsFile = Join-Path $root 'agent\plugins.psd1'
    if (Test-Path -LiteralPath $pluginsFile) {
        try {
            $p = Import-PowerShellDataFile -LiteralPath $pluginsFile
            # A missing section would give one $null key
            if ($p.Commands -is [System.Collections.IDictionary]) { foreach ($n in @($p.Commands.Keys)) { $topics += Get-SteamEnginePluginTopic 'command' "$n" } }
            if ($p.Busy -is [System.Collections.IDictionary]) { foreach ($n in @($p.Busy.Keys)) { $topics += Get-SteamEnginePluginTopic 'busy' "$n" } }
        } catch {
            Warn "plugins.psd1 not readable ($($_.Exception.Message)); its entities are cleared only if the agent noted them."
        }
    }
    $announced = Read-SharedTextFile (Join-Path $root 'agent\announced_plugins.txt')
    if ($announced) { $topics += @($announced -split "`r?`n" | Where-Object { $_ -match '^homeassistant/' }) }
    $topics = @(@($topics) + @(Get-SteamEngineRetainedTopics) | Select-Object -Unique)
} catch {
    Warn "Home Assistant has to be cleaned up by hand ($(Get-DeviceText)): $($_.Exception.Message)"
    $topics = @()
}
if ($topics.Count -gt 0 -and $cred.Problem) {
    Warn "MQTT login not usable ($($cred.Problem)), Home Assistant has to be cleaned up by hand ($(Get-DeviceText))."
    $topics = @()
}
$cleared = 0
$failed = 0
foreach ($t in $topics) {
    Invoke-Step "clear $t" {
        try {
            Publish-MqttRetained -BrokerHost $cfg.MqttHost -Port $cfg.MqttPort -ClientId ('steam-engine-uninstall-' + $env:COMPUTERNAME) `
                -Username $cred.Username -Password $cred.Password -Topic $t -PayloadBytes ([byte[]]@()) -Qos 1 -Retain $true
            $script:cleared++
        } catch {
            $script:failed++
            Warn "Warning: $t not cleared: $($_.Exception.Message)"
        }
    }
    # Broker not reachable or login refused: the other topics would fail the same way
    if ($cleared -eq 0 -and $failed -ge 3) { Warn 'The other topics are not tried.'; break }
}
# Not one cleared: the folder with the MQTT login stays, so a second run can clean up (step 5)
$keepFolder = ($cleared -eq 0 -and $failed -gt 0)
if ($failed -gt 0 -and -not $keepFolder) { Warn "Not every topic was cleared: remove what is left in Home Assistant by hand ($(Get-DeviceText))." }

# --- 5. Files ------------------------------------------------------------------------------------
Write-Host ''; Write-Host '== Files' -ForegroundColor Cyan
if ($keepFolder) {
    Warn "Nothing could be cleared in Home Assistant, so $root stays with the MQTT login."
    Warn "Run this again when Home Assistant and its MQTT broker can be reached (or remove $(Get-DeviceText) there by hand and delete the folder)."
    exit 1
}
if (Test-Path -LiteralPath $root) { Invoke-Step "delete folder $root" { Remove-Item -LiteralPath $root -Recurse -Force } } else { Info "There is no $root." }
Info 'Games and the Steam client stay.'
if ($free) { try { $jobLock.ReleaseMutex() } catch { } }
Write-Host ''
if (-not $Yes) {
    Write-Host 'Dry run finished.' -ForegroundColor Yellow
    # For Uninstall.cmd: ask whether to turn autologon off too, or say how when it cannot from here
    if ($autologon.ThisAccount -and -not $AutologonOff) { exit $(if ($autologon.ForeignWriter) { 4 } else { 3 }) }
}
