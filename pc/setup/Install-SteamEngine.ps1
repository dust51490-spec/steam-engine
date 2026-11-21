<#
Sets up the PC side of Steam Engine or updates it.

  Set up (new PC):       powershell -ExecutionPolicy Bypass -File Install-SteamEngine.ps1
  Update scripts only:   powershell -ExecutionPolicy Bypass -File Install-SteamEngine.ps1 -Update
  New MQTT login:        powershell -ExecutionPolicy Bypass -File Install-SteamEngine.ps1 -Update -MqttLogin
  Other MQTT broker:     powershell -ExecutionPolicy Bypass -File Install-SteamEngine.ps1 -Update -MqttHost <address>
  Autologon:             powershell -ExecutionPolicy Bypass -File Install-SteamEngine.ps1 -Update -Autologon on
                         (-Autologon off turns it off; see Set-Autologon.ps1)
  Lock after autologon:  powershell -ExecutionPolicy Bypass -File Install-SteamEngine.ps1 -Update -LockAtLogon on
                         (-LockAtLogon off removes it again)

Layout on the PC (%LOCALAPPDATA%\SteamEngine):
  settings.psd1, mqtt_credentials.txt, mqtt_publish.ps1   shared (the MQTT password encrypted)
  agent\    steam-agent.ps1, plugins.psd1                  scheduled task "Steam Engine Agent"
            lock-at-logon.ps1                              scheduled task "Steam Engine Lock" (optional)
  jobs\     install.ps1, uninstall.ps1, move.ps1
  library\  report.ps1, update history                     started by the agent every 30 minutes

What it does:
  1. Checks for Windows PowerShell 5.1 and the Steam client.
  2. Copies the scripts from this repository. Of every script only the newest old version stays as
     .bak_<time>.
  3. Creates settings.psd1 (MQTT address, the PC's name; all keys in settings.example.psd1) and
     mqtt_credentials.txt, only if they are missing or -New is given (the MQTT login also with
     -MqttLogin). -MqttHost also changes an existing settings.psd1, with -Update too. New games go to
     Steam's default library (the star in Steam > Settings > Storage). Passwords are asked for hidden and
     never printed. The MQTT password is stored encrypted with DPAPI, readable only for this Windows
     user on this PC. A new login is tried with the broker before it is saved (three
     tries; an unreachable broker asks for its address again if that was asked in this run); a kept
     login is tried too, a failure then only warns. The name is only for a second PC on the same
     Home Assistant (the first one needs none); a new setup checks with the broker that no other
     computer runs under that name (steam/.../agent/info) and stops unless you confirm.
  4. Creates the scheduled task "Steam Engine Agent" (at logon, without a window) if it is missing or
     on a new setup. Of the task definitions saved in agent\task_backup the newest three per task
     stay. With -Autologon on|off it turns Windows autologon on or off with Set-Autologon.ps1
     (password checked first and stored encrypted, asked for in a window with administrator rights);
     a setup without -Update asks for it while autologon does not sign in this account (off, or set
     up for another account). With -LockAtLogon on the task "Steam Engine Lock": it locks the screen
     right after an autologon at the PC itself (Steam and the agent keep running), so a woken PC does
     not stand open; -LockAtLogon off removes it. A setup without -Update, and -Autologon on, ask for
     it if the task is missing and -LockAtLogon is not given (without autologon it does nothing), not
     while autologon stays with another account (it would lock every sign-in by hand). The lock is
     set up before autologon comes on. The end says when autologon is off, signs in another account
     or comes without the lock.
  5. Restarts the agent, checks that it runs and says what is left to do in Home Assistant.
Plugins (e.g. a backup script) bring their own parts; their entries in agent\plugins.psd1 stay.
For the PC to start without anyone logging in after a wake-up, it needs autologon (step 4).
ASCII only in this file (Windows PowerShell 5.1 reads .ps1 without BOM as ANSI).
#>
param(
    [switch]$Update,
    [switch]$New,
    [switch]$MqttLogin,
    [ValidateSet('on', 'off')][string]$Autologon,
    [ValidateSet('on', 'off')][string]$LockAtLogon,
    [string]$MqttHost,
    [string]$PcName
)
$ErrorActionPreference = 'Stop'

$repoPc     = Split-Path $PSScriptRoot -Parent
$root       = Join-Path $env:LOCALAPPDATA 'SteamEngine'
$dirAgent   = Join-Path $root 'agent'
$dirJobs    = Join-Path $root 'jobs'
$dirLibrary = Join-Path $root 'library'
$cfgFile    = Join-Path $root 'settings.psd1'
$credFile   = Join-Path $root 'mqtt_credentials.txt'
$taskAgent   = 'Steam Engine Agent'
$taskLock    = 'Steam Engine Lock'
# Written into a new settings.psd1; -New keeps the port of the old one
$mqttPortSetting = 1883

# The same header twice in a row shows once (step 4: a question, then what is done)
function Step($text) {
    if ($text -eq $script:lastStep) { return }
    $script:lastStep = $text
    Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan
}
function Info($text) { Write-Host "   $text" }
function Warn($text) { Write-Host "   $text" -ForegroundColor Yellow }

function Read-Value($question, $default) {
    $hint = if ($default) { " [$default]" } else { '' }
    $a = Read-Host ("   $question$hint")
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return $a.Trim()
}

function Read-Secret($question) {
    $s = Read-Host ("   $question") -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

function Read-YesNo($question, [bool]$default) {
    $hint = if ($default) { 'Y/n' } else { 'y/N' }
    $a = Read-Host ("   $question [$hint]")
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return ($a.Trim() -match '^(y|yes|j|ja)$')
}

# Tries the MQTT login with an empty retained message to a topic nothing uses (it keeps nothing on
# the broker). Needs mqtt_publish.ps1. Returns $null if it works, else @{ Kind; Message } with Kind
# 'login' (user or password refused), 'network' (broker not reachable) or 'other'.
function Test-MqttLogin($hostName, [int]$port, $user, $password) {
    try {
        Publish-MqttRetained -BrokerHost $hostName -Port $port -ClientId ('steam-engine-setup-' + $env:COMPUTERNAME.ToLowerInvariant()) `
            -Username $user -Password $password -Topic 'steam/setup/test' -PayloadBytes ([byte[]]@()) -Qos 1 -Retain $true -TimeoutMs 5000
        return $null
    } catch {
        # The messages of Publish-MqttRetained never contain the login
        $m = $_.Exception.GetBaseException().Message.TrimEnd('.')
        if ($m -match 'return code (\d+)') {
            if ([int]$matches[1] -in 4, 5) { return @{ Kind = 'login'; Message = 'The MQTT broker refused the login: user or password wrong, or the user may not use MQTT.' } }
            return @{ Kind = 'other'; Message = "The MQTT broker refused the connection ($m)." }
        }
        return @{ Kind = 'network'; Message = "The MQTT broker ${hostName}:$port is not reachable ($m)." }
    }
}

# A value for a single-quoted string in settings.psd1: quotes doubled (a path like D:\Bob's Games),
# also the typographic ones PowerShell takes as quotes (U+2018..U+201B)
function ConvertTo-PsdString([string]$value) {
    $quoteChars = "['" + [char]0x2018 + [char]0x2019 + [char]0x201A + [char]0x201B + "]"
    return ($value -replace $quoteChars, '$0$0')
}

# Writes settings.psd1 from $MqttHost, $mqttPortSetting and $PcName
function Save-Settings {
    # UTF-8 with BOM, so a name with non-ASCII characters stays intact
    @(
        '# Local settings of Steam Engine, created by Install-SteamEngine.ps1. Not for the repository.',
        '# All keys: settings.example.psd1 in the setup folder of Steam Engine.',
        '@{',
        "    MqttHost = '$(ConvertTo-PsdString $MqttHost)'",
        "    MqttPort = $mqttPortSetting",
        '    # Name of this PC: only for a second PC on the same Home Assistant, empty for the first one',
        "    PcName   = '$(ConvertTo-PsdString $PcName)'",
        '}'
    ) | Set-Content -LiteralPath $cfgFile -Encoding UTF8
    # Read it back once: a file the scripts cannot read would make them run on defaults
    $check = $null
    try { $check = Import-PowerShellDataFile -LiteralPath $cfgFile } catch { }
    if ($null -eq $check -or $check.MqttHost -ne $MqttHost -or "$($check.PcName)" -ne $PcName) {
        throw "settings.psd1 could not be written readably ($cfgFile). Please check the values and run the setup again."
    }
    $named = if ($PcName) { ", PC name $PcName" } else { '' }
    Info "settings.psd1 saved (MQTT $MqttHost$named)."
}

# Changes one value of the existing settings.psd1 and keeps all else, comments too. The old file stays
# as .bak_<time>; if the result is not right, it is put back. $true when the value is set.
function Set-SettingValue([string]$key, [string]$value) {
    $before = $null
    try { $before = Import-PowerShellDataFile -LiteralPath $cfgFile } catch { }
    if ($null -eq $before) {
        Warn "settings.psd1 cannot be read, $key not changed. Please check the file or use -New."
        return $false
    }
    if ("$($before[$key])" -ceq $value) { Info "$key is $value already."; return $true }
    # Write back in the encoding the file came in (UTF-8 with BOM or ANSI), so other values stay
    $raw = [System.IO.File]::ReadAllBytes($cfgFile)
    $hasBom = $raw.Length -ge 3 -and $raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF
    $enc = if ($hasBom) { New-Object System.Text.UTF8Encoding $true } else { [System.Text.Encoding]::Default }
    $txt = Get-Content -LiteralPath $cfgFile -Raw
    # '$' would be read as a group reference in the replacement
    $quoted = ("'" + (ConvertTo-PsdString $value) + "'").Replace('$', '$$')
    if ($txt -match "(?m)^\s*$key\s*=") {
        $txt = $txt -replace "(?m)^(\s*$key\s*=\s*)'(?:[^']|'')*'", ('${1}' + $quoted)
    } elseif ($txt -match '(?m)^\s*MqttPort\s*=') {
        $txt = $txt -replace '(?m)^(\s*MqttPort\s*=[^\r\n]*)', ("`${1}`r`n    $key = " + $quoted)
    } else {
        $txt = $txt -replace '(?m)^@\{', ("@{`r`n    $key = " + $quoted)
    }
    $bak = $cfgFile + '.bak_' + (Get-Date -Format 'yyyyMMdd_HHmmss')
    Copy-Item -LiteralPath $cfgFile -Destination $bak
    [System.IO.File]::WriteAllText($cfgFile, $txt, $enc)
    # Check: the new value is there and nothing else changed, otherwise back to the old file
    $after = $null
    try { $after = Import-PowerShellDataFile -LiteralPath $cfgFile } catch { }
    $same = $null -ne $after -and "$($after[$key])" -ceq $value
    if ($same) {
        foreach ($k in @($before.Keys) + @($after.Keys)) {
            if ($k -ne $key -and "$($before[$k])" -ne "$($after[$k])") { $same = $false }
        }
    }
    if (-not $same) {
        Copy-Item -LiteralPath $bak -Destination $cfgFile -Force
        Warn "Could not set $key in settings.psd1 (old file restored), please edit it by hand."
        return $false
    }
    Info "$key set to '$value' in settings.psd1 (old file kept as .bak)."
    Remove-OldBackups $cfgFile
    return $true
}

# Keeps only the newest copy <file>.bak_<yyyyMMdd_HHmmss> this setup made of a file (copies named
# otherwise, e.g. made by hand, stay)
function Remove-OldBackups([string]$file) {
    try {
        $leaf = [System.IO.Path]::GetFileName($file)
        $pattern = '^' + [regex]::Escape($leaf) + '\.bak_\d{8}_\d{6}$'
        $baks = @(Get-ChildItem -LiteralPath ([System.IO.Path]::GetDirectoryName($file)) -File |
            Where-Object { $_.Name -match $pattern } | Sort-Object Name -Descending)
        foreach ($b in @($baks | Select-Object -Skip 1)) { Remove-Item -LiteralPath $b.FullName -Force }
    } catch {
        Warn "Older backups of $file not removed: $($_.Exception.Message)"
    }
}

# Saves the definition of a task before it is replaced or removed; the newest three per task stay
function Save-TaskDefinition($name) {
    if (-not (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) { return $false }
    $backup = Join-Path $dirAgent 'task_backup'
    New-Item -ItemType Directory -Force $backup | Out-Null
    $prefix = ($name -replace ' ', '_') + '_'
    Export-ScheduledTask -TaskName $name | Set-Content -LiteralPath (Join-Path $backup ("{0}{1}.xml" -f $prefix, (Get-Date -Format 'yyyyMMdd_HHmmss'))) -Encoding Unicode
    try {
        $pattern = '^' + [regex]::Escape($prefix) + '\d{8}_\d{6}\.xml$'
        @(Get-ChildItem -LiteralPath $backup -File | Where-Object { $_.Name -match $pattern } | Sort-Object Name -Descending) |
            Select-Object -Skip 3 | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
    } catch {
        Warn "Older task definitions of $name not removed: $($_.Exception.Message)"
    }
    return $true
}

function Register-Task($name, $script, $delay, $timeLimit) {
    $arg = '--headless powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $script + '"'
    $action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument $arg
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $trigger.Delay = $delay
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([System.Xml.XmlConvert]::ToTimeSpan($timeLimit))
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $existing = Save-TaskDefinition $name
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Info "Task $name created$(if ($existing) { ' (old definition saved in agent\task_backup)' })."
}

# Only the agent of this Windows user (started from its own folder): an agent of another user on the
# same PC stays, and stopping it would need administrator rights
function Get-AgentProcess {
    $script = Join-Path $dirAgent 'steam-agent.ps1'
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($script, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
}

function Stop-Agent {
    Get-AgentProcess | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

# A first setup on this PC (for the hints at the end)
$firstSetup = -not (Test-Path -LiteralPath $cfgFile)
$settingsSaved = $false

# --- 1. Requirements ------------------------------------------------------------------
Step 'Requirements'
if ($PSVersionTable.PSEdition -ne 'Desktop') { throw 'Please run with Windows PowerShell 5.1 (powershell.exe, not pwsh).' }
$reg = Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue
if (-not ($reg -and $reg.SteamPath)) { throw 'Steam client not found. Please install Steam and log in once.' }
$steamRoot = $reg.SteamPath -replace '/', '\'
Info "Steam: $steamRoot"
# The shared file first: the other scripts use its functions (a script started in between must find them)
$copies = @(
    @('common\mqtt_publish.ps1', $root),
    @('agent\steam-agent.ps1', $dirAgent),
    @('agent\lock-at-logon.ps1', $dirAgent),
    @('jobs\install.ps1', $dirJobs),
    @('jobs\uninstall.ps1', $dirJobs),
    @('jobs\move.ps1', $dirJobs),
    @('library\report.ps1', $dirLibrary))
foreach ($c in $copies) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoPc $c[0]))) { throw "Source missing: $(Join-Path $repoPc $c[0]). Start the script from the folder setup of the extracted package (pc\setup in the repository)." }
}

# --- 2. Copy scripts ------------------------------------------------------------------
Step 'Copy scripts'
foreach ($d in $root, $dirAgent, $dirJobs, $dirLibrary) { New-Item -ItemType Directory -Force $d | Out-Null }
foreach ($c in $copies) {
    $source = Join-Path $repoPc $c[0]
    $target = Join-Path $c[1] (Split-Path $source -Leaf)
    if ((Test-Path -LiteralPath $target) -and ((Get-FileHash -LiteralPath $source).Hash -eq (Get-FileHash -LiteralPath $target).Hash)) {
        Info "$(Split-Path $target -Leaf): unchanged"
    } else {
        if (Test-Path -LiteralPath $target) { Copy-Item -LiteralPath $target -Destination ($target + '.bak_' + (Get-Date -Format 'yyyyMMdd_HHmmss')) }
        Copy-Item -LiteralPath $source -Destination $target -Force
        Info "$(Split-Path $target -Leaf): updated"
    }
    Remove-OldBackups $target
}
# The shared functions (libraries, settings, MQTT login) from the file just copied
. (Join-Path $root 'mqtt_publish.ps1')

# --- 3. Settings and credentials --------------------------------------------------------
if ((Test-Path -LiteralPath $cfgFile) -and -not $New) {
    $change = $PSBoundParameters.ContainsKey('MqttHost')
    if ($change -or -not $Update) { Step 'Settings' }
    if ($PSBoundParameters.ContainsKey('MqttHost')) {
        if ($MqttHost -match '^\S+$') { [void](Set-SettingValue 'MqttHost' $MqttHost) } else { Warn "'$MqttHost' is no MQTT address, MqttHost not changed." }
    }
    if ($PSBoundParameters.ContainsKey('PcName')) {
        Warn 'The name of a PC that is set up cannot be changed with -PcName (its entities in Home Assistant would stay behind). Remove it with Uninstall.cmd and set it up again, or use -New.'
    }
    if (-not $change -and -not $Update) { Info 'settings.psd1 exists, kept (use -New to create it again).' }
} elseif (-not $Update) {
    Step 'Settings'
    # -New: the values of the old file are offered
    $old = $null
    if (Test-Path -LiteralPath $cfgFile) { try { $old = Import-PowerShellDataFile -LiteralPath $cfgFile } catch { } }
    $port = 0
    if ($old -and [int]::TryParse("$($old.MqttPort)", [ref]$port) -and $port -ge 1 -and $port -le 65535) { $mqttPortSetting = $port }
    try {
        $default = Get-SteamDefaultLibrary $steamRoot @(Get-SteamLibraryInfo $steamRoot)
        if ($default.Library) { Info "New games go to Steam's default library (the star in Steam > Settings > Storage): $($default.Library.Path)" }
    } catch { }
    if ($MqttHost -and $MqttHost -notmatch '^\S+$') { throw "'$MqttHost' is no MQTT address." }
    if (-not $MqttHost) {
        $suggestHost = if ($old -and "$($old.MqttHost)" -match '^\S+$') { "$($old.MqttHost)" } else { 'homeassistant.local' }
        $MqttHost = Read-Value 'Address of the MQTT broker (Home Assistant)' $suggestHost
    }
    # The name: none for the first PC (the usual case), one of its own for a second PC on the same
    # Home Assistant. With -New the old name is offered ('-' for none).
    $oldName = if ($old) { "$($old.PcName)".Trim() } else { '' }
    if ($PSBoundParameters.ContainsKey('PcName')) {
        $PcName = "$PcName".Trim()
        $null = ConvertTo-SteamEnginePcId $PcName   # throws for a name without letters or digits
    } else {
        Info 'A name is only needed for a second PC on the same Home Assistant (e.g. Kids PC); the first PC needs none.'
        $question = if ($oldName) { "Name of this PC ('-' for none)" } else { 'Name of this PC, or just Enter for none' }
        for ($try = 1; ; $try++) {
            $PcName = "$(Read-Value $question $oldName)".Trim()
            if ($PcName -eq '-') { $PcName = '' }
            try {
                $null = ConvertTo-SteamEnginePcId $PcName
                break
            } catch {
                if ($try -ge 3) { throw }
                Warn $_.Exception.Message
            }
        }
    }
    if ($old -and $PcName -ne $oldName) {
        $was = if ($oldName) { $oldName } else { 'Steam PC' }
        Warn "This PC was '$was' before: its device and entities stay in Home Assistant, remove them there by hand."
    }
    if (Test-Path -LiteralPath $cfgFile) {
        Copy-Item -LiteralPath $cfgFile -Destination ($cfgFile + '.bak_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
        Remove-OldBackups $cfgFile
    }
    Save-Settings
    $settingsSaved = $true
} elseif (-not (Test-Path -LiteralPath $cfgFile)) {
    Step 'Settings'
    Warn 'settings.psd1 is missing, the scripts use the defaults. Run the setup without -Update to create it.'
}
$cfgNow = Get-SteamEngineConfig
if ($cfgNow.ConfigProblem) { Warn "$($cfgNow.ConfigProblem)." }

# --- 3b. MQTT login --------------------------------------------------------------------------
Step 'MQTT login'
$current = Get-MqttCredentials $credFile
$loginOk = $false
$loginTested = $false
if ($New -or $MqttLogin -or (-not $Update -and -not (Test-Path -LiteralPath $credFile))) {
    Info 'A user of the Mosquitto broker in Home Assistant (Settings > People > Users, or the Mosquitto login).'
    $cfg = Get-SteamEngineConfig
    $u = $current.Username
    $askLogin = $true
    $saveUnchecked = $false
    # Three tries; a broker that is not reachable asks for its address again (only if it was asked
    # in this run), a refused login for user and password
    for ($try = 1; $try -le 3 -and -not $loginOk -and -not $saveUnchecked; $try++) {
        if ($askLogin) {
            $u = Read-Value 'MQTT user' $u
            $pw = Read-Secret 'MQTT password (not shown)'
            if ([string]::IsNullOrWhiteSpace($u) -or [string]::IsNullOrWhiteSpace($pw)) { throw 'MQTT user and password are needed, nothing was changed.' }
        }
        $problem = Test-MqttLogin $cfg.MqttHost $cfg.MqttPort $u $pw
        if (-not $problem) { $loginOk = $true; break }
        Warn $problem.Message
        $askLogin = ($problem.Kind -ne 'network')
        if ($problem.Kind -eq 'network' -and $try -lt 3) {
            if ($settingsSaved) {
                $MqttHost = Read-Value 'Address of the MQTT broker (Home Assistant)' $cfg.MqttHost
                Save-Settings
                $cfg = Get-SteamEngineConfig
            } elseif (Read-YesNo "Save the login anyway (the address $($cfg.MqttHost) is in settings.psd1)?" $false) {
                $saveUnchecked = $true
            } else {
                break
            }
        }
    }
    if (-not $loginOk -and -not $saveUnchecked) {
        $pw = $null
        throw 'The MQTT login did not work, it was not saved. Check that Home Assistant runs, its MQTT broker and the MQTT user, then run the setup again.'
    }
    Save-MqttCredentials $credFile $u $pw
    $pw = $null
    $loginTested = $true
    if ($loginOk) { Info "The login works (broker $($cfg.MqttHost))." }
    Info 'mqtt_credentials.txt saved, the password encrypted for this Windows user.'
} elseif ($current.Problem) {
    Warn "mqtt_credentials.txt: $($current.Problem)."
} else {
    Info 'mqtt_credentials.txt exists, kept (password encrypted).'
}
$current = $null
# A login that was kept: check it, but do not stop (Home Assistant may just be restarting)
if (-not $loginTested) {
    $kept = Get-MqttCredentials $credFile
    if (-not $kept.Problem) {
        $cfg = Get-SteamEngineConfig
        $problem = Test-MqttLogin $cfg.MqttHost $cfg.MqttPort $kept.Username $kept.Password
        if ($problem) {
            Warn $problem.Message
            if ($problem.Kind -eq 'login') { Warn 'Enter it again with: Install.cmd -Update -MqttLogin' }
            Warn 'Until this works the agent cannot reach Home Assistant.'
        } else {
            $loginOk = $true
            Info "The login works (broker $($cfg.MqttHost))."
        }
    }
    $kept = $null
}

# --- 3c. Another computer under this PC's name? -------------------------------------------------
# Only on a setup (not -Update) with a working login. The agent of every PC tells on its info topic
# which computer it runs on.
if (-not $Update -and $loginOk) {
    $names = Get-SteamEngineNames (Get-SteamEngineConfig).PcName
    $cfg = Get-SteamEngineConfig
    $kept = Get-MqttCredentials $credFile
    $read = {
        param($topic)
        $bytes = Get-MqttRetainedMessage -BrokerHost $cfg.MqttHost -Port $cfg.MqttPort `
            -ClientId ('steam-engine-setup-' + $env:COMPUTERNAME.ToLowerInvariant()) -Username $kept.Username `
            -Password $kept.Password -Topic $topic -TimeoutMs 3000
        if ($null -eq $bytes) { return $null }
        return [System.Text.Encoding]::UTF8.GetString($bytes)
    }
    $other = $null
    try {
        $infoText = & $read $names.Topic.AgentInfo
        if ($infoText) {
            $info = $infoText | ConvertFrom-Json
            if ("$($info.computer)" -and "$($info.computer)" -ne $env:COMPUTERNAME) { $other = "the computer $($info.computer)" }
        }
    } catch {
        Warn "Could not check whether another computer uses this PC name ($($_.Exception.Message))."
    }
    $kept = $null
    if ($other) {
        Warn "'$($names.DeviceName)' is already used by $other."
        Warn 'Two computers under one name would both take its jobs. Give this PC a name of its own (run the setup with -New),'
        Warn 'unless the other one no longer runs Steam Engine.'
        if (-not (Read-YesNo "Use '$($names.DeviceName)' for this PC anyway?" $false)) {
            throw "Setup stopped: '$($names.DeviceName)' is in use. Run Install.cmd -New and give this PC another name."
        }
    }
}

# --- 4. Scheduled tasks and autologon -------------------------------------------------------
if (-not $Update -or -not (Get-ScheduledTask -TaskName $taskAgent -ErrorAction SilentlyContinue)) {
    Step 'Scheduled task'
    Register-Task $taskAgent (Join-Path $dirAgent 'steam-agent.ps1') 'PT1M' 'PT0S'
}
# Autologon: a setup without -Update asks while it does not sign in this account (off, or set up for
# another one); it is set up after the lock below. Set-Autologon.ps1 asks for the password in a window
# with administrator rights; this script keeps running without them.
$autologonScript = Join-Path $PSScriptRoot 'Set-Autologon.ps1'
$autologonState = $null
if (-not $Update -and -not $Autologon) {
    $autologonState = & $autologonScript -Query
    if (-not $autologonState.ThisAccount) {
        Step 'Autologon'
        Info 'When Home Assistant wakes this PC, Windows has to sign in by itself (autologon), or Steam cannot work.'
        Info 'The password is checked first and stored encrypted by Windows, as Sysinternals Autologon does.'
        if ($autologonState.ForeignWriter) {
            # Set-Autologon.ps1 would refuse to run as administrator from here: no question, the way
            Warn "It cannot be set up from this folder: other Windows accounts can change files here ($($autologonState.ForeignWriter))."
            Info "Extract the zip again into a folder under $env:USERPROFILE and run there: Install.cmd -Update -Autologon on"
        } elseif ($autologonState.On) {
            Warn "Autologon signs in $($autologonState.User), not this account: after a wake-up Steam Engine does not start."
            $answer = Read-YesNo "Set it up for this account instead (no longer for $($autologonState.User))?" $false
        } else {
            $answer = Read-YesNo 'Set up autologon now?' $true
        }
        if ($answer) { $Autologon = 'on' } elseif (-not $autologonState.ForeignWriter) { Info 'Not set up (later: Install.cmd -Update -Autologon on).' }
    }
}
# The lock before autologon comes on, so that never happens without it being offered (it does nothing
# while autologon is off): a setup without -Update asks once while the task is missing, and so does
# -Autologon on. Not while autologon stays with another account: lock-at-logon.ps1 would then lock
# every sign-in of this account by hand (-LockAtLogon on still sets it up).
if (-not $LockAtLogon -and (-not $Update -or $Autologon -eq 'on') -and -not (Get-ScheduledTask -TaskName $taskLock -ErrorAction SilentlyContinue)) {
    Step 'Lock at logon'
    if ($autologonState.On -and -not $autologonState.ThisAccount -and -not $Autologon) {
        Info "Not offered while autologon signs in $($autologonState.User), not this account: the lock would lock every sign-in"
        Info 'of this account by hand. To set it up anyway: Install.cmd -Update -LockAtLogon on'
    } else {
        Info 'When Home Assistant wakes this PC, Windows signs in by itself (autologon) and the PC stands open.'
        Info 'Steam Engine can lock the screen right after such a logon (Steam and the agent keep running); without autologon it does nothing.'
        if (Read-YesNo 'Lock the screen after an automatic logon?' $true) { $LockAtLogon = 'on' } else { Info 'Not set up (later: Install.cmd -Update -LockAtLogon on).' }
    }
}
if ($LockAtLogon -eq 'on') {
    Step 'Lock at logon'
    Register-Task $taskLock (Join-Path $dirAgent 'lock-at-logon.ps1') 'PT0S' 'PT1M'
    Info 'After an autologon at the PC the screen is locked at once; Steam and the agent keep running.'
} elseif ($LockAtLogon -eq 'off' -and (Get-ScheduledTask -TaskName $taskLock -ErrorAction SilentlyContinue)) {
    Step 'Lock at logon'
    [void](Save-TaskDefinition $taskLock)
    Unregister-ScheduledTask -TaskName $taskLock -Confirm:$false
    Info "Task $taskLock removed."
}
if ($Autologon) {
    Step 'Autologon'
    & $autologonScript -State $Autologon
}

# --- 5. Restart the agent ------------------------------------------------------------------
Step 'Restart the Steam agent'
Stop-Agent
Start-Sleep 2
Start-ScheduledTask -TaskName $taskAgent
Start-Sleep 10
$log = Join-Path $dirAgent 'steam-agent.log'
# The agent's lines since its last start (UTF-8, read shared); it stops at once when the MQTT login is
# not usable
$sinceStart = @()
$tail = Read-FileTail $log 16384
if ($tail) {
    $lines = @($tail -split "`r?`n" | Where-Object { $_ })
    $i = -1
    for ($n = 0; $n -lt $lines.Count; $n++) { if ($lines[$n] -match '  Start \(PID ') { $i = $n } }
    if ($i -ge 0) { $sinceStart = @($lines[$i..($lines.Count - 1)]) }
}
$stopped = @($sinceStart | Where-Object { $_ -match '  Stopped: ' })
$agentRuns = [bool](Get-AgentProcess)
if ($agentRuns -and -not $stopped) {
    Info 'The agent runs; it sends the library in about a minute.'
    # E.g. an entry of plugins.psd1 it left out, or a problem with settings.psd1
    $warnings = @($sinceStart | Where-Object { $_ -match '  Warning: ' } | Select-Object -First 5)
    if ($warnings) {
        Warn 'The agent reports:'
        foreach ($l in $warnings) { Warn ($l.Substring(0, [Math]::Min(240, $l.Length))) }
    }
} else {
    Warn 'The agent does not run:'
    foreach ($l in @(@($stopped) + @($sinceStart | Select-Object -Last 2) | Select-Object -Unique)) { Warn ($l.Substring(0, [Math]::Min(240, $l.Length))) }
    Warn "Log: $log"
}

Step 'Done'
if ($agentRuns -and -not $stopped -and $loginOk) {
    Info 'Steam Engine runs on this PC and reaches Home Assistant.'
} elseif ($agentRuns -and -not $stopped) {
    Warn 'Steam Engine runs on this PC, but the MQTT login could not be checked (see above).'
} else {
    Warn 'Steam Engine does not run on this PC yet (see above). Fix the cause and run the setup again.'
}
if ($firstSetup) {
    Info 'Next, in Home Assistant: install Steam Engine with HACS and add it under Settings > Devices & services.'
    Info "The device ""$((Get-SteamEngineNames (Get-SteamEngineConfig).PcName).DeviceName)"" appears within a minute; the integration then picks it up by itself."
} else {
    Info 'Home Assistant needs the Steam Engine integration of the same version as these scripts.'
}
# Read again: the window of Set-Autologon.ps1 may have been closed early, or by its X after it was done
if (-not $Update -or $Autologon) {
    $autologonState = & $autologonScript -Query
    if (-not $autologonState.On) {
        Warn 'Autologon is off. For Home Assistant to wake the PC and let Steam work without anyone logging in,'
        Warn 'set it up with: Install.cmd -Update -Autologon on'
    } elseif (-not $autologonState.ThisAccount) {
        Warn "Autologon signs in $($autologonState.User), not this account, so Steam Engine does not start after a wake-up."
        Warn 'To set it up for this account: Install.cmd -Update -Autologon on'
        # E.g. the lock set up for an autologon that then did not come on
        if (Get-ScheduledTask -TaskName $taskLock -ErrorAction SilentlyContinue) {
            Warn 'Until then the lock locks every sign-in of this account by hand (Install.cmd -Update -LockAtLogon off removes it).'
        }
    } elseif (-not (Get-ScheduledTask -TaskName $taskLock -ErrorAction SilentlyContinue)) {
        Warn 'Autologon is on without the lock: a woken PC stands open. To lock the screen after it:'
        Warn 'Install.cmd -Update -LockAtLogon on'
    }
}
