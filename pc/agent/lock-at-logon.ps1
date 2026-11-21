# Locks the screen right after logon at the PC itself, so a PC that logs on by itself (autologon after
# a wake-up) does not stand open; Steam and the agent keep running in the locked session. Only while
# Windows autologon is set up (then a sign-in by hand is locked too, which is rare: autologon keeps
# the user signed in) and not in a remote desktop session. One line per logon in lock-at-logon.log.
# Exception: a game start from Home Assistant woke the PC and left a note on steam/launch_pending
# (integration option "Start games without the lock", see PROTOCOL.md). The script looks for it before
# it locks, with a hard limit of about 3 s; only a valid note leaves the lock out (once: the note is
# cleared and remembered in lock-at-logon.used). No note, an expired, used or malformed one, one valid
# for more than 15 minutes, a timeout or any error: it locks.
# Started by the scheduled task "Steam Engine Lock" (Install-SteamEngine.ps1 -Update -LockAtLogon on).
# ASCII only in this file (Windows PowerShell 5.1 reads .ps1 without BOM as ANSI).
$logFile = Join-Path $PSScriptRoot 'lock-at-logon.log'

# Never throws. The shared library is loaded only here, after the decision: the lock must not depend
# on it.
function Write-Log([string]$message) {
    try {
        . (Join-Path (Split-Path $PSScriptRoot -Parent) 'mqtt_publish.ps1')
        Write-SteamEngineLog $logFile $message
    } catch {
        try { [System.IO.File]::AppendAllText($logFile, ([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture) + '  ' + $message + "`r`n")) } catch { }
    }
}

$w = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' -ErrorAction SilentlyContinue
if ("$($w.AutoAdminLogon)" -ne '1') { Write-Log 'Not locked: autologon is off.'; exit 0 }
Add-Type -AssemblyName System.Windows.Forms
if ([System.Windows.Forms.SystemInformation]::TerminalServerSession) { Write-Log 'Not locked: remote desktop session.'; exit 0 }

# The note, read in a runspace of its own so the limit holds whatever hangs (name lookup, broker).
# Returns '<appid>|<until>' for a valid note, else ''. Throws on errors.
$readNote = {
    param([string]$LibPath, [string]$CredFile)
    $ErrorActionPreference = 'Stop'
    . $LibPath
    if ($SteamEngineNamesProblem) { throw $SteamEngineNamesProblem }
    $cfg = Get-SteamEngineConfig
    $cred = Get-MqttCredentials $CredFile
    if ($cred.Problem) { throw "MQTT login: $($cred.Problem)" }
    # Right after the sign-in the network can still be coming up: try again until the time is over
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        try {
            $bytes = Get-MqttRetainedMessage -BrokerHost $cfg.MqttHost -Port $cfg.MqttPort `
                -ClientId ($env:COMPUTERNAME.ToLowerInvariant() + '-steam-lock') -Username $cred.Username `
                -Password $cred.Password -Topic $SteamEngineTopic.LaunchPending -TimeoutMs ([int][math]::Max(500, 2800 - $clock.ElapsedMilliseconds))
            break
        } catch {
            if ($clock.ElapsedMilliseconds -ge 2300) { throw }
            Start-Sleep -Milliseconds 300
        }
    }
    if ($null -eq $bytes -or $bytes.Length -eq 0) { return '' }
    $note = [System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
    $appId = "$($note.appid)"
    if ($appId -notmatch '^[0-9]+$') { return '' }
    $until = [DateTime]::MinValue
    $styles = [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal
    # Exactly the form the integration writes; nothing lenient (no time-only or culture-shaped values)
    if (-not [DateTime]::TryParseExact("$($note.until)", "yyyy-MM-dd'T'HH:mm:ss'Z'", [System.Globalization.CultureInfo]::InvariantCulture, $styles, [ref]$until)) { return '' }
    # Valid now and not further ahead than the 10 minutes HA gives a note (plus some clock skew), so
    # a wrong timestamp or a PC clock far behind cannot leave the lock out
    $now = [DateTime]::UtcNow
    if ($until -le $now -or $until -gt $now.AddMinutes(15)) { return '' }
    return $appId + '|' + $until.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", [System.Globalization.CultureInfo]::InvariantCulture)
}
$noteKey = ''
$noteProblem = ''
try {
    $seRoot = Split-Path $PSScriptRoot -Parent
    $ps = [PowerShell]::Create()
    [void]$ps.AddScript($readNote).AddArgument((Join-Path $seRoot 'mqtt_publish.ps1')).AddArgument((Join-Path $seRoot 'mqtt_credentials.txt'))
    $run = $ps.BeginInvoke()
    if ($run.AsyncWaitHandle.WaitOne(3000)) {
        $noteKey = "$(@($ps.EndInvoke($run)) | Select-Object -Last 1)"
    } else {
        # Left running: the process ends after the lock
        $noteProblem = 'timeout'
    }
} catch {
    $noteProblem = $_.Exception.GetBaseException().Message
}
# A note is used once: remembered here before the lock is left out, so the same note (its clear did
# not reach the broker) cannot leave it out at a second logon. Not readable or writable: it locks.
$noteAppId = ''
if ($noteKey -match '^([0-9]+)\|\S+$') {
    $keyAppId = $Matches[1]
    $usedFile = Join-Path $PSScriptRoot 'lock-at-logon.used'
    try {
        $used = ''
        if (Test-Path -LiteralPath $usedFile) { $used = [System.IO.File]::ReadAllText($usedFile).Trim() }
        if ($used -eq $noteKey) {
            $noteProblem = 'note already used'
        } else {
            [System.IO.File]::WriteAllText($usedFile, $noteKey)
            $noteAppId = $keyAppId
        }
    } catch {
        $noteProblem = "used note not checked: $($_.Exception.Message)"
    }
}
if ($noteAppId -match '^[0-9]+$') {
    Write-Log "Not locked: game start $noteAppId requested from Home Assistant"
    try {
        . (Join-Path (Split-Path $PSScriptRoot -Parent) 'mqtt_publish.ps1')
        $cfg = Get-SteamEngineConfig
        $cred = Get-MqttCredentials (Join-Path (Split-Path $PSScriptRoot -Parent) 'mqtt_credentials.txt')
        Publish-MqttRetained -BrokerHost $cfg.MqttHost -Port $cfg.MqttPort `
            -ClientId ($env:COMPUTERNAME.ToLowerInvariant() + '-steam-lock') -Username $cred.Username `
            -Password $cred.Password -Topic $SteamEngineTopic.LaunchPending -PayloadBytes ([byte[]]@()) -TimeoutMs 5000
    } catch {
        Write-Log "Game start note not cleared: $($_.Exception.Message)"
    }
    exit 0
}

Start-Process rundll32.exe -ArgumentList 'user32.dll,LockWorkStation'
if ($noteProblem) { Write-Log "Screen locked (game start note not read: $noteProblem)." } else { Write-Log 'Screen locked.' }
