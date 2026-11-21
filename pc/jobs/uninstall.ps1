param([Parameter(Mandatory = $true)][string]$AppId)
# Uninstalls a Steam game from the libraries of the Steam client.
# Started by the Steam agent (command uninstall:<appid>). The result goes to Home Assistant over MQTT
# (topic steam/uninstall, see PROTOCOL.md) with a reason code on errors. The agent notices the removed
# manifest in its next round and sends the library then (report.ps1).
# One Steam job at a time: the job lock (mqtt_publish.ps1) is shared with install.ps1. The Steam
# client is closed for the uninstall and always started again afterwards, also when it exits late.
# ASCII only in this file (Windows PowerShell 5.1 reads .ps1 without BOM as ANSI).
$ErrorActionPreference = 'Stop'

$logFile      = Join-Path $PSScriptRoot 'uninstall.log'
$installLock  = Join-Path $PSScriptRoot 'install.lock'
$mqttClientId = ($env:COMPUTERNAME.ToLower() + '-steam-uninstall')
$seRoot = Join-Path $env:LOCALAPPDATA 'SteamEngine'
$mqttCredFile = Join-Path $seRoot 'mqtt_credentials.txt'
# Waiting for the job lock while another uninstall runs (an install job is answered at once)
$jobLockWaitSec = 180

try {
    . (Join-Path $seRoot 'mqtt_publish.ps1')
} catch {
    # Without the shared library there is no log helper and no MQTT: one plain line, then stop
    try { [System.IO.File]::AppendAllText($logFile, ([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture) + "  Stopped: mqtt_publish.ps1 not loaded: $($_.Exception.Message)`r`n")) } catch { }
    exit 1
}
$mqttTopic = $SteamEngineTopic.Uninstall
$seCfg    = Get-SteamEngineConfig
$mqttHost = $seCfg.MqttHost
$mqttPort = $seCfg.MqttPort

function Write-Log([string]$message) { Write-SteamEngineLog $logFile $message }
if ($SteamEngineNamesProblem) { Write-Log "Stopped: $SteamEngineNamesProblem"; exit 1 }

function Send-Result($result, $name, $reason, $freeGb) {
    try {
        $creds = Get-MqttCredentials $mqttCredFile
        if ($creds.Problem) {
            Write-Log "Warning: could not report the result to HA, MQTT login not usable: $($creds.Problem)"
            return
        }
        $body = [ordered]@{ appid = $AppId; name = $name; result = $result; reason = $reason; free_gb = $freeGb }
        $bytes = [System.Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Compress))
        Publish-MqttRetained -BrokerHost $mqttHost -Port $mqttPort -ClientId $mqttClientId `
            -Username $creds.Username -Password $creds.Password -Topic $mqttTopic -PayloadBytes $bytes -Qos 1 -Retain $false
    } catch {
        Write-Log "Warning: could not report the result to HA: $($_.Exception.Message)"
    }
}

# Reason codes (texts in HA): invalid_appid, job_running, not_installed, drive_unavailable,
# library_unreadable, steam_busy, unexpected_folder, shared_folder, game_running, steam_did_not_exit,
# delete_failed, unexpected_error
function Stop-With($code, $name, $detail) {
    Write-Log "ERROR: appid=$AppId - $code$(if ($detail) { " ($detail)" })"
    Send-Result 'error' $name $code $null
    exit 1
}

# The game folder goes as a whole: not when another app of the library (e.g. an editor of the game)
# has it too, nor while that cannot be told (a manifest there cannot be read)
function Assert-OwnGameFolder {
    $other = Find-SteamSharedInstallDir $apps $AppId $installDir
    if ($other -and $other.Unclear) { Stop-With 'steam_busy' $name $other.Unclear }
    if ($other) { Stop-With 'shared_folder' $name "$gameDir is also the install folder of appid=$($other.AppId) ($($other.Name))" }
}

if ($AppId -notmatch '^\d+$') { Stop-With 'invalid_appid' $null }
Write-Log "Uninstall requested: appid=$AppId"
if ($seCfg.ConfigProblem) { Write-Log "Warning: $($seCfg.ConfigProblem)." }

$jobLock = Enter-SteamEngineJobLock 0
if (-not $jobLock) {
    $holder = Read-SteamInstallMarker $installLock
    if ($holder.Running) { Stop-With 'job_running' $null "install job, PID $($holder.Pid)" }
    Write-Log 'Waiting for another uninstall to end.'
    $jobLock = Enter-SteamEngineJobLock ($jobLockWaitSec * 1000)
    if (-not $jobLock) { Stop-With 'job_running' $null "another job still runs after $jobLockWaitSec s" }
}

$exitCode = 1
$name = $null
# Set once this script asked the Steam client to exit: then it is started again at the end
$steamStopped = $false
try {
    # A move of this game cut off after its switch (move.ps1's journal in phase 'switch') is finished or
    # taken back first: until then its old folder or its copy would be left with nothing to delete it
    foreach ($f in @([System.IO.Directory]::GetFiles($PSScriptRoot, "move_pending_$($AppId)_*.json"))) {
        if ([System.IO.Path]::GetFileName($f) -notmatch ('^move_pending_' + $AppId + '_\d{14}\.json$')) { continue }
        $j = $null
        try { $j = (Read-SharedTextFile $f) | ConvertFrom-Json } catch { }
        if (-not $j -or $j.phase -eq 'switch') {
            Stop-With 'job_running' $(if ($j -and $j.name) { [string]$j.name }) "a move of the game cut off after its switch is not finished yet ($f; the agent's start or move.ps1 -Recover does it)"
        }
    }
    # Get-SteamLibraryInfo (mqtt_publish.ps1): libraries without duplicates, Ready = drive there.
    $libInfos = $null
    try { $libInfos = @(Get-SteamLibraryInfo $seCfg.SteamRoot) } catch { Stop-With 'library_unreadable' $null $_.Exception.Message }
    $where = Find-SteamGameLibrary $libInfos $AppId
    if (-not $where.Manifest) {
        if ($where.OfflineLibrary) { Stop-With 'drive_unavailable' $null $where.OfflineLibrary }
        Stop-With 'not_installed' $null
    }
    $manifest = $where.Manifest
    $libRoot = $where.Library

    # UTF-8: Steam writes the manifest without BOM (name and installdir can have non-ASCII characters)
    $c = Read-SharedTextFile $manifest
    if (-not $c) { Stop-With 'not_installed' $null 'manifest not readable' }
    $name       = Get-AcfValue -Key 'name' -Text $c
    if (-not $name) { $name = $AppId }
    $installDir = [string](Get-AcfValue -Key 'installdir' -Text $c)
    $flagsText  = Get-AcfValue -Key 'StateFlags' -Text $c
    $flags      = if ($flagsText -match '^\d+$') { [int64]$flagsText } else { [int64]0 }
    $sizeText   = Get-AcfValue -Key 'SizeOnDisk' -Text $c
    $sizeGb     = if ($sizeText -match '^\d+$') { [math]::Round([int64]$sizeText / 1GB, 1) } else { $null }

    # Steam works on the game right now (download, check, its own uninstall)
    if (($flags -band ($SteamAppState.Active -bor $SteamAppState.Uninstalling)) -ne 0) { Stop-With 'steam_busy' $name "StateFlags=$flags" }
    # installdir comes from the manifest; only a plain folder name may be deleted
    if ([string]::IsNullOrWhiteSpace($installDir) -or $installDir -match '[\\/:]' -or $installDir -match '^\.+$') {
        Stop-With 'unexpected_folder' $name "'$installDir'"
    }
    $apps    = [System.IO.Path]::Combine($libRoot, 'steamapps')
    $gameDir = [System.IO.Path]::Combine($apps, 'common', $installDir)
    $workshopDir = [System.IO.Path]::Combine($apps, 'workshop', 'content', $AppId)
    Assert-OwnGameFolder
    # A game or workshop folder that is a link points somewhere else: nothing is deleted there
    foreach ($p in @($gameDir, $workshopDir)) {
        if (Test-SteamLink $p) { Stop-With 'unexpected_folder' $name "$p is a junction or symbolic link" }
    }

    $running = Get-SteamRunningAppId
    if ($running -ne 0) { Stop-With 'game_running' $name "appid=$running" }
    $why = Get-SteamGameRunningReason $AppId $gameDir
    if ($why) { Stop-With 'game_running' $name $why }

    # Stop the Steam client, otherwise it keeps the game as installed in memory (started again below)
    if (Test-SteamClientRunning) {
        Write-Log 'Stopping the Steam client.'
        $steamStopped = $true
        if (-not (Stop-SteamClient 90)) {
            Write-Log 'The Steam client did not exit within 90 s, waiting 60 s more.'
            if (-not (Wait-SteamClientExit 60)) { Stop-With 'steam_did_not_exit' $name 'within 150 s' }
        }
        Start-Sleep -Seconds 3
    }
    # Nothing may have started from the folder or taken it meanwhile (with Steam closed its manifests
    # stay as they are)
    $why = Get-SteamGameRunningReason $AppId $gameDir
    if ($why) { Stop-With 'game_running' $name $why }
    Assert-OwnGameFolder

    $failure = $null
    try {
        # Game files first, then the manifest: if something stays behind, Steam does not see the game as gone.
        # Remove-SteamFolder removes a link inside a folder itself, never what it points to.
        $left = Remove-SteamFolder $gameDir
        if ($left) { throw "$gameDir not fully deleted: $left" }
        Remove-Item -LiteralPath $manifest -Force
        foreach ($rest in @(
                [System.IO.Path]::Combine($apps, 'downloading', $AppId),
                [System.IO.Path]::Combine($apps, 'shadercache', $AppId),
                $workshopDir)) {
            [void](Remove-SteamFolder $rest)
        }
        $workshopAcf = [System.IO.Path]::Combine($apps, 'workshop', "appworkshop_$AppId.acf")
        if (Test-Path -LiteralPath $workshopAcf) { Remove-Item -LiteralPath $workshopAcf -Force -ErrorAction SilentlyContinue }
    } catch {
        $failure = $_.Exception.Message
    }

    $freeGb = $null
    try { $freeGb = [math]::Round(([System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($libRoot))).AvailableFreeSpace / 1GB, 1) } catch { }

    if ($failure) {
        Stop-With 'delete_failed' $name $failure
    }
    Write-Log "OK: appid=$AppId ($name) uninstalled from $gameDir (about $sizeGb GB), free on the drive: $freeGb GB."
    Send-Result 'ok' $name $null $freeGb
    $exitCode = 0
} catch {
    # Not one of the expected errors above: HA still gets an answer
    $msg = $_.Exception.Message
    Write-Log "ERROR: appid=$AppId - unexpected_error ($msg)"
    Send-Result 'error' $name 'unexpected_error' $null
} finally {
    if ($steamStopped) {
        # Also when it exited only late: the Steam client must not stay closed
        try {
            if (Start-SteamClient -WaitExitSec 60) {
                Write-Log 'Steam client started again.'
            } else {
                Write-Log 'Warning: the Steam client did not exit, it keeps running.'
            }
        } catch {
            Write-Log "Warning: the Steam client could not be started again: $($_.Exception.Message)"
        }
    }
    Exit-SteamEngineJobLock $jobLock
}
exit $exitCode
