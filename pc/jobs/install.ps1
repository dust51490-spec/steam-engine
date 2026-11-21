# Installs or updates Steam games with the Steam client. Started by the Steam agent for the command
# install:[<job>:]<appid>[@<letter>],<appid>[@<letter>],... (see PROTOCOL.md) and reports its progress
# on steam/job, every state with the job ID <job> Home Assistant gave the job (-JobId; none when started
# without one). The letter is the target drive of a first install; without it Steam's default library.
# A suitable drive without a library gets a new one (<letter>:\SteamLibrary, New-SteamLibrary) while
# the client is closed for the first install; the job checks that Steam took it after the start.
# A first install writes a minimal appmanifest and restarts the client, an update is started with
# steam://validate/<appid>. Install folder, name and size come from api.steamcmd.net (a public web API,
# not SteamCMD); an update of an installed game also works without it.
# One Steam job at a time: the job lock (mqtt_publish.ps1) is shared with uninstall.ps1. install.lock
# names the running job; left behind, it tells report.ps1 that the job was interrupted. A copy started
# for games another job has or just had leaves them (HA sends a command again when it does not see the
# job start). Every job ends with done or done_with_errors on steam/job, also after an unexpected error.
# ASCII only in this file (Windows PowerShell 5.1 reads .ps1 without BOM as ANSI).
# Not -AppIds: a parameter and a later variable of the same name are the same variable in PowerShell.
param([string]$JobAppIds = '', [string]$JobId = '')
$ErrorActionPreference = 'Stop'

$seRoot        = Join-Path $env:LOCALAPPDATA 'SteamEngine'
$logFile       = Join-Path $PSScriptRoot 'install.log'
$lockFile      = Join-Path $PSScriptRoot 'install.lock'
# The end state of a job that could not be sent (MQTT down); report.ps1 sends it later
$finalFile     = Join-Path $PSScriptRoot 'install_final.json'
$reportScript  = Join-Path $seRoot 'library\report.ps1'
# Test switch: this file next to the script pretends free space in GB (Get-TestFreeGb, PROTOCOL.md)
$spaceTestFile = Join-Path $PSScriptRoot 'space_test_free_gb.txt'
# Reserve for the space check: 10 % of the game size, at least 5 GB (Get-SteamSpaceReserve)
# Manifests a job could not take back (see Add-PendingCleanup)
$pendingCleanupFile = Join-Path $PSScriptRoot 'pending_manifest_cleanup.txt'
# The games of the last job that ended (PID and app IDs like install.lock), written before it frees
# the job lock: a copy of this script that waited sees by its time which games were handled meanwhile
$lastJobFile = Join-Path $PSScriptRoot 'install_last.txt'

$mqttClientId       = ($env:COMPUTERNAME.ToLower() + '-steam-job')
$mqttCredFile       = Join-Path $seRoot 'mqtt_credentials.txt'
$mqttRetainedStates = @('done', 'done_with_errors')

# No sign of the game in the Steam log within this time = Steam will not install it (not owned,
# unknown, Steam offline). No progress at all for the idle time = give up.
$clientStartTimeout = 300
$clientIdleTimeout  = 3600
# An error Steam does not get past within this time ends the job (Steam retries by itself every ~30 s)
$clientRetryTimeout = 600
# A round of the wait for Steam (10 s) that took longer than this: the PC slept (standby, hibernation),
# and that time counts for none of the limits above
$sleepGapSec = 120
# A new library: Steam must load it within this time after its start (content_log)
$libraryAcceptSec = 90
# Less than this written (or read while checking) since the last progress is no progress:
# idle Steam writes a few KB now and then
$clientProgressBytes = 32MB
# Waiting for the job lock: an uninstall holds it for a few minutes (Steam exit up to 150 s, deleting
# a big game, waiting for a late exit before the restart). A running install job is waited for
# without this limit (see the main part). Looked at again every $jobLockStepSec seconds.
$jobLockWaitSec = 600
$jobLockStepSec = 5
# The library report at the end of a job; one that runs longer hangs and is stopped
$reportMaxSec = 300

try {
    . (Join-Path $seRoot 'mqtt_publish.ps1')
} catch {
    # Without the shared library there is no log helper and no MQTT: one plain line, then stop
    try { [System.IO.File]::AppendAllText($logFile, ([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture) + "  Stopped: mqtt_publish.ps1 not loaded: $($_.Exception.Message)`r`n")) } catch { }
    exit 1
}

function Write-Log([string]$message) { Write-SteamEngineLog $logFile $message }
if ($SteamEngineNamesProblem) { Write-Log "Stopped: $SteamEngineNamesProblem"; exit 1 }

$mqttJobTopic = $SteamEngineTopic.Job

$seCfg              = Get-SteamEngineConfig
$mqttHost           = $seCfg.MqttHost
$mqttPort           = $seCfg.MqttPort
# The library for first installs, found once per job before it stops the Steam client
# (Resolve-NewGameLibrary); $null with the reason in $newGameLibraryProblem
$script:newGameLibrary = $null
$script:newGameLibraryProblem = ''
$steamClientConnLog = [System.IO.Path]::Combine($seCfg.SteamRoot, 'logs', 'connection_log.txt')
$steamContentLog    = [System.IO.Path]::Combine($seCfg.SteamRoot, 'logs', 'content_log.txt')
$mqttCreds = Get-MqttCredentials $mqttCredFile
$mqttReady = -not ([string]::IsNullOrWhiteSpace($mqttCreds.Username) -or [string]::IsNullOrWhiteSpace($mqttCreds.Password))
# StateFlags of a manifest written for a first install: Steam starts the download right away
$newManifestFlags = $SteamAppState.UpdateRequired -bor $SteamAppState.UpdateStarted

# --- job state on steam/job -----------------------------------------------------------------------

function ConvertTo-JobStateJson([string]$state, $attrs) {
    $body = [ordered]@{ state = $state }
    # Home Assistant knows by it which job a state is of (also an end read again later)
    if ($JobId) { $body['job'] = $JobId }
    if ($attrs) {
        foreach ($key in $attrs.Keys) { $body[$key] = $attrs[$key] }
    }
    return (ConvertTo-Json -InputObject $body -Compress -Depth 5)
}

# $true when the broker took the state. Final states are retained.
function Send-JobState([string]$state, $attrs) {
    if (-not $mqttReady) { return $false }
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes((ConvertTo-JobStateJson $state $attrs))
        Publish-MqttRetained -BrokerHost $mqttHost -Port $mqttPort -ClientId $mqttClientId `
            -Username $mqttCreds.Username -Password $mqttCreds.Password -Topic $mqttJobTopic -PayloadBytes $bytes -Qos 1 -Retain ($mqttRetainedStates -contains $state)
        return $true
    } catch {
        # Only log the error message itself, never the CONNECT payload or credentials.
        Write-Log "Warning: MQTT state update failed: $($_.Exception.Message)"
        return $false
    }
}

# The end of the job (done or done_with_errors), tried for about a minute. If MQTT stays down, the state
# is kept in install_final.json and install.lock stays, so report.ps1 sends it later.
$script:jobEnded = $false
$script:endPending = $false
function Send-JobEnd([string]$state, $result) {
    $script:jobEnded = $true
    if (-not $mqttReady) { return }
    foreach ($pause in @(0, 5, 15, 30)) {
        if ($pause) { Start-Sleep -Seconds $pause }
        if (Send-JobState $state $result) { return }
    }
    try {
        [System.IO.File]::WriteAllText($finalFile, (ConvertTo-JobStateJson $state $result), (New-Object System.Text.UTF8Encoding $false))
        $script:endPending = $true
        Write-Log "End state '$state' not sent, kept for report.ps1 ($finalFile)."
    } catch {
        Write-Log "Warning: end state '$state' neither sent nor kept: $($_.Exception.Message)"
    }
}

# Sends the end state the last job could not send (install_final.json), then forgets it
function Send-PendingEnd {
    $text = Read-SharedTextFile $finalFile
    if ($text -and $mqttReady) {
        try {
            Publish-MqttRetained -BrokerHost $mqttHost -Port $mqttPort -ClientId $mqttClientId `
                -Username $mqttCreds.Username -Password $mqttCreds.Password -Topic $mqttJobTopic `
                -PayloadBytes ([System.Text.Encoding]::UTF8.GetBytes($text.Trim())) -Qos 1 -Retain $true
            Write-Log 'End state of the last job sent late.'
        } catch {
            Write-Log "Warning: end state of the last job not sent: $($_.Exception.Message)"
        }
    }
    Remove-Item -LiteralPath $finalFile -Force -ErrorAction SilentlyContinue
}

# Results per appid for the final message (the HA push names the games)
$resOk = New-Object System.Collections.ArrayList
$resFailed = New-Object System.Collections.ArrayList
# The Steam client was handling the game already
$resSkipped = New-Object System.Collections.ArrayList
# {appid, needed_gb, free_gb, drive}
$resNoSpace = New-Object System.Collections.ArrayList
# Not started because a game was running
$resGameRunning = New-Object System.Collections.ArrayList
# Installed for the first time (for the push text)
$resNew = New-Object System.Collections.ArrayList
$script:handled = @{}
$script:position = 0
$script:gameIsNew = $false

# The result of one game: log line, state on steam/job and the list for the final message. Kinds: ok,
# failed, skipped (the Steam client handles it already), no_space, game_running. $detail is for the log.
function Complete-Game([string]$appId, [string]$kind, [string]$reason = '', [string]$code = '', $space = $null, [string]$detail = '') {
    if ($script:handled.ContainsKey($appId)) {
        if ($reason) { Write-Log "appid=${appId}: $reason" }
        return
    }
    $script:handled[$appId] = $true
    $attrs = [ordered]@{ appid = $appId; position = $script:position; count = $appIds.Count }
    $logReason = if ($detail) { "$reason ($detail)" } else { $reason }
    if ($kind -eq 'ok') {
        [void]$resOk.Add($appId)
        if ($script:gameIsNew) { [void]$resNew.Add($appId) }
        Write-Log "OK: appid=$appId installed/updated by the Steam client."
        $attrs['percent'] = 100
        [void](Send-JobState 'installed' $attrs)
        return
    }
    if ($kind -eq 'failed') {
        [void]$resFailed.Add($appId)
        Write-Log "ERROR: appid=$appId - $logReason"
        $state = 'error'
    } else {
        if ($kind -eq 'no_space') { [void]$resNoSpace.Add($space) }
        elseif ($kind -eq 'game_running') { [void]$resGameRunning.Add($appId) }
        else { [void]$resSkipped.Add($appId) }
        Write-Log "Skipped: appid=$appId - $logReason."
        $state = 'skipped'
    }
    $attrs['reason'] = $reason
    $attrs['reason_code'] = $code
    if ($space) {
        foreach ($k in @('needed_gb', 'free_gb', 'drive')) { $attrs[$k] = $space[$k] }
    }
    [void](Send-JobState $state $attrs)
}

function Get-JobResult {
    $result = [ordered]@{
        count        = $appIds.Count
        ok           = @($resOk.ToArray())
        failed       = @($resFailed.ToArray())
        skipped      = @($resSkipped.ToArray())
        no_space     = @($resNoSpace.ToArray())
        game_running = @($resGameRunning.ToArray())
        new          = @($resNew.ToArray())
    }
    if ($resGameRunning.Count -gt 0) {
        # The game that runs now: Home Assistant waits for it to close, also while the agent does not
        # report it yet (it reads RunningAppID once a round)
        $running = Get-SteamRunningAppId
        if ($running -ne 0) { $result['game_appid'] = [string]$running }
    }
    return $result
}

# --- manifests, app info, space -------------------------------------------------------------------

function Get-AcfNumber($manifestPath, $key) {
    $v = Get-AcfValue $manifestPath $key
    if ($v -match '^\d+$') { return [int64]$v }
    return $null
}

function Get-AppStateFlags($manifestPath) {
    return (Get-AcfNumber $manifestPath 'StateFlags')
}

function Get-InstalledSize($manifestPath) {
    $v = Get-AcfNumber $manifestPath 'SizeOnDisk'
    if ($null -eq $v) { return [int64]0 }
    return $v
}

function ConvertFrom-Vdf([string]$text) {
    # Minimal VDF reader (key/value in quotes, blocks in {}), returns hashtables
    $root = @{}
    $stack = New-Object System.Collections.Stack
    $cur = $root
    $key = $null
    foreach ($m in [regex]::Matches($text, '"((?:[^"\\]|\\.)*)"|\{|\}')) {
        if ($m.Value -eq '{') {
            $block = @{}
            if ($null -ne $key) { $cur[$key] = $block }
            $stack.Push($cur); $cur = $block; $key = $null
        } elseif ($m.Value -eq '}') {
            if ($stack.Count -eq 0) { break }
            $cur = $stack.Pop(); $key = $null
        } elseif ($null -eq $key) {
            $key = $m.Groups[1].Value
        } else {
            $cur[$key] = $m.Groups[1].Value; $key = $null
        }
    }
    return $root
}

function Get-DepotSize($app, $installedDepots, [bool]$withDlc) {
    # Size of the installation: sum of manifests.public.size.
    # Installed depots (from the manifest) always count, so language and DLC fit on updates.
    # Otherwise only Windows depots without another language, DLC depots only with $withDlc
    # (anonymously the DLC ownership is not visible). $null = unknown.
    if (-not $app -or -not ($app['depots'] -is [hashtable])) { return $null }
    [int64]$sum = 0
    $found = $false
    foreach ($id in $app['depots'].Keys) {
        if ($id -notmatch '^\d+$') { continue }
        $d = $app['depots'][$id]
        if (-not ($d -is [hashtable])) { continue }
        if ($installedDepots -notcontains $id) {
            if ($d.ContainsKey('dlcappid') -and -not $withDlc) { continue }
            $cfg = $d['config']
            if ($cfg -is [hashtable]) {
                if ($cfg['oslist'] -and ($cfg['oslist'] -split ',') -notcontains 'windows') { continue }
                if ($cfg['language'] -and $cfg['language'] -ne 'english') { continue }
                if ($cfg['lowviolence'] -eq '1') { continue }
            }
        }
        $pub = if ($d['manifests'] -is [hashtable]) { $d['manifests']['public'] } else { $null }
        if ($pub -is [hashtable] -and $pub['size'] -match '^\d+$') {
            $sum += [int64]$pub['size']
            $found = $true
        }
    }
    if ($found) { return $sum }
    return $null
}

function Get-InstalledDepots($manifestPath) {
    $text = Read-SharedTextFile $manifestPath
    if (-not $text) { return @() }
    try {
        $st = (ConvertFrom-Vdf $text)['AppState']
        if ($st -is [hashtable] -and $st['InstalledDepots'] -is [hashtable]) { return @($st['InstalledDepots'].Keys) }
    } catch { }
    return @()
}

function Test-DriveReady($path) {
    # A library on a drive that is not there (USB disk unplugged): DriveInfo would only give $null
    try { return ([System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($path))).IsReady } catch { return $false }
}

# The test value of space_test_free_gb.txt for the drive of $root, or $null (Get-SteamTestFreeGb)
function Get-TestFreeGb($root) { return (Get-SteamTestFreeGb $spaceTestFile $root) }

function Get-FreeBytes($root) { return (Get-SteamFreeBytes $spaceTestFile $root) }

function ConvertTo-Hashtable($o) {
    # JSON objects from Invoke-RestMethod to hashtables (Get-DepotSize expects the VDF shape)
    if ($null -eq $o) { return $null }
    if ($o -is [System.Management.Automation.PSCustomObject]) {
        $h = @{}
        foreach ($p in $o.PSObject.Properties) { $h[$p.Name] = ConvertTo-Hashtable $p.Value }
        return $h
    }
    if ($o -is [array]) { return @($o | ForEach-Object { ConvertTo-Hashtable $_ }) }
    return [string]$o
}

function Get-AppInfoOnline($appId) {
    # Install folder, name and depots from api.steamcmd.net (the same source as the update check
    # of the integration). Steam does not fill in installdir by itself for a hand-made manifest.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    for ($try = 1; $try -le 2; $try++) {
        try {
            $r = Invoke-RestMethod -Uri ("https://api.steamcmd.net/v1/info/{0}" -f $appId) -TimeoutSec 30 -UseBasicParsing
            $app = ConvertTo-Hashtable $r.data.$appId
            if ($app -is [hashtable] -and $app['config'] -is [hashtable] -and $app['config']['installdir']) {
                $name = if ($app['common'] -is [hashtable]) { $app['common']['name'] } else { $null }
                return [pscustomobject]@{ InstallDir = $app['config']['installdir']; Name = $name; App = $app }
            }
        } catch {
            Write-Log "Warning: api.steamcmd.net for appid=$appId failed (try $try): $($_.Exception.Message)"
        }
        if ($try -lt 2) { Start-Sleep -Seconds 5 }
    }
    return $null
}

# --- Steam client ---------------------------------------------------------------------------------

# Set while this job has shut the client down and not started it again (see the finally block)
$script:steamStoppedByJob = $false
# UTC time of the last client start by this job (to wait for its log-on before steam://validate)
$script:steamStartedUtc = $null

function Stop-SteamClientForJob {
    # $true when the client is not running (any more)
    if (-not (Test-SteamClientRunning)) { return $true }
    $script:steamStoppedByJob = $true
    return (Stop-SteamClient 90)
}

function Start-SteamClientForJob {
    if (Start-SteamClient) { $script:steamStartedUtc = [DateTime]::UtcNow }
    $script:steamStoppedByJob = $false
}

# Why the game cannot be started now, or $null
function Get-GameRunningReason($appId, [bool]$wasInstalled) {
    $running = Get-SteamRunningAppId
    if ($running -eq [int64]$appId) { return 'the game is running' }
    # First install: Steam reads new manifests only when it starts
    if (-not $wasInstalled -and $running -ne 0) { return "a game is running (appid=$running), the Steam client cannot restart for a first install" }
    return $null
}

function Wait-SteamLoggedOn($since, $timeoutSec) {
    # The client is ready for steam:// commands once connection_log shows "[Logged On" after $since
    # (local time, like the log). The wait itself is timed with a stopwatch (daylight saving).
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $timeoutSec) {
        Start-Sleep -Seconds 3
        $text = Read-FileTail $steamClientConnLog 65536
        if (-not $text) { continue }
        foreach ($line in ($text -split "`n")) {
            if ($line -match '^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] \[Logged On') {
                if ([datetime]::ParseExact($matches[1], 'yyyy-MM-dd HH:mm:ss', $inv) -ge $since) { return $true }
            }
        }
    }
    return $false
}

function Read-NewLogLines($path, [ref]$offset) {
    # Complete lines appended to a Steam log since $offset (Read-SteamLogLines in mqtt_publish.ps1)
    return (Read-SteamLogLines $path $offset)
}

function Get-SteamIo {
    # Bytes written/read by steam.exe. Steam does not write the download progress anywhere
    # (the manifest stays at 0 until the end), but the bytes it writes match BytesToStage.
    $p = Get-CimInstance Win32_Process -Filter "Name='steam.exe'" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $p) { return $null }
    return [pscustomobject]@{ ProcessId = [int]$p.ProcessId; Write = [int64]$p.WriteTransferCount; Read = [int64]$p.ReadTransferCount }
}

function Convert-ClientPhase([string]$flags) {
    # "Running Update,Downloading,Staging," etc. from content_log
    if ($flags -match 'Committing') { return 'commit' }
    if ($flags -match 'Verifying') { return 'verify' }
    if ($flags -match 'Downloading|Staging') { return 'download' }
    if ($flags -match 'Preallocating|Reconfiguring') { return 'prepare' }
    return $null
}

# --- manifests written by this script -------------------------------------------------------------

function Add-PendingCleanup($appId, $manifestPath) {
    # A manifest this script wrote but could not take back now (a game runs, Steam did not exit).
    # The next job removes it (Clear-PendingManifests), otherwise it would block the shutdown for ever.
    # UTF-8 without BOM: a library path can contain non-ASCII characters
    try { [System.IO.File]::AppendAllText($pendingCleanupFile, ("{0}|{1}`r`n" -f $appId, $manifestPath), (New-Object System.Text.UTF8Encoding $false)) } catch { }
}

function Save-PendingCleanup($entries) {
    if (@($entries).Count -eq 0) {
        Remove-Item -LiteralPath $pendingCleanupFile -Force -ErrorAction SilentlyContinue
        return
    }
    try { [System.IO.File]::WriteAllLines($pendingCleanupFile, [string[]]@($entries), (New-Object System.Text.UTF8Encoding $false)) } catch { }
}

function Remove-ManifestFiles($manifestPath, $appId) {
    Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
    $dl = [System.IO.Path]::Combine([System.IO.Path]::GetDirectoryName($manifestPath), 'downloading')
    Remove-Item -LiteralPath ([System.IO.Path]::Combine($dl, $appId)) -Recurse -Force -ErrorAction SilentlyContinue
    # the wildcard only in -Filter, the folder literal (a library path may contain [ ])
    Get-ChildItem -LiteralPath $dl -Filter ("state_{0}_*" -f $appId) -File -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
}

function Remove-ClientManifest($manifestPath, $appId) {
    # Takes back a manifest written by this script that Steam never installed, otherwise it hangs
    # as "update started" for ever (and the agent reports a download that blocks the shutdown).
    # Steam keeps a known manifest in memory and writes it again on exit, so a running client is
    # closed first and started again; one that does not run stays closed.
    # Returns $true when removed; otherwise the next job removes it.
    $flags = Get-AppStateFlags $manifestPath
    if ($null -eq $flags -or ($flags -band $SteamAppState.FullyInstalled) -ne 0) { return $true }
    if (-not (Test-SteamClientRunning)) {
        Remove-ManifestFiles $manifestPath $appId
        Write-Log "Unused manifest of appid=$appId removed (Steam client not running)."
        return $true
    }
    if ((Get-SteamRunningAppId) -ne 0) {
        Write-Log "Warning: appid=$appId - a game is running, the unused manifest stays until the next job ($manifestPath)."
        Add-PendingCleanup $appId $manifestPath
        return $false
    }
    if (-not (Stop-SteamClientForJob)) {
        Write-Log "Warning: appid=$appId - the Steam client did not exit, the unused manifest stays until the next job ($manifestPath)."
        Add-PendingCleanup $appId $manifestPath
        return $false
    }
    Remove-ManifestFiles $manifestPath $appId
    Start-Sleep -Seconds 2
    Start-SteamClientForJob
    Write-Log "Unused manifest of appid=$appId removed, Steam client started again."
    return $true
}

function Clear-PendingManifests {
    # Removes manifests left behind by an earlier job (see Add-PendingCleanup) while no game runs.
    # Only a manifest Steam never took on is touched: still as written, nothing downloaded.
    # One that Steam has started to download in the meantime stays (Steam goes on with it).
    $text = Read-SharedTextFile $pendingCleanupFile
    if ($null -eq $text) { return }
    $entries = @($text -split "`r?`n" | Where-Object { $_ -match '^\d+\|' })
    # Entries on a drive that is not there now stay for a later job
    $keep = @($entries | Where-Object { -not (Test-DriveReady ($_ -split '\|', 2)[1]) })
    $todo = @($entries | Where-Object {
        $parts = $_ -split '\|', 2
        (Test-DriveReady $parts[1]) -and
            (Get-AppStateFlags $parts[1]) -eq $newManifestFlags -and -not (Get-AcfNumber $parts[1] 'BytesToDownload') -and
            -not (Test-Path -LiteralPath ([System.IO.Path]::Combine([System.IO.Path]::GetDirectoryName($parts[1]), 'downloading', $parts[0])))
    })
    if ($todo.Count -eq 0) {
        Save-PendingCleanup $keep
        return
    }
    if ((Get-SteamRunningAppId) -ne 0) {
        Write-Log "Left-behind manifests ($($todo.Count)) stay, a game is running."
        return
    }
    $wasRunning = Test-SteamClientRunning
    if (-not (Stop-SteamClientForJob)) {
        Write-Log "Left-behind manifests ($($todo.Count)) stay, the Steam client did not exit."
        return
    }
    foreach ($e in $todo) {
        $parts = $e -split '\|', 2
        Remove-ManifestFiles $parts[1] $parts[0]
        Write-Log "Left-behind manifest of appid=$($parts[0]) removed ($($parts[1]))."
    }
    Save-PendingCleanup $keep
    if ($wasRunning) {
        Start-Sleep -Seconds 2
        Start-SteamClientForJob
    }
}

# --- new library ----------------------------------------------------------------------------------

# The Steam account of the installed games (LastOwner of a manifest) for a hand-made manifest: looked
# for in $steamapps first, then in every other library ('' when none has one)
function Get-ManifestOwner([string]$steamapps) {
    $dirs = @($steamapps)
    try {
        foreach ($li in @(Get-SteamLibraryInfo $seCfg.SteamRoot)) {
            if ($li.Ready) { $dirs += [System.IO.Path]::Combine($li.Path, 'steamapps') }
        }
    } catch { }
    foreach ($d in $dirs) {
        foreach ($m in @(Get-ChildItem -LiteralPath $d -Filter 'appmanifest_*.acf' -ErrorAction SilentlyContinue)) {
            $o = Get-AcfValue $m.FullName 'LastOwner'
            if ($o -match '^[1-9]\d+$') { return $o }
        }
    }
    return ''
}

# After the client start: did Steam take the library New-SteamLibrary added ($Library)? Returns
# 'ok', 'refused' or 'lost' (Wait-SteamLibraryAccepted in mqtt_publish.ps1; $Offset is a copy).
function Wait-NewLibraryAccepted($Library, [int64]$Offset) {
    $r = Wait-SteamLibraryAccepted $seCfg.SteamRoot $Library $Offset $libraryAcceptSec
    if ($r.Note) { Write-Log $r.Note }
    return $r.State
}

# Takes back a new library Steam did not take ($State 'refused') or after which Steam lost libraries
# ($State 'lost'): closes the client, puts back the libraryfolders.vdf backups and starts the client
# again. Both remove the manifest; 'refused' also removes what New-SteamLibrary created, 'lost' leaves
# the new folder (it is no library any more after the restore). A restore that cannot be done leaves
# the pending marker (Set-SteamLibraryRestorePending). Returns the result of Invoke-ClientInstall.
function Undo-NewLibrary($Library, [string]$State, $manifestPath, $appId) {
    $code = if ($State -eq 'lost') { 'library_config_lost' } else { 'library_not_created' }
    $backups = @($Library.Files | Where-Object { $_.Backup } | ForEach-Object { $_.Backup }) -join ', '
    $reason = if ($State -eq 'lost') { 'Steam dropped libraries from before after the new library was added' } else { "Steam did not load the new library $($Library.Path)" }
    if (-not (Stop-SteamClientForJob)) {
        # Steam writes its libraries back when it exits: nothing is restored under a running client
        $reason += "; the Steam client did not exit, nothing restored (backups: $backups)"
        Set-SteamLibraryRestorePending $Library 'the Steam client did not exit'
        Add-PendingCleanup $appId $manifestPath
        return @{ Result = 'error'; Reason = $reason; Code = $code }
    }
    if (Restore-SteamLibraryConfig $Library) {
        Write-Log "libraryfolders.vdf restored from the backups ($backups)."
        $reason += '; libraryfolders.vdf restored'
    } else {
        $reason += "; libraryfolders.vdf NOT restored, put back by hand from $backups"
        Set-SteamLibraryRestorePending $Library 'the backups could not be copied back'
    }
    if ($State -ne 'lost') {
        [void](Remove-ClientManifest $manifestPath $appId)
        if (Remove-SteamLibraryFolder $Library) { Write-Log "New library folder $($Library.Path) removed." }
        else { $reason += "; $($Library.Path) not fully removed" }
    } else {
        # Steam is closed: the manifest goes, the folder with its libraryfolder.vdf stays
        Remove-ManifestFiles $manifestPath $appId
        Write-Log "The new library folder $($Library.Path) stays (not registered after the restore), manifest removed."
    }
    Start-Sleep -Seconds 2
    Start-SteamClientForJob
    return @{ Result = 'error'; Reason = $reason; Code = $code }
}

# --- one game -------------------------------------------------------------------------------------

# Whether Steam holds the game's update for a game that was started ('scheduler finished' with result
# Suspended): the reason 'Disabled (Suspended)' (no downloads while a game runs, Steam's default; also
# when the client exits), or none (a game start ends an update that was only queued, or a shader
# update, without one). Not 'Priority (Suspended)' or 'Shader Priority (Suspended)': another download
# goes first, and Steam goes on with this one after it.
function Test-HeldForGame([string]$result, [string]$cancel) {
    return $result -eq 'Suspended' -and ($cancel -eq '' -or $cancel -eq 'Disabled (Suspended)')
}

function Invoke-ClientInstall($appId, $appName, $installDir, $manifestPath, [bool]$wasInstalled, $position, $count, [string]$newLibraryDrive = '') {
    # Returns @{ Result = 'ok' | 'error' | 'game_running'; Reason = '...'; Code = '...' }
    # $newLibraryDrive: a first install to a drive without a library; New-SteamLibrary adds it while
    # the client is closed, and $manifestPath lies in it.
    # Elapsed times use UTC (a local clock jumps at daylight saving); log times stay local.
    # Checked again here: a game may have started during the app info call
    $why = Get-GameRunningReason $appId $wasInstalled
    if ($why) {
        return @{ Result = 'game_running'; Reason = $why; Code = 'game_running' }
    }
    $logOffset = if (Test-Path -LiteralPath $steamContentLog) { (Get-Item -LiteralPath $steamContentLog).Length } else { [int64]0 }
    $triggerUtc = [DateTime]::UtcNow
    $triggerUnix = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $createdManifest = $false

    if (-not $wasInstalled) {
        # First install: Steam reads new manifests only when it starts (the library was checked before)
        $steamapps = [System.IO.Path]::GetDirectoryName($manifestPath)
        if (-not (Stop-SteamClientForJob)) {
            return @{ Result = 'error'; Reason = 'the Steam client did not exit'; Code = 'steam_did_not_exit' }
        }
        $newLibrary = $null
        if ($newLibraryDrive) {
            # Only now: Steam is closed (a running game was ruled out above)
            try {
                $newLibrary = New-SteamLibrary $seCfg.SteamRoot $newLibraryDrive
            } catch {
                $why = $_.Exception.Message
                $code = if ($_.Exception.Data['Code']) { [string]$_.Exception.Data['Code'] } else { 'library_not_created' }
                Start-SteamClientForJob
                return @{ Result = 'error'; Reason = "no new Steam library on ${newLibraryDrive}: $why"; Code = $code }
            }
            Write-Log "New Steam library $($newLibrary.Path) added (block $($newLibrary.Index), libraryfolders.vdf backed up)."
        }
        $owner = Get-ManifestOwner $steamapps
        $lines = @('"AppState"', '{',
            ("`t`"appid`"`t`t`"{0}`"" -f $appId),
            "`t`"Universe`"`t`t`"1`"")
        # The name from api.steamcmd.net as a vdf string: no quotes or control characters, a backslash escaped
        if ($appName) { $lines += ("`t`"name`"`t`t`"{0}`"" -f (($appName -replace '["\x00-\x1F]', '') -replace '\\', '\\')) }
        # Update Required + Update Started: Steam starts the download right away
        $lines += ("`t`"StateFlags`"`t`t`"{0}`"" -f $newManifestFlags)
        $lines += ("`t`"installdir`"`t`t`"{0}`"" -f $installDir)
        if ($owner) { $lines += ("`t`"LastOwner`"`t`t`"{0}`"" -f $owner) }
        $lines += "`t`"AutoUpdateBehavior`"`t`t`"0`""
        $lines += '}'
        try {
            # Through a temporary file: a failed write never leaves an empty or cut-off manifest that
            # later jobs would take for an installed game
            Write-SteamFileAtomic $manifestPath ((New-Object System.Text.UTF8Encoding $false).GetBytes(($lines -join "`n") + "`n"))
        } catch {
            $why = "manifest not written: $($_.Exception.Message)"
            if ($newLibrary) {
                # Steam is still closed: the new library is taken back at once
                $restored = Restore-SteamLibraryConfig $newLibrary
                $removed = Remove-SteamLibraryFolder $newLibrary
                if ($restored -and $removed) { $why += "; new library $($newLibrary.Path) taken back" }
                else { $why += "; new library $($newLibrary.Path) not fully taken back" }
                if (-not $restored) { Set-SteamLibraryRestorePending $newLibrary 'the backups could not be copied back' }
            }
            Start-SteamClientForJob
            return @{ Result = 'error'; Reason = $why; Code = 'manifest_not_written' }
        }
        $createdManifest = $true
        Write-Log "Manifest written: $manifestPath (installdir='$installDir'), starting the Steam client."
        $logOffset = if (Test-Path -LiteralPath $steamContentLog) { (Get-Item -LiteralPath $steamContentLog).Length } else { [int64]0 }
        Start-Sleep -Seconds 2
        Start-SteamClientForJob
        if ($newLibrary) {
            # A copy of the offset: the game's own log lines since the start are read again below
            $accepted = Wait-NewLibraryAccepted $newLibrary $logOffset
            if ($accepted -ne 'ok') { return (Undo-NewLibrary $newLibrary $accepted $manifestPath $appId) }
            # From here the library stays, also when the download fails
            Write-Log "Steam loaded the new library $($newLibrary.Path)."
            Remove-OldSteamLibraryBackups $newLibrary
        }
        # the start timeout counts from the client start (and the library check), not from before the shutdown
        $triggerUtc = [DateTime]::UtcNow
    } else {
        # Update: steam://validate starts a pending update at once ("Priority First"), without a restart
        if (-not (Test-SteamClientRunning)) { Start-SteamClientForJob }
        if ($script:steamStartedUtc -and ([DateTime]::UtcNow - $script:steamStartedUtc).TotalSeconds -lt 300) {
            # the client was (re)started by this job just now: wait until it is logged on
            if (-not (Wait-SteamLoggedOn $script:steamStartedUtc.ToLocalTime().AddSeconds(-1) 180)) {
                return @{ Result = 'error'; Reason = 'the Steam client did not log on'; Code = 'steam_not_logged_on' }
            }
            Start-Sleep -Seconds 5
        }
        # The log is read from before the client start ($logOffset above): Steam may have begun the
        # update by itself right after starting. Only the start timeout counts from the validate.
        $triggerUtc = [DateTime]::UtcNow
        Write-SteamEngineOwnCall
        Start-Process ("steam://validate/{0}" -f $appId)
        Write-Log "Update of appid=$appId started with steam://validate."
    }

    # $runCancel: the reason of the last 'update canceled' line, until its 'scheduler finished' line
    $activity = $false; $started = $false; $finished = $null; $lastCancel = ''; $runCancel = ''; $lastResult = ''
    $failSinceUtc = $null; $suspendLogged = $false; $heldForGame = $false
    $phase = 'prepare'; $dlTotal = [int64]0; $stTotal = [int64]0; $stDone = [int64]0; $maxPct = 0.0; $verifyPct = 0.0
    $ioPid = $null; $baseW = [int64]0; $baseR = [int64]0; $accW = [int64]0; $accR = [int64]0; $lastW = [int64]0; $lastR = [int64]0
    $written = [int64]0; $readBytes = [int64]0; $lastWritten = [int64]0; $lastRead = [int64]0
    $lastActivityUtc = [DateTime]::UtcNow; $noSteamSinceUtc = $null; $revalidated = 0
    $roundUtc = [DateTime]::UtcNow
    $sizeOnDisk = Get-InstalledSize $manifestPath
    $appPattern = '(?i)appid ' + $appId + '\b'
    # Steam is on it already (e.g. started the update by itself after the client start): no start timeout
    $flagsNow = Get-AppStateFlags $manifestPath
    if ($wasInstalled -and $null -ne $flagsNow -and ($flagsNow -band $SteamAppState.Active) -ne 0) { $activity = $true }
    $idleMinutes = [int]($clientIdleTimeout / 60)

    while ($true) {
        Start-Sleep -Seconds 10
        $nowUtc = [DateTime]::UtcNow
        # The PC slept (standby, hibernation) since the last round: that time counts for no time limit
        $gap = $nowUtc - $roundUtc
        $roundUtc = $nowUtc
        if ($gap.TotalSeconds -gt $sleepGapSec) {
            $lastActivityUtc += $gap; $triggerUtc += $gap
            if ($failSinceUtc) { $failSinceUtc += $gap }
            if ($noSteamSinceUtc) { $noSteamSinceUtc += $gap }
            Write-Log ("appid={0}: no round for {1:N0} minutes (the PC slept?), the time limits leave that time out." -f $appId, $gap.TotalMinutes)
        }
        foreach ($line in (Read-NewLogLines $steamContentLog ([ref]$logOffset))) {
            if ($line -notmatch $appPattern) { continue }
            $activity = $true
            # No progress while an error waits for Steam's retry: its failing retries log lines too
            if (-not $failSinceUtc) { $lastActivityUtc = $nowUtc }
            if ($line -match 'update started : download (\d+)/(\d+),.* stage (\d+)/(\d+)') {
                $started = $true
                $dlTotal = [int64]$matches[2]
                $stTotal = [int64]$matches[4]
                # A download Steam goes on with (paused, held for a game, a job sent again) starts with
                # what is staged already: the progress counts from there, the writes from here
                $stDone = [math]::Min([int64]$matches[3], $stTotal)
                $ioPid = $null
                $accW = [int64]0; $accR = [int64]0; $lastWritten = [int64]0; $lastRead = [int64]0
                if ($stDone -gt 0) {
                    Write-Log ("appid={0}: Steam goes on with the download ({1:N2} of {2:N2} GB written, {3:N2} GB to download)." -f $appId, ($stDone / 1GB), ($stTotal / 1GB), ($dlTotal / 1GB))
                } else {
                    Write-Log ("appid={0}: Steam started the download ({1:N2} GB to download, {2:N2} GB to write)." -f $appId, ($dlTotal / 1GB), ($stTotal / 1GB))
                }
            } elseif ($line -match 'App update changed : (.+)$') {
                $change = $matches[1]
                # Steam works on the game (again)
                if ($change.StartsWith('Running Update')) { $heldForGame = $false }
                $p = Convert-ClientPhase $change
                if ($p) { $phase = $p }
            } elseif ($line -match 'update canceled : (.+)$') {
                $lastCancel = $matches[1].Trim()
                $runCancel = $lastCancel
            } elseif ($line -match 'scheduler finished : removed from schedule \(result ([^,)]+)') {
                $res = $matches[1].Trim()
                $heldForGame = Test-HeldForGame $res $runCancel
                $runCancel = ''
                if ($res -eq 'No Error') {
                    $finished = $res
                } else {
                    # Steam retries by itself (~30 s); only an error it does not get past ends the job.
                    # Suspended: held for a game (see below), the client exits, or another download first.
                    $lastResult = $res
                    if (-not $failSinceUtc) {
                        $failSinceUtc = $nowUtc
                        # Steam got past it only with real progress from here (see below)
                        $lastWritten = $written; $lastRead = $readBytes
                        Write-Log "appid=${appId}: Steam reports '$res' ($lastCancel), waiting for Steam to retry."
                    }
                }
            } elseif ($line -match 'staying in schedule \(result ([^,)]+)') {
                # Kept queued. Suspended: held for a game (see below), the client exits, another
                # download goes first (Steam goes on with this one after it), or paused
                $res = $matches[1].Trim()
                $heldForGame = Test-HeldForGame $res $runCancel
                $runCancel = ''
                if (-not $suspendLogged) {
                    $suspendLogged = $true
                    Write-Log "appid=${appId}: Steam keeps the update queued ($res, $lastCancel)."
                }
            }
        }

        $io = Get-SteamIo
        if ($io) {
            $noSteamSinceUtc = $null
            if (-not $ioPid) { $ioPid = $io.ProcessId; $baseW = $io.Write; $baseR = $io.Read }
            elseif ($ioPid -ne $io.ProcessId) {
                # the client restarted itself: keep what the old process did, the new one counts from 0
                $accW += $lastW - $baseW; $accR += $lastR - $baseR
                $ioPid = $io.ProcessId; $baseW = [int64]0; $baseR = [int64]0
            }
            $lastW = $io.Write; $lastR = $io.Read
            $written = $accW + $lastW - $baseW
            $readBytes = $accR + $lastR - $baseR
        } elseif (-not $noSteamSinceUtc) {
            $noSteamSinceUtc = $nowUtc
        }
        # Real progress only: idle Steam writes a few KB now and then, a download many MB, and a check
        # of the files (also a re-check after the download started) reads many MB. Only such progress
        # shows that Steam got past an error (not a new 'update started' or phase of a retry).
        if (($written - $lastWritten) -ge $clientProgressBytes) { $lastActivityUtc = $nowUtc; $lastWritten = $written; $failSinceUtc = $null }
        if (($readBytes - $lastRead) -ge $clientProgressBytes) { $lastActivityUtc = $nowUtc; $lastRead = $readBytes; $failSinceUtc = $null }

        # Backstop for a missed end line: the manifest says installed, nothing pending, and updated since the start
        if (-not $finished -and $activity) {
            $f = Get-AppStateFlags $manifestPath
            $updatedAt = Get-AcfNumber $manifestPath 'LastUpdated'
            if ($null -ne $f -and ($f -band $SteamAppState.FullyInstalled) -ne 0 -and ($f -band $SteamAppState.UpdateRequired) -eq 0 -and
                ($f -band ($SteamAppState.Active -bor $SteamAppState.Queued)) -eq 0 -and $updatedAt -ge $triggerUnix) {
                Write-Log "appid=${appId}: manifest shows the game installed (StateFlags=$f), no end line seen."
                $finished = 'No Error'
            }
        }

        if ($finished) {
            $flags = Get-AppStateFlags $manifestPath
            for ($w = 0; $w -lt 6 -and ($null -eq $flags -or ($flags -band $SteamAppState.FullyInstalled) -eq 0); $w++) {
                Start-Sleep -Seconds 5
                $flags = Get-AppStateFlags $manifestPath
            }
            if ($null -ne $flags -and ($flags -band $SteamAppState.FullyInstalled) -ne 0 -and ($flags -band $SteamAppState.UpdateRequired) -eq 0) {
                return @{ Result = 'ok'; Reason = ''; Code = '' }
            }
            if ($null -ne $flags -and ($flags -band $SteamAppState.UpdateRequired) -ne 0 -and $revalidated -lt 2) {
                # verified, but the update itself is still pending: ask once more
                $revalidated++
                $finished = $null
                Write-Log "appid=$appId still needs an update after the check (StateFlags=$flags), steam://validate again."
                Write-SteamEngineOwnCall
                Start-Process ("steam://validate/{0}" -f $appId)
                continue
            }
            return @{ Result = 'error'; Reason = "Steam finished, but the game is not installed (StateFlags=$flags)"; Code = 'not_installed_after' }
        }

        if ($heldForGame) {
            # Steam holds downloads while a game runs (its default) and goes on after it: the game waits
            # for that in Home Assistant, which sends it again once the game is closed. Held behind
            # another download the job waits on: that download's bytes count as progress (above).
            $running = Get-SteamRunningAppId
            if ($running -ne 0) {
                $why = if ($running -eq [int64]$appId) { 'the game was started' } else { "a game was started (appid=$running)" }
                return @{ Result = 'game_running'; Reason = "$why, Steam holds the download"; Code = 'game_running' }
            }
        }
        if ($failSinceUtc -and ($nowUtc - $failSinceUtc).TotalSeconds -gt $clientRetryTimeout) {
            $reason = "Steam: $lastResult"
            if ($lastCancel) { $reason += " ($lastCancel)" }
            if ($createdManifest -and -not $started) {
                if (-not (Remove-ClientManifest $manifestPath $appId)) { $reason += '; manifest left behind, removed by the next job' }
            } elseif ($started) {
                # the partial download stays: Steam keeps it queued and goes on by itself
                $reason += '; Steam keeps the download queued'
            }
            return @{ Result = 'error'; Reason = $reason; Code = 'steam_error' }
        }
        if (-not $activity -and ($nowUtc - $triggerUtc).TotalSeconds -gt $clientStartTimeout) {
            $reason = 'the Steam client did not start the download (game not owned, or Steam offline)'
            if ($createdManifest -and -not (Remove-ClientManifest $manifestPath $appId)) { $reason += '; manifest left behind, removed by the next job' }
            return @{ Result = 'error'; Reason = $reason; Code = 'download_not_started' }
        }
        if ($noSteamSinceUtc -and ($nowUtc - $noSteamSinceUtc).TotalSeconds -gt 120) {
            # a manifest Steam never touched is taken back (Steam is closed, so no restart)
            if ($createdManifest -and -not $activity) { [void](Remove-ClientManifest $manifestPath $appId) }
            return @{ Result = 'error'; Reason = 'the Steam client is not running'; Code = 'steam_not_running' }
        }
        if (($nowUtc - $lastActivityUtc).TotalSeconds -gt $clientIdleTimeout) {
            $reason = "no progress for $idleMinutes minutes"
            if ($lastCancel) { $reason += " ($lastCancel)" }
            return @{ Result = 'error'; Reason = $reason; Code = 'no_progress' }
        }

        $attrs = [ordered]@{ appid = $appId; position = $position; count = $count }
        if ($started -and $stTotal -gt 0) {
            # round down: 100 % is only reported as 'installed'
            $pct = [math]::Floor([math]::Min(0.999, ($stDone + $written) / $stTotal) * 1000) / 10
            if ($pct -gt $maxPct) { $maxPct = $pct }
            $attrs['downloaded_gb'] = [math]::Round(($dlTotal * $maxPct / 100) / 1GB, 2)
            $attrs['total_gb'] = [math]::Round($dlTotal / 1GB, 2)
        }
        $attrs['phase'] = $phase
        if ($phase -eq 'download') {
            $attrs['phase_percent'] = $maxPct
        } elseif ($phase -eq 'verify' -and $sizeOnDisk -gt 0 -and -not $started) {
            # a check before the download reads the whole game
            $v = [math]::Floor([math]::Min(0.999, $readBytes / $sizeOnDisk) * 1000) / 10
            if ($v -gt $verifyPct) { $verifyPct = $v }
            $attrs['phase_percent'] = $verifyPct
        } else {
            $attrs['phase_percent'] = 0
        }
        $attrs['updated'] = Format-SteamEngineUtc $lastActivityUtc
        $attrs['percent'] = $maxPct
        # Sent every round, also unchanged (Home Assistant ignores a state that did not change): only
        # end states are retained, so after a restart of Home Assistant or an MQTT reconnect the running
        # job shows again within seconds. Progress is judged by phase, percent and 'updated'.
        [void](Send-JobState 'downloading' $attrs)
    }
}

function Resolve-NewGameLibrary {
    # Steam's default library (Get-SteamDefaultLibrary), read before this job stops the Steam client
    # (Steam rewrites localconfig.vdf when it exits) and kept for all games of the job. When the star
    # cannot be read, the main Steam folder, as Steam itself would do.
    try {
        $d = Get-SteamDefaultLibrary $seCfg.SteamRoot @(Get-SteamLibraryInfo $seCfg.SteamRoot)
    } catch {
        $script:newGameLibraryProblem = $_.Exception.Message
        Write-Log "Warning: no library for first installs: $($script:newGameLibraryProblem)"
        return
    }
    if (-not $d.Library) {
        $script:newGameLibraryProblem = 'no Steam library found'
        Write-Log "Warning: no library for first installs: $($script:newGameLibraryProblem)"
        return
    }
    $script:newGameLibrary = $d.Library.Path
    if ($d.FromStar) {
        Write-Log "Library for first installs: $($script:newGameLibrary) (Steam's default library, index $($d.Index))."
    } else {
        Write-Log "Library for first installs: $($script:newGameLibrary) (main Steam folder: $($d.Problem))."
    }
}

function Install-Game([string]$appId) {
    # The cheap local checks first, then the one web call (app info), then the space check
    $script:gameIsNew = $false
    # A drive letter when the game's target drive gets a new library
    $newLibraryDrive = ''
    try {
        $libInfos = @(Get-SteamLibraryInfo $seCfg.SteamRoot)
    } catch {
        # Without the list of libraries neither an installed game nor a library for a new one is known
        Complete-Game $appId 'failed' "libraryfolders.vdf cannot be read: $($_.Exception.Message)" 'library_unreadable'
        return
    }
    $where = Find-SteamGameLibrary $libInfos $appId
    if ($where.OfflineLibrary) {
        # Steam lists the game for a library whose drive is not there (USB disk unplugged): no second install elsewhere
        Complete-Game $appId 'failed' "the drive of the library $($where.OfflineLibrary) is not available" 'drive_unavailable' -detail 'the game is installed there'
        return
    }
    $wasInstalled = [bool]$where.Manifest
    if ($wasInstalled) {
        # An installed game stays in its library (a target drive is ignored)
        $gameLibrary = $where.Library
        $manifestPath = $where.Manifest
        if ($script:targets.ContainsKey($appId)) { Write-Log "appid=$appId is installed in $gameLibrary, the target drive $($script:targets[$appId]) is ignored." }
        # A first install Steam has not finished yet (held for a game, cut off, sent again) still
        # counts as new: Steam never had the game fully installed
        $flagsBefore = Get-AppStateFlags $manifestPath
        if ($null -ne $flagsBefore -and ($flagsBefore -band $SteamAppState.FullyInstalled) -eq 0) { $script:gameIsNew = $true }
    } elseif ($script:targets.ContainsKey($appId)) {
        # A first install to the drive chosen in HA: the library there by the rule of the drives list
        # (Get-SteamDriveLibrary), never another drive. A suitable drive without any library gets a
        # new one, <letter>\SteamLibrary (Invoke-ClientInstall). The libraries are read again for
        # every game, so the next game of the job to that drive finds the library the first one added.
        $letter = $script:targets[$appId]
        if (-not (Test-DriveReady ($letter + '\'))) {
            Complete-Game $appId 'failed' "the target drive $letter is not available" 'drive_unavailable'
            return
        }
        $lib = Get-SteamDriveLibrary $libInfos $letter $(if ($script:newGameLibrary) { $script:newGameLibrary } else { '' })
        if ($lib) {
            $gameLibrary = $lib.Path
            Write-Log "Library for appid=${appId}: $gameLibrary (target drive $letter)."
        } else {
            $dead = @($libInfos | Where-Object { (Get-SteamDriveLetter $_.Path) -eq $letter }) | Select-Object -First 1
            if ($dead) {
                # Steam lists a library there whose folder is gone: not replaced by a second one
                Complete-Game $appId 'failed' "the library $($dead.Path) on the target drive $letter has no steamapps folder (see libraryfolders.vdf)" 'not_a_library'
                return
            }
            # The folder first (the more precise reason), then the rule of the drives list without its
            # minimum free space (the space check decides)
            $gameLibrary = $letter + '\SteamLibrary'
            $problem = Get-SteamLibraryFolderProblem $gameLibrary
            if ($problem) {
                Complete-Game $appId 'failed' "the target drive $letter cannot take a Steam library" 'drive_not_suitable' -detail $problem
                return
            }
            if (-not (@(Get-SteamCandidateDrives 0) | Where-Object { $_.Drive -eq $letter })) {
                Complete-Game $appId 'failed' "the target drive $letter cannot take a Steam library" 'drive_not_suitable' -detail 'it must be a fixed, ready NTFS drive'
                return
            }
            $newLibraryDrive = $letter
            Write-Log "Library for appid=${appId}: new library $gameLibrary (target drive $letter has none yet)."
        }
        $manifestPath = [System.IO.Path]::Combine($gameLibrary, 'steamapps', ("appmanifest_{0}.acf" -f $appId))
        $script:gameIsNew = $true
    } else {
        # A first install goes to Steam's default library (the star), found at the start of the job.
        # Its drive not there: no first install, never silently to another drive.
        if (-not $script:newGameLibrary) {
            Complete-Game $appId 'failed' "no library for first installs: $($script:newGameLibraryProblem)" 'library_unreadable'
            return
        }
        $gameLibrary = $script:newGameLibrary
        if (-not (Test-DriveReady $gameLibrary)) {
            Complete-Game $appId 'failed' "the drive of the library $gameLibrary is not available" 'drive_unavailable'
            return
        }
        $lib = @($libInfos | Where-Object { $_.Path -ieq $gameLibrary }) | Select-Object -First 1
        if (-not $lib -or -not $lib.Ready) {
            Complete-Game $appId 'failed' "the default library '$gameLibrary' has no steamapps folder (see libraryfolders.vdf)" 'not_a_library'
            return
        }
        $manifestPath = [System.IO.Path]::Combine($gameLibrary, 'steamapps', ("appmanifest_{0}.acf" -f $appId))
        $script:gameIsNew = $true
    }

    # Steam works on the game right now: leave it to Steam. Paused or queued alone is no reason,
    # steam://validate starts it at once.
    $flags = Get-AppStateFlags $manifestPath
    if ($null -ne $flags -and ($flags -band $SteamAppState.Active) -ne 0) {
        Complete-Game $appId 'skipped' 'the Steam client is handling this game already' 'steam_busy' -detail "StateFlags=$flags"
        return
    }
    $why = Get-GameRunningReason $appId $wasInstalled
    if ($why) {
        Complete-Game $appId 'game_running' $why 'game_running'
        return
    }

    Write-Log "Finding install folder and size for appid=$appId ..."
    $appInfo = Get-AppInfoOnline $appId
    $installDir = $null
    $appName = $null
    if ($appInfo) {
        $installDir = $appInfo.InstallDir
        $appName = $appInfo.Name
        # A first install writes installdir into the manifest: only a plain folder name (same rule as a move)
        if (-not $wasInstalled -and -not (Test-SteamPlainFolderName $installDir)) {
            Complete-Game $appId 'failed' 'install folder unknown' 'install_folder_unknown' -detail "installdir '$installDir' from api.steamcmd.net is not a plain folder name"
            return
        }
    } elseif ($wasInstalled) {
        # An update needs no app info (Steam knows the game); only the space check is left out
        $installDir = Get-AcfValue $manifestPath 'installdir'
        Write-Log "Warning: appid=$appId - no app info from api.steamcmd.net, updating anyway (installdir='$installDir' from the manifest), size unknown."
    } else {
        Complete-Game $appId 'failed' 'install folder unknown' 'install_folder_unknown' -detail 'no app info from api.steamcmd.net'
        return
    }
    if (-not (Test-DriveReady $gameLibrary)) {
        Complete-Game $appId 'failed' "the drive of the library $gameLibrary is not available" 'drive_unavailable'
        return
    }

    # Space check: missing part of the game size plus a reserve.
    # A first install counts without DLC (minimum); only a sure lack of space blocks.
    if ($appInfo) {
        $depots = @(Get-InstalledDepots $manifestPath)
        $sizeBytes = $null
        try { $sizeBytes = Get-DepotSize $appInfo.App $depots $false } catch { }
        if ($depots.Count -eq 0 -and $null -ne $sizeBytes) {
            $maxBytes = $null
            try { $maxBytes = Get-DepotSize $appInfo.App $depots $true } catch { }
            if ($maxBytes -gt $sizeBytes) { Write-Log ("First install appid={0}: with all DLC it would be {1:N1} GB." -f $appId, ($maxBytes / 1GB)) }
        }
        if ($null -eq $sizeBytes) {
            Write-Log "Warning: appid=$appId - size unknown (no depot sizes from api.steamcmd.net), installing without a space check."
        } else {
            $total = [int64]$sizeBytes
            $missing = [math]::Max([int64]0, $total - (Get-InstalledSize $manifestPath))
            $reserve = Get-SteamSpaceReserve $total
            $neededBytes = $missing + $reserve
            $freeBytes = Get-FreeBytes $gameLibrary
            $test = if ($null -ne (Get-TestFreeGb $gameLibrary)) { ' (TEST VALUE from space_test_free_gb.txt)' } else { '' }
            # Upper case like the drives list and the games' drive (report.ps1), so HA can compare them
            $drive = Get-SteamDriveLetter $gameLibrary
            $neededGb = [math]::Round($neededBytes / 1GB, 1)
            $freeGb = [math]::Round($freeBytes / 1GB, 1)
            Write-Log ("Space appid={0}: game {1:N1} GB, missing {2:N1} GB + reserve {3:N1} GB = {4} GB, free on {5} {6} GB{7}." -f $appId, ($total / 1GB), ($missing / 1GB), ($reserve / 1GB), $neededGb, $drive, $freeGb, $test)
            if ($freeBytes -lt $neededBytes) {
                Complete-Game $appId 'no_space' 'not enough space' 'no_space' ([ordered]@{ appid = $appId; needed_gb = $neededGb; free_gb = $freeGb; drive = $drive })
                return
            }
        }
    }

    Write-Log "Installing appid=$appId (installdir='$installDir') with the Steam client ($($script:position) of $($appIds.Count)) ..."
    [void](Send-JobState 'downloading' ([ordered]@{ appid = $appId; position = $script:position; count = $appIds.Count; percent = 0; phase = 'prepare'; phase_percent = 0 }))
    $r = Invoke-ClientInstall $appId $appName $installDir $manifestPath $wasInstalled $script:position $appIds.Count $newLibraryDrive
    if ($r.Result -eq 'ok') {
        Complete-Game $appId 'ok'
    } elseif ($r.Result -eq 'game_running') {
        Complete-Game $appId 'game_running' $r.Reason $r.Code
    } else {
        Complete-Game $appId 'failed' $r.Reason $r.Code
    }
}

function Invoke-LibraryReport {
    # The library changed: HA sees the new builds with the end of the job, not only with the agent's
    # next report. Once per job, in its own process with a time limit (a hanging report would hold
    # this job and its lock).
    if (-not (Test-Path -LiteralPath $reportScript)) { return }
    try {
        $p = Start-Process powershell.exe -NoNewWindow -PassThru `
            -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + $reportScript + '"')
        # keeps the exit code readable after the end (Windows PowerShell 5.1)
        $null = $p.Handle
        if (-not $p.WaitForExit($reportMaxSec * 1000)) {
            try { $p.Kill() } catch { }
            Write-Log "Warning: library report still running after $reportMaxSec s, stopped."
        } elseif ($p.ExitCode -ne 0) {
            Write-Log "Warning: library report ended with exit code $($p.ExitCode) (see report.log)."
        } else {
            Write-Log 'Library report updated.'
        }
    } catch {
        Write-Log "Warning: library report failed: $($_.Exception.Message)"
    }
}

# The games of $ids that another install job has (running, per install.lock) or had (one that ended
# after this process started, per install_last.txt): HA sends a command again when it does not see
# the job start in time (e.g. while an uninstall holds the job lock), so several copies of this
# script can wait for the same games. The first to get the lock takes them, the others leave them.
function Get-OpenAppIds($ids, $holder) {
    $last = $null
    try {
        if ([System.IO.File]::Exists($lastJobFile) -and [System.IO.File]::GetLastWriteTimeUtc($lastJobFile) -ge $script:processStartUtc) {
            $last = Read-SteamInstallMarker $lastJobFile
        }
    } catch { }
    return @($ids | Where-Object {
        -not ($holder.Running -and $holder.AppIds -contains $_) -and -not ($last -and $last.AppIds -contains $_)
    })
}

# Records the games of a job that ended (also one that gave up waiting), see Get-OpenAppIds
function Save-LastJob($ids) {
    try { Write-SteamInstallMarker $lastJobFile $ids } catch { Write-Log "Warning: install_last.txt not written: $($_.Exception.Message)" }
}

# The games of the command text '<appid>[@<letter>],...': AppIds (in order, each once) and Targets
# (appid -> drive letter as 'E:', only games with a target). Parts that do not fit are left out; a
# game named twice keeps its first part. Split first, then one anchored pattern per part: no
# backtracking on long or broken text.
function ConvertFrom-JobAppIds([string]$Text) {
    $ids = New-Object System.Collections.ArrayList
    $targets = @{}
    foreach ($part in ($Text -split ',')) {
        $m = [regex]::Match($part.Trim(), '^([0-9]+)(?:@([A-Za-z]))?$')
        if (-not $m.Success -or $ids.Contains($m.Groups[1].Value)) { continue }
        $id = $m.Groups[1].Value
        [void]$ids.Add($id)
        if ($m.Groups[2].Success) { $targets[$id] = $m.Groups[2].Value.ToUpperInvariant() + ':' }
    }
    return [pscustomobject]@{ AppIds = @($ids.ToArray()); Targets = $targets }
}

# --- main -----------------------------------------------------------------------------------------

$job = ConvertFrom-JobAppIds $JobAppIds
$appIds = @($job.AppIds)
# Target drive per game (first installs only), see Install-Game
$script:targets = $job.Targets
if ($appIds.Count -eq 0) {
    Write-Log "No job: no app IDs given ('$JobAppIds')."
    exit 0
}
if ($JobId -cnotmatch '^[a-z0-9]{0,16}$') {
    Write-Log "Warning: job ID ignored, not 1 to 16 of a-z and 0-9."
    $JobId = ''
}
if (-not $mqttReady) {
    Write-Log "Warning: no state messages, MQTT login not usable: $($mqttCreds.Problem) ($mqttCredFile)."
}
if ($seCfg.ConfigProblem) {
    Write-Log "Warning: $($seCfg.ConfigProblem)."
}

# When the agent started this copy for the command: a job that ended later had its games already
$processStartUtc = [DateTime]::UtcNow
try { $processStartUtc = (Get-Process -Id $PID).StartTime.ToUniversalTime() } catch { }

# One job at a time. While the job lock is held this copy waits in short steps and looks each time
# which of its games are still open (Get-OpenAppIds); it ends quietly once none are. A running install
# job is waited for as long as it runs (every game in it has its own time limits); anything else (an
# uninstall, the setup) at most $jobLockWaitSec.
$jobLock = Enter-SteamEngineJobLock 0
$waitedSec = 0
$waitLogged = $false
while (-not $jobLock) {
    $holder = Read-SteamInstallMarker $lockFile
    $open = @(Get-OpenAppIds $appIds $holder)
    if ($open.Count -eq 0) {
        # HA sent the command again (it did not see the job start): nothing to do
        Write-Log "Skipped: another install job has or had these games since this command came ($($appIds -join ', '))."
        exit 0
    }
    if (-not $holder.Running) {
        if ($waitedSec -ge $jobLockWaitSec) {
            $reason = "another Steam job still runs after $([int]($jobLockWaitSec / 60)) minutes"
            Write-Log "Stopped: $reason, not installed: $($open -join ', ')."
            $result = [ordered]@{ count = $open.Count; ok = @(); failed = $open; skipped = @(); no_space = @(); game_running = @(); new = @(); reason = $reason; reason_code = 'job_running' }
            [void](Send-JobState 'done_with_errors' $result)
            # Reported: other copies waiting for the same games leave them
            Save-LastJob $open
            exit 1
        }
        $waitedSec += $jobLockStepSec
    }
    if (-not $waitLogged) {
        $what = if ($holder.Running) { "an install job, PID $($holder.Pid)" } else { 'an uninstall or the setup' }
        Write-Log "Waiting: the job lock is held ($what); open: $($open -join ', ')."
        $waitLogged = $true
    }
    $jobLock = Enter-SteamEngineJobLock ($jobLockStepSec * 1000)
}
# Also when the lock came at once: a job that ended just before may have had some of the games
$holder = Read-SteamInstallMarker $lockFile
$open = @(Get-OpenAppIds $appIds $holder)
if ($open.Count -eq 0) {
    Write-Log "Skipped: the job that just ended had these games ($($appIds -join ', '))."
    Exit-SteamEngineJobLock $jobLock
    exit 0
}
if ($open.Count -lt $appIds.Count) {
    Write-Log "Left out, handled by the job waited for: $(@($appIds | Where-Object { $open -notcontains $_ }) -join ', ')."
    $appIds = $open
}

$exitCode = 1
$script:jobError = ''
try {
    # A lock file left behind: the last job was interrupted, or its end state could not be sent
    if (Test-Path -LiteralPath $lockFile) {
        Write-Log 'The last job did not end cleanly (install.lock left behind).'
        Send-PendingEnd
    }
    # The job ID too: report.ps1 names the job in 'aborted' when this job is interrupted
    Write-SteamInstallMarker $lockFile $appIds $JobId
    Write-Log "Accepted: $($appIds.Count) game(s) ($(@($appIds | ForEach-Object { if ($script:targets.ContainsKey($_)) { "$_ to $($script:targets[$_])" } else { $_ } }) -join ', '))$(if ($JobId) { ", job $JobId" })."
    [void](Send-JobState 'started' ([ordered]@{ appids = $appIds; count = $appIds.Count }))
    Resolve-NewGameLibrary
    try {
        Clear-PendingManifests
    } catch {
        Write-Log "Warning: left-behind manifests not cleared: $($_.Exception.Message)"
    }
    for ($i = 0; $i -lt $appIds.Count; $i++) {
        $appId = $appIds[$i]
        $script:position = $i + 1
        try {
            Install-Game $appId
        } catch {
            Complete-Game $appId 'failed' "unexpected exception: $($_.Exception.Message)" 'unexpected_error'
        }
    }
    if ($resOk.Count -gt 0) { Invoke-LibraryReport }
    if ($resFailed.Count -gt 0 -or $resNoSpace.Count -gt 0 -or $resGameRunning.Count -gt 0) {
        Send-JobEnd 'done_with_errors' (Get-JobResult)
    } else {
        Send-JobEnd 'done' (Get-JobResult)
        $exitCode = 0
    }
} catch {
    $script:jobError = $_.Exception.Message
    Write-Log "ERROR: the job stopped: $($script:jobError)"
} finally {
    if (-not $script:jobEnded) {
        # The job ends for HA in any case; games not handled yet count as failed
        foreach ($id in $appIds) {
            if (-not $script:handled.ContainsKey($id)) { [void]$resFailed.Add($id) }
        }
        $result = Get-JobResult
        $result['reason'] = if ($script:jobError) { "the job stopped: $($script:jobError)" } else { 'the job stopped' }
        $result['reason_code'] = 'job_failed'
        Send-JobEnd 'done_with_errors' $result
    }
    # A client this job shut down is started again, also when it exits only now
    if ($script:steamStoppedByJob) {
        try {
            if (Start-SteamClient -WaitExitSec 60) {
                Write-Log 'Steam client started again (it was shut down by this job).'
            } else {
                Write-Log 'Warning: the Steam client this job shut down did not exit, it keeps running.'
            }
        } catch {
            Write-Log "Warning: the Steam client could not be started again: $($_.Exception.Message)"
        }
    }
    # Before the job lock is free: a copy of this script that waited for it leaves these games
    Save-LastJob $appIds
    # With an unsent end state the lock file stays: report.ps1 sends the state later
    if (-not $script:endPending) { Remove-Item -LiteralPath $lockFile -Force -ErrorAction SilentlyContinue }
    Exit-SteamEngineJobLock $jobLock
}
exit $exitCode
