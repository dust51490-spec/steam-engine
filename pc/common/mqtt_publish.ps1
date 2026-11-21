# Shared library of Steam Engine on the PC: settings, logging, Steam libraries and manifests, control
# of the Steam client, the job lock, and a minimal MQTT 3.1.1 publisher (CONNECT + PUBLISH[+PUBACK] +
# DISCONNECT over plain TCP, no external client needed) with a reader for one retained message.
# Dot-sourced by the agent, the jobs, the library report and the setup. The file keeps its old name
# because plugins dot-source it by this path (they use Get-SteamEngineConfig, Get-MqttCredentials and
# Publish-MqttRetained). It defines only functions with Steam or Mqtt in their name plus Get-AcfValue,
# Read-FileTail and Read-SharedTextFile, and two variables, $SteamAppState and $SteamEngineTopic, so it
# never replaces a caller's own Write-Log or similar. The process helpers add the type
# SteamEngineProcess on their first use (Initialize-SteamProcessType).
# ASCII only in this file (Windows PowerShell 5.1 reads .ps1 without BOM as ANSI).
#
# Security note: CONNECT carries user name and password in plain text (port 1883, unencrypted).
# This file must NEVER print or log credentials - neither by Write-Host/Write-Verbose nor in thrown
# error messages. On connection errors only throw return codes or generic messages, never the
# CONNECT payload or lines read from the credentials file.

# Bits of StateFlags in an appmanifest (EAppState of the Steam client)
$SteamAppState = @{
    UpdateRequired = 2
    FullyInstalled = 4
    UpdateRunning  = 256
    UpdatePaused   = 512
    UpdateStarted  = 1024
    Uninstalling   = 2048
    Reconfiguring  = 65536
    Validating     = 131072
    AddingFiles    = 262144
    Preallocating  = 524288
    Downloading    = 1048576
    Staging        = 2097152
    Committing     = 4194304
}
# Steam works on the game right now. Not FullyInstalled (set on every installed game), and not
# UpdatePaused or UpdateStarted alone: those only mean paused or queued, which can last for weeks.
$SteamAppState.Active = 256 -bor 65536 -bor 131072 -bor 262144 -bor 524288 -bor 1048576 -bor 2097152 -bor 4194304
# Paused or queued, nothing happening right now
$SteamAppState.Queued = 512 -bor 1024

# Shared local settings of Steam Engine. Install-SteamEngine.ps1 creates them:
# %LOCALAPPDATA%\SteamEngine\settings.psd1, e.g. (all keys: pc/setup/settings.example.psd1)
# @{ MqttHost = 'homeassistant.local'; MqttPort = 1883 }
# Without the file or a value the default applies. SteamRoot comes from the registry of the Steam client.
# ConfigProblem says why the file could not be used (fully or in part); empty when it is fine or
# missing. Keys no script knows (e.g. InstallLibrary of older versions) are ignored. First installs
# go to Steam's default library (Get-SteamDefaultLibrary), not to a setting.
function Get-SteamEngineConfig {
    $cfg = [ordered]@{
        MqttHost      = 'homeassistant.local'
        MqttPort      = 1883
        PcName        = ''
        SteamRoot     = 'C:\Program Files (x86)\Steam'
        ConfigProblem = ''
    }
    $problems = @()
    $reg = Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue
    if ($reg -and $reg.SteamPath) { $cfg.SteamRoot = ($reg.SteamPath -replace '/', '\') }
    $f = Join-Path $env:LOCALAPPDATA 'SteamEngine\settings.psd1'
    if (Test-Path -LiteralPath $f) {
        try {
            $d = Import-PowerShellDataFile -LiteralPath $f -ErrorAction Stop
            foreach ($k in @($d.Keys)) {
                if ($k -eq 'ConfigProblem') { continue }
                if ($null -ne $d[$k] -and "$($d[$k])" -ne '') { $cfg[$k] = $d[$k] }
            }
        } catch {
            $problems += "settings.psd1 not readable, defaults used ($($_.Exception.Message))"
        }
    }
    $port = 0
    if ([int]::TryParse(("$($cfg.MqttPort)").Trim(), [ref]$port) -and $port -ge 1 -and $port -le 65535) {
        $cfg.MqttPort = $port
    } else {
        $problems += "MqttPort '$($cfg.MqttPort)' in settings.psd1 is no port number, 1883 used"
        $cfg.MqttPort = 1883
    }
    $cfg.ConfigProblem = $problems -join '; '
    return $cfg
}

# Appends a line with the local time to a log file. Never throws: a log that cannot be written (locked
# by a reader, disk full) must not end a job or the agent. Tries a few times, then drops the line.
# Moves the file to <file>.old (replacing it) once it is larger than $MaxBytes. UTF-8 without BOM.
function Write-SteamEngineLog([string]$Path, [string]$Message, [int64]$MaxBytes = 1MB) {
    try {
        $stamp = [DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture)
        $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($stamp + '  ' + $Message + "`r`n")
        $length = [int64]0
        for ($try = 1; $try -le 3; $try++) {
            try {
                # FileShare ReadWrite, Delete: other writers, readers and the rotation are not blocked
                $fs = [System.IO.File]::Open($Path, 'Append', 'Write', 'ReadWrite,Delete')
                try { $fs.Write($bytes, 0, $bytes.Length); $length = $fs.Length } finally { $fs.Dispose() }
                break
            } catch {
                if ($try -lt 3) { Start-Sleep -Milliseconds 200 }
            }
        }
        if ($length -gt $MaxBytes) {
            try {
                $old = $Path + '.old'
                if ([System.IO.File]::Exists($old)) { [System.IO.File]::Delete($old) }
                [System.IO.File]::Move($Path, $old)
            } catch { }
        }
    } catch { }
}

# ISO 8601 time in UTC with the offset written out, e.g. 2026-10-31T12:34:56+00:00. Independent of the
# culture: in .NET format strings ':' is the culture's time separator ('.' under fi-FI).
function Format-SteamEngineUtc([datetime]$Time) {
    return $Time.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture) + '+00:00'
}

# Reads a whole text file (UTF-8, a BOM decides otherwise) while others may write, rename or delete it
# (FileShare ReadWrite, Delete): Steam's manifests and logs, and the scripts' own logs. A reader must
# never block a writer or the log rotation. $null when the file is missing or cannot be read.
function Read-SharedTextFile([string]$Path) {
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite,Delete')
        try {
            $reader = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8, $true)
            return $reader.ReadToEnd()
        } finally { $fs.Dispose() }
    } catch {
        return $null
    }
}

# The last $Bytes of a text file (UTF-8), read like Read-SharedTextFile. A line cut off at the start is
# left out. $null when the file is missing or cannot be read.
function Read-FileTail([string]$Path, [int]$Bytes = 65536) {
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite,Delete')
        try {
            # One byte more before the cut: a line break there means the first line is whole
            $n = [int][Math]::Min([int64]$Bytes + 1, $fs.Length)
            $start = $fs.Length - $n
            [void]$fs.Seek($start, 'Begin')
            $buf = New-Object byte[] $n
            $read = 0
            while ($read -lt $n) {
                $r = $fs.Read($buf, $read, $n - $read)
                if ($r -le 0) { break }
                $read += $r
            }
        } finally { $fs.Dispose() }
    } catch {
        return $null
    }
    if ($start -eq 0) { return [System.Text.Encoding]::UTF8.GetString($buf, 0, $read) }
    $nl = [Array]::IndexOf($buf, [byte]10, 0, $read)
    if ($nl -lt 0) { return '' }
    return [System.Text.Encoding]::UTF8.GetString($buf, $nl + 1, $read - $nl - 1)
}

# The first value of "<Key>" "<value>" in an appmanifest (.acf) or another vdf file, as a string, or
# $null. Reads $Path shared; give -Text instead to take several values from one read.
function Get-AcfValue([string]$Path, [string]$Key, [string]$Text = '') {
    if (-not $Text) { $Text = Read-SharedTextFile $Path }
    if (-not $Text) { return $null }
    $m = [regex]::Match($Text, '"' + [regex]::Escape($Key) + '"\s+"([^"]*)"', 'IgnoreCase')
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
}

# $true for an installdir that is a plain folder name: not empty, no path separator, drive colon,
# wildcard, quote or control character, and not only dots (so it stays inside steamapps\common)
function Test-SteamPlainFolderName([string]$Dir) {
    return -not ([string]::IsNullOrWhiteSpace($Dir) -or $Dir -match '[\\/:*?"<>|\x00-\x1F]' -or $Dir -match '^\.+$')
}

# The Steam libraries: the main Steam folder plus every library in libraryfolders.vdf, each as
#   Path       - without a trailing backslash; paths are compared without case
#   DriveReady - the drive is there (a USB disk can be unplugged while Steam keeps it listed)
#   Ready      - drive there and steamapps readable (a deleted library folder: DriveReady, not Ready)
#   AppIds     - the games Steam lists for this library in the vdf (also when it is not Ready)
#   Index      - the number of its block in libraryfolders.vdf (the main folder is 0), as Steam's
#                LastInstallFolderIndex counts (Get-SteamDefaultLibrary); $null if not found
# The drive letter is written in upper case (Steam keeps its own folder as c:\...).
# Paths are built with Path.Combine and tested with -LiteralPath: Join-Path throws for a missing
# drive, and a path with [ ] would be read as a wildcard. The vdf is UTF-8 without BOM. Steam rewrites
# it at every start, so it is read shared and tried again; if it stays unreadable this throws (a
# list without the other libraries would make their games look uninstalled).
function Get-SteamLibraryInfo([string]$SteamRoot) {
    $libs = New-Object System.Collections.ArrayList
    # The block number in front of a "path": the last "<n>" { before it
    $blockRe = New-Object System.Text.RegularExpressions.Regex('"(\d+)"\s*\{', 'RightToLeft')
    $add = {
        param([string]$p, $ids, $index)
        $p = $p.TrimEnd('\')
        if ($p -match '^[a-z]:') { $p = $p.Substring(0, 1).ToUpperInvariant() + $p.Substring(1) }
        foreach ($l in $libs) {
            if ($l.Path -ieq $p) {
                foreach ($i in $ids) { if (-not $l.AppIds.Contains($i)) { [void]$l.AppIds.Add($i) } }
                if ($null -eq $l.Index) { $l.Index = $index }
                return
            }
        }
        $driveReady = $false
        $ready = $false
        try {
            $driveReady = ([System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($p))).IsReady
            $ready = $driveReady -and (Test-Path -LiteralPath ([System.IO.Path]::Combine($p, 'steamapps')))
        } catch { }
        $list = New-Object System.Collections.ArrayList
        foreach ($i in $ids) { if (-not $list.Contains($i)) { [void]$list.Add($i) } }
        [void]$libs.Add([pscustomobject]@{ Path = $p; DriveReady = $driveReady; Ready = $ready; AppIds = $list; Index = $index })
    }
    & $add $SteamRoot @() 0
    foreach ($vdf in @([System.IO.Path]::Combine($SteamRoot, 'steamapps', 'libraryfolders.vdf'), [System.IO.Path]::Combine($SteamRoot, 'config', 'libraryfolders.vdf'))) {
        if (-not (Test-Path -LiteralPath $vdf)) { continue }
        $text = $null
        $lastError = $null
        for ($try = 1; $try -le 5 -and $null -eq $text; $try++) {
            try {
                $fs = [System.IO.File]::Open($vdf, 'Open', 'Read', 'ReadWrite,Delete')
                try { $text = (New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)).ReadToEnd() } finally { $fs.Close() }
                # A file Steam is just writing can be empty or cut off: braces must match
                if ($text -notmatch '"libraryfolders"' -or ([regex]::Matches($text, '\{').Count -ne [regex]::Matches($text, '\}').Count)) {
                    $text = $null
                    $lastError = 'incomplete'
                }
            } catch {
                $lastError = $_.Exception.Message
            }
            if ($null -eq $text) { Start-Sleep -Milliseconds 500 }
        }
        if ($null -eq $text) { throw "libraryfolders.vdf not readable ($vdf): $lastError" }
        # One block per library: "path" "...", later "apps" { "<appid>" "<size>" ... }
        $pm = [regex]::Matches($text, '"path"\s+"([^"]+)"')
        for ($k = 0; $k -lt $pm.Count; $k++) {
            $end = if ($k + 1 -lt $pm.Count) { $pm[$k + 1].Index } else { $text.Length }
            $block = $text.Substring($pm[$k].Index, $end - $pm[$k].Index)
            $ids = @()
            $am = [regex]::Match($block, '"apps"\s*\{([^{}]*)\}')
            if ($am.Success) { $ids = @([regex]::Matches($am.Groups[1].Value, '"(\d+)"\s+"\d+"') | ForEach-Object { $_.Groups[1].Value }) }
            $bm = $blockRe.Match($text, 0, $pm[$k].Index)
            $index = if ($bm.Success) { [int]$bm.Groups[1].Value } else { $null }
            & $add ($pm[$k].Groups[1].Value -replace '\\\\', '\') $ids $index
        }
    }
    return @($libs.ToArray())
}

# Where a game is installed, from the list of Get-SteamLibraryInfo:
#   Library, Manifest - the first Ready library with the game's appmanifest, and that manifest
#   OfflineLibrary    - otherwise a library on a drive that is not there but listed with the game in
#                       libraryfolders.vdf (USB disk unplugged): the game must not be installed twice
# All empty: not installed.
function Find-SteamGameLibrary($LibInfos, [string]$AppId) {
    foreach ($li in @($LibInfos)) {
        if (-not $li.Ready) { continue }
        $p = [System.IO.Path]::Combine($li.Path, 'steamapps', ('appmanifest_{0}.acf' -f $AppId))
        if (Test-Path -LiteralPath $p) { return [pscustomobject]@{ Library = $li.Path; Manifest = $p; OfflineLibrary = $null } }
    }
    $offline = $null
    foreach ($li in @($LibInfos)) {
        if (-not $li.DriveReady -and $li.AppIds.Contains([string]$AppId)) { $offline = $li.Path; break }
    }
    return [pscustomobject]@{ Library = $null; Manifest = $null; OfflineLibrary = $offline }
}

# Another app of the library folder $SteamApps (<library>\steamapps) whose appmanifest names the same
# install folder as $AppId (Steam lets apps share one, e.g. a game and its editor). Only reads.
# $null when there is none; else AppId, Name and Unclear: '' for the first app found, or what could
# not be read (the folder, or a manifest that has no installdir after 3 tries; Steam rewrites
# manifests now and then): then it is not known whether another app shares the folder, and the
# folder must not be deleted. A manifest that stays gone counts for nothing (Steam removed it).
# installdir is compared without case, as Windows names folders.
function Find-SteamSharedInstallDir([string]$SteamApps, [string]$AppId, [string]$InstallDir) {
    if (-not $InstallDir -or -not [System.IO.Directory]::Exists($SteamApps)) { return $null }
    $files = @()
    try { $files = @([System.IO.Directory]::GetFiles($SteamApps, 'appmanifest_*.acf')) } catch {
        return [pscustomobject]@{ AppId = ''; Name = ''; Unclear = "$SteamApps cannot be read: $($_.Exception.Message)" }
    }
    foreach ($f in $files) {
        if ([System.IO.Path]::GetFileName($f) -notmatch '^appmanifest_(\d+)\.acf$' -or $matches[1] -eq $AppId) { continue }
        $other = $matches[1]
        $text = $null
        $dir = $null
        for ($try = 1; $try -le 3 -and -not $dir; $try++) {
            if ($try -gt 1) { Start-Sleep -Milliseconds 200 }
            $text = Read-SharedTextFile $f
            if ($text) { $dir = Get-AcfValue -Key 'installdir' -Text $text }
        }
        if (-not $dir) {
            if (-not [System.IO.File]::Exists($f)) { continue }
            return [pscustomobject]@{ AppId = $other; Name = ''; Unclear = "$f cannot be read or names no installdir" }
        }
        if (-not [string]::Equals([string]$dir, $InstallDir, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        $name = Get-AcfValue -Key 'name' -Text $text
        return [pscustomobject]@{ AppId = $other; Name = $(if ($name) { $name } else { $other }); Unclear = '' }
    }
    return $null
}

# The userdata folder of the Steam account whose settings count, or $null:
#   1. the account logged on in the running client (HKCU\...\Steam\ActiveProcess ActiveUser, 0 while
#      the client is closed)
#   2. else the only account folder
#   3. else the account whose config\localconfig.vdf was written last (the last one logged on)
# (loginusers.vdf has no MostRecent in the current client any more.)
function Get-SteamUserDataFolder([string]$SteamRoot) {
    $userdata = [System.IO.Path]::Combine($SteamRoot, 'userdata')
    $active = Get-ItemProperty 'HKCU:\Software\Valve\Steam\ActiveProcess' -ErrorAction SilentlyContinue
    $id = [int64]0
    if ($active -and [int64]::TryParse("$($active.ActiveUser)", [ref]$id) -and $id -ne 0) {
        $dir = [System.IO.Path]::Combine($userdata, "$id")
        if (Test-Path -LiteralPath $dir -PathType Container) { return $dir }
    }
    $dirs = @(Get-ChildItem -LiteralPath $userdata -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^\d+$' -and $_.Name -ne '0' })
    if ($dirs.Count -eq 1) { return $dirs[0].FullName }
    $latest = $null
    $latestTime = [datetime]::MinValue
    foreach ($d in $dirs) {
        try {
            $f = [System.IO.FileInfo]::new([System.IO.Path]::Combine($d.FullName, 'config', 'localconfig.vdf'))
            if ($f.Exists -and $f.LastWriteTimeUtc -gt $latestTime) { $latest = $d.FullName; $latestTime = $f.LastWriteTimeUtc }
        } catch { }
    }
    return $latest
}

# Steam's default library: the one with the star in Steam > Settings > Storage, which Steam keeps as
# "LastInstallFolderIndex" (the block number in libraryfolders.vdf) in the account's
# config\localconfig.vdf. Steam writes it at once when the star moves; choosing another folder in its
# install dialog does not change it. A missing key means 0 (the main folder), as in Steam.
# $LibInfos: the list of Get-SteamLibraryInfo. Read every time (never cached): the star can move.
# Returns
#   Library  - the entry of $LibInfos (may be on a drive that is not there: check DriveReady)
#   Index    - the index read from Steam, $null when it could not be read
#   FromStar - $true when Library is the star; $false for the fallback to the main Steam folder
#   Problem  - why the star could not be used ('' when it could)
# Only that one value is taken from localconfig.vdf (it holds account data): never log the file.
function Get-SteamDefaultLibrary([string]$SteamRoot, $LibInfos) {
    $libs = @($LibInfos)
    $r = [pscustomobject]@{ Library = $(if ($libs.Count -gt 0) { $libs[0] } else { $null }); Index = $null; FromStar = $false; Problem = '' }
    $account = Get-SteamUserDataFolder $SteamRoot
    if (-not $account) { $r.Problem = 'no Steam account folder found'; return $r }
    $file = [System.IO.Path]::Combine($account, 'config', 'localconfig.vdf')
    # A file Steam is just writing can be cut off: a missing key only counts in a complete file
    $index = $null
    for ($try = 1; $try -le 3 -and $null -eq $index; $try++) {
        $text = Read-SharedTextFile $file
        if ($text) {
            $m = [regex]::Match($text, '"LastInstallFolderIndex"\s+"(\d+)"')
            if ($m.Success) { $index = [int]$m.Groups[1].Value }
            elseif ($text.TrimEnd().EndsWith('}') -and [regex]::Matches($text, '\{').Count -eq [regex]::Matches($text, '\}').Count) { $index = 0 }
        }
        if ($null -eq $index -and $try -lt 3) { Start-Sleep -Milliseconds 300 }
    }
    if ($null -eq $index) { $r.Problem = 'localconfig.vdf of the Steam account not readable'; return $r }
    $r.Index = $index
    $star = @($libs | Where-Object { $_.Index -eq $index }) | Select-Object -First 1
    if (-not $star) { $r.Problem = "Steam's default library (index $index) is not in libraryfolders.vdf"; return $r }
    $r.Library = $star
    $r.FromStar = $true
    return $r
}

# The drive letter of a path in upper case ('F:'), or '' for a path without one (\\server\share)
function Get-SteamDriveLetter([string]$Path) {
    try { $root = [System.IO.Path]::GetPathRoot($Path) } catch { return '' }
    if ($root -match '^([A-Za-z]):') { return $matches[1].ToUpperInvariant() + ':' }
    return ''
}

# Drives that could take a new library: fixed (a USB disk that reports itself as fixed too), ready,
# NTFS (exFAT and ReFS not yet tested with Steam), with a letter and at least $MinFreeGb free. Each as
# Drive ('D:'), FreeGb, TotalGb. Only reads; may wake a sleeping hard disk, so the agent calls it
# seldom. A drive whose <letter>:\SteamLibrary cannot become a new library is left out
# (Get-SteamLibraryFolderProblem: an old libraryfolder.vdf, a folder that is not empty), so HA only
# offers drives an install accepts. Libraries are not taken out here (Get-SteamDriveList does that).
function Get-SteamCandidateDrives([int]$MinFreeGb = 10) {
    $out = New-Object System.Collections.ArrayList
    $all = @()
    try { $all = @([System.IO.DriveInfo]::GetDrives()) } catch { return @() }
    foreach ($d in $all) {
        # Every property can throw (a drive going away, a device error)
        try {
            if ($d.DriveType -ne [System.IO.DriveType]::Fixed -or -not $d.IsReady -or $d.DriveFormat -ne 'NTFS') { continue }
            $letter = Get-SteamDriveLetter $d.Name
            if (-not $letter) { continue }
            $free = [int][math]::Floor($d.AvailableFreeSpace / 1GB)
            if ($free -lt $MinFreeGb) { continue }
            if (Get-SteamLibraryFolderProblem ($letter + '\SteamLibrary')) { continue }
            [void]$out.Add([pscustomobject]@{ Drive = $letter; FreeGb = $free; TotalGb = [int][math]::Round($d.TotalSize / 1GB) })
        } catch { }
    }
    return @($out.ToArray())
}

# BusType and MediaType of MSFT_PhysicalDisk (root/Microsoft/Windows/Storage) as sent to HA
$SteamDiskBus = @{
    1 = 'scsi'; 2 = 'atapi'; 3 = 'ata'; 4 = '1394'; 5 = 'ssa'; 6 = 'fibre_channel'; 7 = 'usb'
    8 = 'raid'; 9 = 'iscsi'; 10 = 'sas'; 11 = 'sata'; 12 = 'sd'; 13 = 'mmc'; 14 = 'virtual'
    15 = 'file_backed_virtual'; 16 = 'spaces'; 17 = 'nvme'; 18 = 'scm'; 19 = 'ufs'
}
$SteamDiskMedia = @{ 3 = 'hdd'; 4 = 'ssd' }

# The kind of disk behind each drive letter: a hashtable 'F:' -> @{ Bus; Media }, Bus as in
# $SteamDiskBus ('usb', 'nvme', 'sata', ...) and Media 'ssd' / 'hdd', each $null when unknown.
# Letter -> partitions (Win32_LogicalDiskToPartition, also for a dynamic volume over several disks)
# -> disk number -> MSFT_PhysicalDisk (DeviceId = disk number). A volume over several disks gets a
# value only when all its disks agree. NVMe without a media type counts as SSD; a disk that
# MSFT_PhysicalDisk does not list (a Storage Spaces disk) still gets 'usb' from Win32_DiskDrive.
# Only reads (CIM, a few 100 ms); every query on its own, a failing one only leaves values $null.
function Get-SteamDriveHardware {
    $out = @{}
    $diskByLetter = @{}
    try {
        foreach ($a in @(Get-CimInstance -ClassName Win32_LogicalDiskToPartition -OperationTimeoutSec 15 -ErrorAction Stop)) {
            $letter = Get-SteamDriveLetter ([string]$a.Dependent.DeviceID + '\')
            # 'Disk #4, Partition #0' (not localized, unlike Name)
            if (-not $letter -or [string]$a.Antecedent.DeviceID -notmatch '^Disk #(\d+),') { continue }
            if (-not $diskByLetter.ContainsKey($letter)) { $diskByLetter[$letter] = New-Object System.Collections.ArrayList }
            if (-not $diskByLetter[$letter].Contains([int]$matches[1])) { [void]$diskByLetter[$letter].Add([int]$matches[1]) }
        }
    } catch { return $out }
    $physical = @{}
    try {
        foreach ($p in @(Get-CimInstance -Namespace 'root/Microsoft/Windows/Storage' -ClassName MSFT_PhysicalDisk -OperationTimeoutSec 15 -ErrorAction Stop)) {
            if ([string]$p.DeviceId -match '^\d+$') { $physical[[int]$p.DeviceId] = $p }
        }
    } catch { }
    $usb = @{}
    try {
        foreach ($d in @(Get-CimInstance -ClassName Win32_DiskDrive -Property Index, InterfaceType -OperationTimeoutSec 15 -ErrorAction Stop)) {
            if ($d.InterfaceType -eq 'USB' -and $null -ne $d.Index) { $usb[[int]$d.Index] = $true }
        }
    } catch { }
    foreach ($letter in $diskByLetter.Keys) {
        $buses = @{}; $medias = @{}
        foreach ($n in $diskByLetter[$letter]) {
            $bus = $null; $media = $null
            $p = $physical[$n]
            if ($p) {
                try { $bus = $SteamDiskBus[[int]$p.BusType] } catch { }
                try { $media = $SteamDiskMedia[[int]$p.MediaType] } catch { }
            }
            if (-not $bus -and $usb.ContainsKey($n)) { $bus = 'usb' }
            if (-not $media -and $bus -eq 'nvme') { $media = 'ssd' }
            $buses[[string]$bus] = $true; $medias[[string]$media] = $true
        }
        # One value only when every disk has the same, known one
        $one = { param($h) if ($h.Count -eq 1) { $k = @($h.Keys)[0]; if ($k) { return $k } }; return $null }
        $out[$letter] = @{ Bus = (& $one $buses); Media = (& $one $medias) }
    }
    return $out
}

# The Steam library a game goes to on drive $Letter ('F:' or 'f'), or $null when no Ready library of
# $LibInfos is there. Several libraries on one drive: $DefaultPath (Steam's default library) wins,
# else the lowest Index (a library without Index last, then the first in the list). The same rule for
# the drives list sent to HA and for the target drive of an install, so both name the same library.
function Get-SteamDriveLibrary($LibInfos, [string]$Letter, [string]$DefaultPath = '') {
    $want = Get-SteamDriveLetter ($Letter.TrimEnd(':') + ':\')
    if (-not $want) { return $null }
    $best = $null
    $bestRank = 0
    foreach ($li in @($LibInfos)) {
        if (-not $li.Ready -or (Get-SteamDriveLetter $li.Path) -ne $want) { continue }
        if ($DefaultPath -and $li.Path -ieq $DefaultPath) { return $li }
        $rank = if ($null -eq $li.Index) { [int]::MaxValue } else { [int]$li.Index }
        if (-not $best -or $rank -lt $bestRank) { $best = $li; $bestRank = $rank }
    }
    return $best
}

# The drives to install to (field "drives" of steam/agent/state), one entry per drive letter:
#   drive ('F:'), free_gb, total_gb, library (path, or $null for a drive without one), default,
#   bus and media (from $Hardware, Get-SteamDriveHardware; $null for a letter it does not have)
# - every drive with a Ready library of $LibInfos; its library by Get-SteamDriveLibrary
# - the drives of $CandidateDrives (Get-SteamCandidateDrives) without any library of $LibInfos
#   (also none that is not Ready: its folder is gone, but Steam still lists it)
# default: the library a first install without a choice uses ($Default of Get-SteamDefaultLibrary).
# None when its drive or folder is not there. Order: default, libraries, drives without library,
# each by letter. $DriveSpace: FreeGb and TotalGb per drive letter, measured by the caller for its
# libraries this round (each drive is read once per round); a library drive without it is left out.
function Get-SteamDriveList($LibInfos, $Default, $CandidateDrives, [hashtable]$DriveSpace, [hashtable]$Hardware) {
    $defaultPath = if ($Default -and $Default.Library) { $Default.Library.Path } else { '' }
    $withLib = @{}
    $ready = @{}
    foreach ($li in @($LibInfos)) {
        $l = Get-SteamDriveLetter $li.Path
        if (-not $l) { continue }
        $withLib[$l] = $true
        if ($li.Ready) { $ready[$l] = $true }
    }
    $list = New-Object System.Collections.ArrayList
    foreach ($letter in @($ready.Keys | Sort-Object)) {
        $lib = Get-SteamDriveLibrary $LibInfos $letter $defaultPath
        $s = if ($DriveSpace) { $DriveSpace[$letter] } else { $null }
        if (-not $lib -or -not $s) { continue }
        [void]$list.Add([ordered]@{
            drive    = $letter
            free_gb  = $s.FreeGb
            total_gb = $s.TotalGb
            library  = $lib.Path
            default  = [bool]($defaultPath -and $lib.Path -ieq $defaultPath)
            bus      = $(if ($Hardware -and $Hardware[$letter]) { $Hardware[$letter].Bus } else { $null })
            media    = $(if ($Hardware -and $Hardware[$letter]) { $Hardware[$letter].Media } else { $null })
        })
    }
    foreach ($cd in @($CandidateDrives | Sort-Object Drive)) {
        if ($withLib.ContainsKey($cd.Drive)) { continue }
        $hw = if ($Hardware) { $Hardware[$cd.Drive] } else { $null }
        [void]$list.Add([ordered]@{
            drive = $cd.Drive; free_gb = $cd.FreeGb; total_gb = $cd.TotalGb; library = $null; default = $false
            bus = $(if ($hw) { $hw.Bus } else { $null }); media = $(if ($hw) { $hw.Media } else { $null })
        })
    }
    # The default first, the rest in its order
    return @(@($list | Where-Object { $_['default'] }) + @($list | Where-Object { -not $_['default'] }))
}

# --- new Steam library ---------------------------------------------------------------------------
# Steam has no interface to add a library. With the client closed, New-SteamLibrary adds one the way
# Steam's own storage manager stores it: <X>:\SteamLibrary\steamapps, a libraryfolder.vdf in the folder
# and a new block in libraryfolders.vdf (config\ and the copy in steamapps\, which Steam reads at its
# start). Steam resets ALL libraries to its main folder when it cannot parse libraryfolders.vdf, so
# every step is checked, the old file is kept as a backup and put back on any failure.

# The library blocks of a libraryfolders.vdf text as a list of Index, Path (unescaped) and ContentId,
# or $null when the text is not a complete, well-formed libraryfolders.vdf (braces, quotes, nothing
# after the last brace but white space).
function Get-SteamLibraryBlocks([string]$Text) {
    if (-not $Text) { return $null }
    $tokens = [regex]::Matches($Text, '"((?:[^"\\]|\\.)*)"|\{|\}|(\S)')
    $blocks = New-Object System.Collections.ArrayList
    $depth = 0; $done = $false; $key = $null; $blockKey = $null; $cur = $null; $first = $true
    foreach ($t in $tokens) {
        if ($t.Groups[2].Success -or $done) { return $null }
        if ($first) {
            if ($t.Value -ne '"libraryfolders"') { return $null }
            $first = $false
            continue
        }
        if ($t.Value -eq '{') {
            if ($null -eq $key -and $depth -gt 0) { return $null }
            $depth++
            if ($depth -eq 2) { $blockKey = $key; $cur = @{ Path = $null; ContentId = $null } }
            $key = $null
        } elseif ($t.Value -eq '}') {
            if ($null -ne $key -or $depth -le 0) { return $null }
            if ($depth -eq 2 -and $blockKey -match '^\d+$') {
                [void]$blocks.Add([pscustomobject]@{ Index = [int]$blockKey; Path = $cur.Path; ContentId = $cur.ContentId })
            }
            $depth--
            if ($depth -eq 0) { $done = $true }
        } elseif ($null -eq $key) {
            if ($depth -eq 0) { return $null }
            $key = $t.Groups[1].Value
        } else {
            if ($depth -eq 2) {
                $v = [regex]::Replace($t.Groups[1].Value, '\\(.)', '$1')
                if ($key -ieq 'path') { $cur.Path = $v } elseif ($key -ieq 'contentid') { $cur.ContentId = $v }
            }
            $key = $null
        }
    }
    if (-not $done) { return $null }
    return , @($blocks.ToArray())
}

# Why $LibraryPath cannot become a new library, or '' when it can: it must not exist or be an empty
# folder. An old library (libraryfolder.vdf) is never taken over or overwritten. Only reads.
function Get-SteamLibraryFolderProblem([string]$LibraryPath) {
    try {
        if ([System.IO.File]::Exists($LibraryPath)) { return "$LibraryPath is a file" }
        if (-not [System.IO.Directory]::Exists($LibraryPath)) { return '' }
        if ([System.IO.File]::Exists([System.IO.Path]::Combine($LibraryPath, 'libraryfolder.vdf'))) { return "$LibraryPath holds an old Steam library (libraryfolder.vdf)" }
        if (@([System.IO.Directory]::EnumerateFileSystemEntries($LibraryPath)).Count -gt 0) { return "$LibraryPath is not empty" }
        return ''
    } catch {
        return "$LibraryPath cannot be read: $($_.Exception.Message)"
    }
}

# $true when a Steam client of $SteamRoot runs (a process whose path cannot be read counts as one)
function Test-SteamClientRunningFrom([string]$SteamRoot) {
    foreach ($p in @(Get-Process -Name steam -ErrorAction SilentlyContinue)) {
        $exe = $null
        try { $exe = $p.Path } catch { }
        if (-not $exe) { return $true }
        if ([System.IO.Path]::GetDirectoryName($exe).TrimEnd('\') -ieq $SteamRoot.TrimEnd('\')) { return $true }
    }
    return $false
}

# Writes $Bytes to $Path through a temporary file and File.Replace, so a reader never sees half a file
function Write-SteamFileAtomic([string]$Path, [byte[]]$Bytes) {
    $tmp = $Path + '.steamengine_new'
    try {
        [System.IO.File]::WriteAllBytes($tmp, $Bytes)
        if ([System.IO.File]::Exists($Path)) {
            # [NullString]::Value: a plain $null would arrive as '' (no backup file wanted here)
            [System.IO.File]::Replace($tmp, $Path, [NullString]::Value)
        } else {
            [System.IO.File]::Move($tmp, $Path)
        }
    } finally {
        if ([System.IO.File]::Exists($tmp)) { try { [System.IO.File]::Delete($tmp) } catch { } }
    }
}

# Puts back the libraryfolders.vdf files New-SteamLibrary changed (its result $Library): the backup,
# or no file where none was. $true when every file is back byte for byte. Only while Steam is closed.
function Restore-SteamLibraryConfig($Library) {
    $ok = $true
    foreach ($f in @($Library.Files)) {
        try {
            if ($f.Backup) {
                $a = [System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($f.Backup))
                # a file that was never changed (its write failed) is left as it is
                if ([System.IO.File]::Exists($f.File) -and [System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($f.File)) -eq $a) { continue }
                [System.IO.File]::Copy($f.Backup, $f.File, $true)
                if ([System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($f.File)) -ne $a) { $ok = $false }
            } elseif ([System.IO.File]::Exists($f.File)) {
                [System.IO.File]::Delete($f.File)
            }
        } catch {
            $ok = $false
        }
    }
    return $ok
}

# The marker of a libraryfolders.vdf that could not be put back after a failed new library: it names
# the backups to restore by hand. While it exists New-SteamLibrary refuses and no backup is removed,
# so the only good copy is never lost. The user deletes it after the restore.
function Get-SteamLibraryRestorePendingFile([string]$SteamRoot) {
    return [System.IO.Path]::Combine($SteamRoot.TrimEnd('\'), 'config', 'libraryfolders.vdf.restore_pending')
}

# Writes that marker for $Library (the result of New-SteamLibrary) with its backups; $Why one line
function Set-SteamLibraryRestorePending($Library, [string]$Why) {
    $lines = @('libraryfolders.vdf was not put back after a new Steam library failed: ' + $Why,
        'Close Steam, copy each backup over its file, then delete this file:')
    foreach ($f in @($Library.Files)) {
        if ($f.Backup) { $lines += ('{0} -> {1}' -f $f.Backup, $f.File) } else { $lines += ('(no backup, delete) {0}' -f $f.File) }
    }
    try {
        [System.IO.File]::AppendAllText((Get-SteamLibraryRestorePendingFile $Library.SteamRoot), (($lines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding $false))
    } catch { }
}

# Removes older libraryfolders.vdf.bak_<yyyyMMdd_HHmmss> next to the backups of $Library, once Steam
# took the new library and kept every library from before (never while a restore is pending).
function Remove-OldSteamLibraryBackups($Library) {
    if ([System.IO.File]::Exists((Get-SteamLibraryRestorePendingFile $Library.SteamRoot))) { return }
    foreach ($f in @($Library.Files)) {
        if (-not $f.Backup) { continue }
        try {
            $dir = [System.IO.Path]::GetDirectoryName($f.File)
            foreach ($o in @([System.IO.Directory]::GetFiles($dir, 'libraryfolders.vdf.bak_*'))) {
                if ($o -ine $f.Backup -and [System.IO.Path]::GetFileName($o) -match '^libraryfolders\.vdf\.bak_\d{8}_\d{6}$') { [System.IO.File]::Delete($o) }
            }
        } catch { }
    }
}

# Removes what New-SteamLibrary created in the library folder (its result $Library): its
# libraryfolder.vdf, then folders that are empty (Steam may have added some), the library folder only
# when New-SteamLibrary made it. Files Steam put there stay. $true when nothing of it is left.
function Remove-SteamLibraryFolder($Library) {
    $path = $Library.Path
    try {
        if ($Library.CreatedMarker) {
            $marker = [System.IO.Path]::Combine($path, 'libraryfolder.vdf')
            if ([System.IO.File]::Exists($marker)) { [System.IO.File]::Delete($marker) }
        }
        if ($Library.CreatedSteamapps -and [System.IO.Directory]::Exists($path)) {
            # deepest first; a folder with a file in it stays, and so do its parents
            $dirs = @([System.IO.Directory]::GetDirectories([System.IO.Path]::Combine($path, 'steamapps'), '*', 'AllDirectories') | Sort-Object Length -Descending)
            foreach ($d in ($dirs + @([System.IO.Path]::Combine($path, 'steamapps')))) {
                if ([System.IO.Directory]::Exists($d) -and @([System.IO.Directory]::EnumerateFileSystemEntries($d)).Count -eq 0) { [System.IO.Directory]::Delete($d) }
            }
        }
        if ($Library.CreatedRoot -and [System.IO.Directory]::Exists($path) -and @([System.IO.Directory]::EnumerateFileSystemEntries($path)).Count -eq 0) {
            [System.IO.Directory]::Delete($path)
        }
    } catch {
        return $false
    }
    if ($Library.CreatedRoot) { return -not [System.IO.Directory]::Exists($path) }
    return -not [System.IO.Directory]::Exists([System.IO.Path]::Combine($path, 'steamapps'))
}

# Adds the Steam library <Drive>\SteamLibrary (Drive like 'E:'). Only while the Steam client of
# $SteamRoot is closed (refused otherwise). -LibraryPath gives another folder instead (tests).
# Refused (nothing changed, exception Data['Code'] = 'drive_not_suitable'): Steam runs, a library is
# registered on that drive or at that path, or the folder exists and is not empty or holds an old
# library (libraryfolder.vdf). Refused with 'library_not_created': an earlier failure left a restore
# pending (Get-SteamLibraryRestorePendingFile). Steps: the new libraryfolders.vdf text is built and checked first (it
# must parse, keep every old block with its index, path and contentid, and add exactly the new one);
# then both libraryfolders.vdf are backed up as <file>.bak_<yyyyMMdd_HHmmss> (older ones are removed
# only after Steam took the library, Remove-OldSteamLibraryBackups), the folders and libraryfolder.vdf are created, both libraryfolders.vdf written
# atomically and read back. Any failure after the first change puts the backups back, removes what
# was created and throws (Data['Code'] = 'library_not_created'); a restore that fails leaves the
# pending marker. Returns SteamRoot, Path, ContentId, Index, OldPaths (the library paths in libraryfolders.vdf before), Files (File, Backup per
# libraryfolders.vdf, Backup $null when there was none), CreatedRoot, CreatedSteamapps, CreatedMarker:
# what Restore-SteamLibraryConfig and Remove-SteamLibraryFolder need to take it back.
function New-SteamLibrary([string]$SteamRoot, [string]$Drive, [string]$LibraryPath = '') {
    $fail = {
        param([string]$Message, [string]$Code)
        $ex = New-Object System.InvalidOperationException($Message)
        $ex.Data['Code'] = $Code
        throw $ex
    }
    $SteamRoot = $SteamRoot.TrimEnd('\')
    $letter = Get-SteamDriveLetter ($Drive.TrimEnd(':', '\') + ':\')
    if (-not $letter) { & $fail "no drive letter in '$Drive'" 'drive_not_suitable' }
    if (-not $LibraryPath) { $LibraryPath = $letter + '\SteamLibrary' }
    $LibraryPath = $LibraryPath.TrimEnd('\')
    if (Test-SteamClientRunningFrom $SteamRoot) { & $fail 'the Steam client is running' 'drive_not_suitable' }
    $pending = Get-SteamLibraryRestorePendingFile $SteamRoot
    if ([System.IO.File]::Exists($pending)) { & $fail "an earlier new library left libraryfolders.vdf unrestored, see $pending" 'library_not_created' }

    # The current state: both files, read once (Steam is closed, nobody writes them)
    $files = @(
        [System.IO.Path]::Combine($SteamRoot, 'config', 'libraryfolders.vdf'),
        [System.IO.Path]::Combine($SteamRoot, 'steamapps', 'libraryfolders.vdf'))
    $old = @{}
    $source = $null
    foreach ($f in $files) {
        if (-not [System.IO.File]::Exists($f)) { continue }
        $bytes = [System.IO.File]::ReadAllBytes($f)
        $bom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
        $text = if ($bom) { [System.Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3) } else { [System.Text.Encoding]::UTF8.GetString($bytes) }
        $blocks = Get-SteamLibraryBlocks $text
        if ($null -eq $blocks) { & $fail "$f cannot be parsed" 'library_not_created' }
        foreach ($b in $blocks) { $old[('{0}|{1}|{2}' -f $b.Index, "$($b.Path)".ToLowerInvariant(), $b.ContentId)] = $b }
        # config\ counts (Steam keeps both the same); the steamapps copy when it is the only one
        if (-not $source) { $source = [pscustomobject]@{ Text = $text; Bom = $bom; Blocks = $blocks } }
    }
    if (-not $source) { & $fail 'no libraryfolders.vdf found' 'library_not_created' }
    $oldBlocks = @($old.Values)
    $libs = @(Get-SteamLibraryInfo $SteamRoot)
    foreach ($l in $libs) {
        if ((Get-SteamDriveLetter $l.Path) -eq $letter) { & $fail "a Steam library is registered on $letter already ($($l.Path))" 'drive_not_suitable' }
        if ($l.Path -ieq $LibraryPath) { & $fail "$LibraryPath is registered as a Steam library already" 'drive_not_suitable' }
    }
    foreach ($b in $oldBlocks) {
        if ("$($b.Path)".TrimEnd('\') -ieq $LibraryPath) { & $fail "$LibraryPath is registered as a Steam library already" 'drive_not_suitable' }
    }
    $problem = Get-SteamLibraryFolderProblem $LibraryPath
    if ($problem) { & $fail $problem 'drive_not_suitable' }
    $root = [System.IO.Path]::GetPathRoot($LibraryPath)
    if (-not [System.IO.Directory]::Exists($root)) { & $fail "the drive $root is not there" 'drive_not_suitable' }

    # A new contentid: 1..2^63-1, none of the others
    $ids = @($oldBlocks | ForEach-Object { $_.ContentId })
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $buf = New-Object byte[] 8
    do {
        $rng.GetBytes($buf)
        $cid = [System.BitConverter]::ToInt64($buf, 0) -band [int64]::MaxValue
    } while ($cid -eq 0 -or $ids -contains "$cid")
    $rng.Dispose()
    $contentId = "$cid"
    $index = 1
    foreach ($b in $oldBlocks) { if ($b.Index -ge $index) { $index = $b.Index + 1 } }

    # The new text: one block more before the last brace, in Steam's layout (tabs, its line ending)
    $text = $source.Text
    $nl = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $esc = $LibraryPath.Replace('\', '\\')
    $block = (@(
        "`t`"$index`"", "`t{",
        "`t`t`"path`"`t`t`"$esc`"",
        "`t`t`"label`"`t`t`"`"",
        "`t`t`"contentid`"`t`t`"$contentId`"",
        "`t`t`"totalsize`"`t`t`"0`"",
        "`t`t`"update_clean_bytes_tally`"`t`t`"0`"",
        "`t`t`"time_last_update_verified`"`t`t`"0`"",
        "`t`t`"apps`"", "`t`t{", "`t`t}", "`t}") -join $nl) + $nl
    $last = $text.LastIndexOf('}')
    $lineStart = $text.LastIndexOf("`n", [Math]::Max(0, $last - 1)) + 1
    if ($lineStart -le 0 -or $text.Substring($lineStart, $last - $lineStart).Trim()) {
        $newText = $text.Substring(0, $last) + $nl + $block + $text.Substring($last)
    } else {
        $newText = $text.Substring(0, $lineStart) + $block + $text.Substring($lineStart)
    }
    $want = [pscustomobject]@{ Index = $index; Path = $LibraryPath; ContentId = $contentId }
    if (-not (Test-SteamLibraryConfig $newText $oldBlocks $want)) { & $fail 'the new libraryfolders.vdf did not pass the check, nothing changed' 'library_not_created' }
    $newBytes = [System.Text.Encoding]::UTF8.GetBytes($newText)
    if ($source.Bom) { $newBytes = [byte[]](@(0xEF, 0xBB, 0xBF) + $newBytes) }

    $result = [pscustomobject]@{
        SteamRoot = $SteamRoot; Path = $LibraryPath; ContentId = $contentId; Index = $index
        OldPaths = @($oldBlocks | ForEach-Object { "$($_.Path)".TrimEnd('\') } | Sort-Object -Unique)
        Files = @(); CreatedRoot = $false; CreatedSteamapps = $false; CreatedMarker = $false
    }
    $stamp = [DateTime]::Now.ToString('yyyyMMdd_HHmmss', [System.Globalization.CultureInfo]::InvariantCulture)
    try {
        foreach ($f in $files) {
            $bak = $null
            if ([System.IO.File]::Exists($f)) {
                $bak = $f + '.bak_' + $stamp
                [System.IO.File]::Copy($f, $bak, $true)
            }
            $result.Files += [pscustomobject]@{ File = $f; Backup = $bak }
        }
        if (-not [System.IO.Directory]::Exists($LibraryPath)) {
            [void][System.IO.Directory]::CreateDirectory($LibraryPath)
            $result.CreatedRoot = $true
        }
        $steamapps = [System.IO.Path]::Combine($LibraryPath, 'steamapps')
        [void][System.IO.Directory]::CreateDirectory($steamapps)
        $result.CreatedSteamapps = $true
        $marker = (@('"libraryfolder"', '{',
            "`t`"contentid`"`t`t`"$contentId`"",
            "`t`"label`"`t`t`"`"",
            ("`t`"launcher`"`t`t`"{0}`"" -f [System.IO.Path]::Combine($SteamRoot, 'steam.exe').Replace('\', '\\')),
            '}') -join $nl) + $nl
        [System.IO.File]::WriteAllText([System.IO.Path]::Combine($LibraryPath, 'libraryfolder.vdf'), $marker, (New-Object System.Text.UTF8Encoding $false))
        $result.CreatedMarker = $true
        foreach ($f in $files) {
            if (-not [System.IO.Directory]::Exists([System.IO.Path]::GetDirectoryName($f))) { continue }
            Write-SteamFileAtomic $f $newBytes
            # read back: what Steam will read
            $back = [System.IO.File]::ReadAllBytes($f)
            if ([System.BitConverter]::ToString($back) -ne [System.BitConverter]::ToString($newBytes)) { throw "$f differs after writing" }
            $t = if ($source.Bom) { [System.Text.Encoding]::UTF8.GetString($back, 3, $back.Length - 3) } else { [System.Text.Encoding]::UTF8.GetString($back) }
            if (-not (Test-SteamLibraryConfig $t $oldBlocks $want)) { throw "$f did not pass the check after writing" }
        }
    } catch {
        $why = $_.Exception.Message
        $restored = Restore-SteamLibraryConfig $result
        $removed = Remove-SteamLibraryFolder $result
        $msg = "new library $LibraryPath not created: $why"
        if (-not $restored) {
            $msg += '; libraryfolders.vdf NOT restored, backups: ' + (@($result.Files | Where-Object { $_.Backup } | ForEach-Object { $_.Backup }) -join ', ')
            Set-SteamLibraryRestorePending $result $why
        }
        if (-not $removed) { $msg += "; $LibraryPath not fully removed" }
        & $fail $msg 'library_not_created'
    }
    return $result
}

# $true when $Text is a well-formed libraryfolders.vdf that keeps every block of $OldBlocks (same
# index, path and contentid) and has $New (Index, Path, ContentId) as the only other block
function Test-SteamLibraryConfig([string]$Text, $OldBlocks, $New) {
    $blocks = Get-SteamLibraryBlocks $Text
    if ($null -eq $blocks) { return $false }
    if ($blocks.Count -ne @($OldBlocks).Count + 1) { return $false }
    $have = @{}
    foreach ($b in $blocks) { $have[('{0}|{1}|{2}' -f $b.Index, "$($b.Path)".ToLowerInvariant(), $b.ContentId)] = $true }
    foreach ($b in @($OldBlocks)) {
        if (-not $have.ContainsKey(('{0}|{1}|{2}' -f $b.Index, "$($b.Path)".ToLowerInvariant(), $b.ContentId))) { return $false }
    }
    return $have.ContainsKey(('{0}|{1}|{2}' -f $New.Index, $New.Path.ToLowerInvariant(), $New.ContentId))
}

# --- space and Steam logs (install.ps1, move.ps1) -------------------------------------------------

# The reserve a job keeps free on a drive besides a game of $SizeBytes: 10 % of the game size, at
# least 5 GB. The integration (space.py) and the card use the same rule.
function Get-SteamSpaceReserve([int64]$SizeBytes) {
    return [math]::Max([int64]5GB, [int64]($SizeBytes * 0.10))
}

# The test value of the test switch $TestFile (space_test_free_gb.txt next to the jobs, see
# PROTOCOL.md) for the drive of $Path, or $null: one value per line, 'F:=20' for one drive (also
# 'f=20'), a plain number for every drive not named; anything else is ignored. No file, or nothing
# for this drive: $null (the real free space counts).
function Get-SteamTestFreeGb([string]$TestFile, [string]$Path) {
    if (-not $TestFile) { return $null }
    $t = Read-SharedTextFile $TestFile
    if (-not $t) { return $null }
    $letter = Get-SteamDriveLetter $Path
    $all = $null
    foreach ($line in ($t -split "`r?`n")) {
        $l = $line.Trim()
        if ($l -match '^(\d+)$') { $all = [int64]$matches[1]; continue }
        if ($l -match '^([A-Za-z]):?\s*=\s*(\d+)$' -and ($matches[1].ToUpperInvariant() + ':') -eq $letter) { return [int64]$matches[2] }
    }
    return $all
}

# The free bytes on the drive of $Path, or the test value of $TestFile (Get-SteamTestFreeGb)
function Get-SteamFreeBytes([string]$TestFile, [string]$Path) {
    $test = Get-SteamTestFreeGb $TestFile $Path
    if ($null -ne $test) { return $test * 1GB }
    return [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($Path)).AvailableFreeSpace
}

# Complete lines appended to a Steam log since $Offset (bytes). Moves $Offset only past the last
# complete line, so a line Steam is still writing comes whole with the next call. A log shorter than
# $Offset was started anew and is read from its beginning.
function Read-SteamLogLines([string]$Path, [ref]$Offset) {
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    $text = ''
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite,Delete')
        try {
            $len = $fs.Length
            if ($len -lt $Offset.Value) { $Offset.Value = 0 }
            $count = [int][Math]::Min(16MB, $len - $Offset.Value)
            if ($count -le 0) { return @() }
            [void]$fs.Seek($Offset.Value, 'Begin')
            $buf = New-Object byte[] $count
            $n = $fs.Read($buf, 0, $count)
            if ($n -le 0) { return @() }
            $last = [Array]::LastIndexOf($buf, [byte]10, $n - 1)
            if ($last -lt 0) { return @() }
            $text = [System.Text.Encoding]::UTF8.GetString($buf, 0, $last + 1)
            $Offset.Value += $last + 1
        } finally { $fs.Close() }
    } catch { return @() }
    return @($text -split "`r?`n" | Where-Object { $_ })
}

# The length of content_log.txt of $SteamRoot right now (0 when there is none): the
# offset to read Steam's lines after a client start from (Read-SteamLogLines)
function Get-SteamContentLogOffset([string]$SteamRoot) {
    $f = [System.IO.Path]::Combine($SteamRoot, 'logs', 'content_log.txt')
    try { if ([System.IO.File]::Exists($f)) { return ([System.IO.FileInfo]::new($f)).Length } } catch { }
    return [int64]0
}

# After a client start: did Steam take the library New-SteamLibrary added ($Library)? Reads
# content_log of $SteamRoot from $Offset (taken before the start) for
#   Loaded <n> apps from install folder "<library>\steamapps\..."  (also for 0 apps)
#   Triggered async write of Steam library folders configuration   (Steam writes the vdf back)
# then reads config\libraryfolders.vdf. $OnWait (optional) runs every 2 s while it waits (progress).
# Returns State 'ok', 'refused' (no such line within $TimeoutSec) or 'lost' (Steam reset its
# libraries, or a library from before is missing), and Note: a line for the caller's log or ''.
function Wait-SteamLibraryAccepted([string]$SteamRoot, $Library, [int64]$Offset, [int]$TimeoutSec = 90, [scriptblock]$OnWait = $null) {
    $log = [System.IO.Path]::Combine($SteamRoot, 'logs', 'content_log.txt')
    $loadedRe = '(?i)Loaded \d+ apps from install folder "' + [regex]::Escape($Library.Path + '\steamapps')
    $loaded = $false; $written = $false; $reset = $false
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec -and -not $reset -and -not ($loaded -and $written)) {
        Start-Sleep -Seconds 2
        if ($OnWait) { & $OnWait }
        foreach ($line in (Read-SteamLogLines $log ([ref]$Offset))) {
            if ($line -match $loadedRe) { $loaded = $true }
            elseif ($line -match 'Triggered async write of Steam library folders configuration') { if ($loaded) { $written = $true } }
            elseif ($line -match 'library folders configuration will be reset|Load failed: .*libraryfolders\.vdf') { $reset = $true }
        }
    }
    if ($reset) { return [pscustomobject]@{ State = 'lost'; Note = 'Steam could not load libraryfolders.vdf and reset its libraries.' } }
    # the async write lands a moment after its log line
    Start-Sleep -Seconds 3
    $file = [System.IO.Path]::Combine($SteamRoot, 'config', 'libraryfolders.vdf')
    $blocks = $null
    for ($try = 1; $try -le 5 -and $null -eq $blocks; $try++) {
        $blocks = Get-SteamLibraryBlocks (Read-SharedTextFile $file)
        if ($null -eq $blocks) { Start-Sleep -Seconds 1 }
    }
    if ($null -eq $blocks) { return [pscustomobject]@{ State = 'lost'; Note = 'libraryfolders.vdf not readable after the start of the Steam client.' } }
    $now = @($blocks | ForEach-Object { "$($_.Path)".TrimEnd('\') })
    $missing = @($Library.OldPaths | Where-Object { $p = $_; -not ($now | Where-Object { $_ -ieq $p }) })
    if ($missing.Count -gt 0) { return [pscustomobject]@{ State = 'lost'; Note = "Steam dropped libraries from libraryfolders.vdf: $($missing -join ', ')." } }
    if (-not $loaded) { return [pscustomobject]@{ State = 'refused'; Note = '' } }
    $note = if (-not $written) { "Note: Steam loaded the new library $($Library.Path), no write of libraryfolders.vdf seen." } else { '' }
    return [pscustomobject]@{ State = 'ok'; Note = $note }
}

# --- game folders and what runs from them (move.ps1, uninstall.ps1, the agent) ---------------------

# $true when $Path exists and is a junction or symbolic link
function Test-SteamLink([string]$Path) {
    try {
        if (-not $Path -or -not ([System.IO.Directory]::Exists($Path) -or [System.IO.File]::Exists($Path))) { return $false }
        return (([System.IO.File]::GetAttributes($Path) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
    } catch { return $false }
}

# Deletes the folder $Path with everything in it, without following links: a junction or symbolic
# link (also $Path itself) is removed itself, never what it points to (Remove-Item -Recurse of Windows
# PowerShell 5.1 deletes the files behind a junction). Read-only files are deleted too. An entry that
# cannot be deleted (locked) is passed over, the rest still goes; then one more try after $RetrySec.
# Returns '' when $Path is gone (or was no folder), else what failed. $OnWait runs every 500 entries
# (progress during long deletes).
function Remove-SteamFolder([string]$Path, [scriptblock]$OnWait = $null, [int]$RetrySec = 2) {
    if (-not $Path -or -not [System.IO.Directory]::Exists($Path)) { return '' }
    $problem = ''
    for ($try = 1; $try -le 2; $try++) {
        $problem = Remove-SteamFolderOnce $Path $OnWait
        if (-not [System.IO.Directory]::Exists($Path)) { return '' }
        if ($try -eq 1) { Start-Sleep -Seconds $RetrySec }
    }
    if (-not $problem) { $problem = "$Path is still there" }
    return $problem
}

# One pass of Remove-SteamFolder; returns what failed ('' when nothing did)
function Remove-SteamFolderOnce([string]$Path, [scriptblock]$OnWait) {
    $errors = New-Object System.Collections.ArrayList
    $root = $null
    try {
        $root = [System.IO.DirectoryInfo]::new($Path)
        if (($root.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { $root.Delete(); return '' }
    } catch { return $_.Exception.Message }
    $dirs = New-Object System.Collections.ArrayList
    $stack = New-Object System.Collections.Stack
    $stack.Push($root)
    $n = 0
    while ($stack.Count -gt 0) {
        $d = $stack.Pop()
        [void]$dirs.Add($d)
        $entries = @()
        try { $entries = @($d.EnumerateFileSystemInfos()) } catch { [void]$errors.Add("$($d.FullName): $($_.Exception.Message)"); continue }
        foreach ($e in $entries) {
            $n++
            if ($OnWait -and $n % 500 -eq 0) { & $OnWait }
            try {
                if (($e.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                    if ($e -is [System.IO.DirectoryInfo]) { $e.Delete() } else { $e.Attributes = 'Normal'; $e.Delete() }
                } elseif ($e -is [System.IO.DirectoryInfo]) {
                    $stack.Push($e)
                } else {
                    if (($e.Attributes -band [System.IO.FileAttributes]::ReadOnly) -ne 0) { $e.Attributes = 'Normal' }
                    $e.Delete()
                }
            } catch { [void]$errors.Add("$($e.FullName): $($_.Exception.Message)") }
        }
    }
    # deepest first; a folder that still holds what could not be deleted stays
    for ($i = $dirs.Count - 1; $i -ge 0; $i--) {
        try {
            $dirs[$i].Refresh()
            if (($dirs[$i].Attributes -band [System.IO.FileAttributes]::ReadOnly) -ne 0) { $dirs[$i].Attributes = 'Directory' }
            $dirs[$i].Delete()
        } catch { if ($errors.Count -eq 0) { [void]$errors.Add("$($dirs[$i].FullName): $($_.Exception.Message)") } }
    }
    if ($errors.Count -eq 0) { return '' }
    return ('{0} entries not deleted, first: {1}' -f $errors.Count, $errors[0])
}

# The type SteamEngineProcess: the path of a process's executable (also of an elevated one, whose
# Process.Path cannot be read without admin rights) and the final path of a folder. Added once per
# PowerShell process, on first use; throws when it cannot be compiled.
function Initialize-SteamProcessType {
    if ('SteamEngineProcess' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class SteamEngineProcess {
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern bool QueryFullProcessImageName(IntPtr process, int flags, StringBuilder name, ref int size);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    // null when the process cannot be opened (protected, gone)
    public static string ImagePath(int pid) {
        IntPtr h = OpenProcess(0x1000, false, pid);   // PROCESS_QUERY_LIMITED_INFORMATION
        if (h == IntPtr.Zero) { return null; }
        try {
            StringBuilder sb = new StringBuilder(32768);
            int size = sb.Capacity;
            if (!QueryFullProcessImageName(h, 0, sb, ref size)) { return null; }
            return sb.ToString(0, size);
        } finally { CloseHandle(h); }
    }
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern IntPtr CreateFile(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern uint GetFinalPathNameByHandle(IntPtr file, StringBuilder path, uint size, uint flags);
    // The path of a folder with junctions and symbolic links followed (as the kernel reports an
    // image path); null when it cannot be opened
    public static string FinalPath(string path) {
        // No access rights needed; FILE_FLAG_BACKUP_SEMANTICS (0x02000000) opens a directory
        IntPtr h = CreateFile(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero);   // share all, OPEN_EXISTING
        if (h == new IntPtr(-1)) { return null; }
        try {
            StringBuilder sb = new StringBuilder(32768);
            uint n = GetFinalPathNameByHandle(h, sb, (uint)sb.Capacity, 0);
            if (n == 0 || n >= (uint)sb.Capacity) { return null; }
            string s = sb.ToString(0, (int)n);
            if (s.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) { return @"\\" + s.Substring(8); }
            if (s.StartsWith(@"\\?\")) { return s.Substring(4); }
            return s;
        } finally { CloseHandle(h); }
    }
}
'@
}

# The path of the executable of process $Id, or $null (gone, protected)
function Get-SteamProcessImagePath([int]$Id) {
    Initialize-SteamProcessType
    return [SteamEngineProcess]::ImagePath($Id)
}

# The processes whose executable lies in one of $Folders (full paths, compared without case and with a
# trailing backslash), each as Id, Name (with .exe), Path, SessionId and the Process object. A folder
# behind a junction or symbolic link is matched by its target path as well (the kernel reports that).
# Left out: this process, the system's (PID 4 and below) and those whose path cannot be read.
function Get-SteamFolderProcesses([string[]]$Folders) {
    Initialize-SteamProcessType
    $prefixes = New-Object System.Collections.ArrayList
    foreach ($f in @($Folders)) {
        if (-not $f) { continue }
        foreach ($p in @($f, [SteamEngineProcess]::FinalPath($f))) {
            if (-not $p) { continue }
            $p = $p.TrimEnd('\') + '\'
            # never a drive root
            if ($p.Length -gt 3 -and -not ($prefixes | Where-Object { $_ -ieq $p })) { [void]$prefixes.Add($p) }
        }
    }
    if ($prefixes.Count -eq 0) { return @() }
    $result = New-Object System.Collections.ArrayList
    foreach ($proc in [System.Diagnostics.Process]::GetProcesses()) {
        try {
            if ($proc.Id -le 4 -or $proc.Id -eq $PID) { continue }
            $path = [SteamEngineProcess]::ImagePath($proc.Id)
            if (-not $path) { continue }
            foreach ($p in $prefixes) {
                if (-not $path.StartsWith($p, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
                [void]$result.Add([pscustomobject]@{ Id = $proc.Id; Name = $proc.ProcessName + '.exe'; Path = $path; SessionId = $proc.SessionId; Process = $proc })
                break
            }
        } catch { }
    }
    return @($result.ToArray())
}

# The app IDs Steam runs right now (strings): RunningAppID and every app with Running = 1 under
# HKCU\Software\Valve\Steam\Apps (several games at once)
function Get-RunningSteamAppIds {
    $ids = New-Object System.Collections.ArrayList
    $id = Get-SteamRunningAppId
    if ($id -ne 0) { [void]$ids.Add("$id") }
    foreach ($key in @(Get-ChildItem 'HKCU:\Software\Valve\Steam\Apps' -ErrorAction SilentlyContinue)) {
        if ($key.PSChildName -notmatch '^\d+$' -or $ids.Contains($key.PSChildName)) { continue }
        if ("$($key.GetValue('Running'))" -eq '1') { [void]$ids.Add($key.PSChildName) }
    }
    return @($ids.ToArray())
}

# Why the game $AppId counts as running for a job that copies or deletes its install folder $GameDir,
# or '': Steam runs it (Get-RunningSteamAppIds), or a program runs from that folder, also one Steam did
# not start (e.g. a tool started with Windows): its files are in use all the same
function Get-SteamGameRunningReason([string]$AppId, [string]$GameDir) {
    if (@(Get-RunningSteamAppIds) -contains [string]$AppId) { return "Steam runs the game (appid=$AppId)" }
    $p = @(Get-SteamFolderProcesses @($GameDir)) | Select-Object -First 1
    if ($p) { return "$($p.Name) (PID $($p.Id)) runs from $GameDir" }
    return ''
}

# --- Steam client --------------------------------------------------------------------------------
# Never closed by force: -shutdown asks the client to exit. A job that closed it must start it again,
# also when it exits only after the wait (Start-SteamClient -WaitExitSec), so it never stays closed.

function Get-SteamClientExe {
    $reg = Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue
    if ($reg -and $reg.SteamExe) { return ($reg.SteamExe -replace '/', '\') }
    $root = if ($reg -and $reg.SteamPath) { $reg.SteamPath -replace '/', '\' } else { 'C:\Program Files (x86)\Steam' }
    return [System.IO.Path]::Combine($root, 'steam.exe')
}

function Test-SteamClientRunning {
    return [bool](Get-Process -Name steam -ErrorAction SilentlyContinue)
}

# The app ID of the game Steam runs right now, or 0
function Get-SteamRunningAppId {
    $reg = Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue
    $id = [int64]0
    if ($reg -and [int64]::TryParse("$($reg.RunningAppID)", [ref]$id)) { return $id }
    return [int64]0
}

# Waits up to $TimeoutSec for the client to exit; $true when it does not run (any more)
function Wait-SteamClientExit([int]$TimeoutSec) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ((Test-SteamClientRunning) -and $sw.Elapsed.TotalSeconds -lt $TimeoutSec) { Start-Sleep -Seconds 1 }
    return -not (Test-SteamClientRunning)
}

# Asks the client to exit and waits up to $TimeoutSec; $true when it does not run (any more)
function Stop-SteamClient([int]$TimeoutSec = 90) {
    if (-not (Test-SteamClientRunning)) { return $true }
    Start-Process -FilePath (Get-SteamClientExe) -ArgumentList '-shutdown'
    return (Wait-SteamClientExit $TimeoutSec)
}

# --- Input caused by own Steam calls ------------------------------------------------------------
# A steam:// command or a client start brings Steam's window up, and Windows counts that as input
# (GetLastInputInfo jumps to that second although nobody is at the PC; seen with steam://validate on
# a locked PC). The jobs note the time of such a call here, and the agent does not report an input
# that comes up right after it (Get-Telemetry), so HA does not take an automatically woken PC for one
# used by hand. Plain text, one UTC tick count per line, the last few calls.
$SteamEngineOwnCallFile = Join-Path $env:LOCALAPPDATA 'SteamEngine\own_steam_calls.txt'
# How long after an own call an input still counts as caused by it
$SteamEngineOwnCallWindowSec = 30

function Write-SteamEngineOwnCall {
    try {
        $now = [DateTime]::UtcNow
        $keep = @(Get-SteamEngineOwnCalls | Where-Object { ($now - $_).TotalHours -lt 1 } | Select-Object -Last 9)
        $lines = @($keep | ForEach-Object { $_.Ticks.ToString([System.Globalization.CultureInfo]::InvariantCulture) }) + $now.Ticks.ToString([System.Globalization.CultureInfo]::InvariantCulture)
        [System.IO.File]::WriteAllText($SteamEngineOwnCallFile, ($lines -join "`r`n") + "`r`n")
    } catch { }
}

# The UTC times of the noted calls (oldest first), empty when there are none
function Get-SteamEngineOwnCalls {
    $text = Read-SharedTextFile $SteamEngineOwnCallFile
    if (-not $text) { return }
    foreach ($line in ($text -split "`r?`n")) {
        $t = [int64]0
        if ([int64]::TryParse($line.Trim(), [ref]$t) -and $t -gt 0) { New-Object DateTime $t, ([DateTimeKind]::Utc) }
    }
}

# $true when an input at $InputUtc came up right after an own Steam call (from 2 s before the call,
# clocks are read in different processes, to $SteamEngineOwnCallWindowSec after it)
function Test-SteamEngineOwnCallInput([datetime]$InputUtc) {
    foreach ($call in @(Get-SteamEngineOwnCalls)) {
        $d = ($InputUtc - $call).TotalSeconds
        if ($d -ge -2 -and $d -le $SteamEngineOwnCallWindowSec) { return $true }
    }
    return $false
}

# Starts the client in the background (-silent). -WaitExitSec first waits that long for a client that
# is still shutting down. $true when started, $false when a client still runs (nothing started).
# Throws when steam.exe cannot be started.
function Start-SteamClient([int]$WaitExitSec = 0) {
    if ($WaitExitSec -gt 0) { [void](Wait-SteamClientExit $WaitExitSec) }
    if (Test-SteamClientRunning) { return $false }
    Write-SteamEngineOwnCall
    Start-Process -FilePath (Get-SteamClientExe) -ArgumentList '-silent'
    return $true
}

# --- Job lock ------------------------------------------------------------------------------------
# One Steam job at a time: install.ps1, uninstall.ps1 and move.ps1 hold the named mutex Local\SteamEngineJob
# while they work. Windows frees it when the process ends, also when it is killed. install.ps1 also
# writes jobs\install.lock (Write-SteamInstallMarker) and removes it at its end: a lock file while the
# mutex is free means the job was interrupted (report.ps1 then reports 'aborted').

# The mutex when this process owns it now, or $null when another process holds it longer than
# $TimeoutMs. Release it with Exit-SteamEngineJobLock.
function Enter-SteamEngineJobLock([int]$TimeoutMs = 0) {
    $m = New-Object System.Threading.Mutex($false, 'Local\SteamEngineJob')
    $owned = $false
    try {
        $owned = $m.WaitOne($TimeoutMs)
    } catch {
        # Abandoned: the last owner ended without releasing it; it is ours now
        $e = $_.Exception
        if ($e -is [System.Threading.AbandonedMutexException] -or $e.InnerException -is [System.Threading.AbandonedMutexException]) {
            $owned = $true
        } else {
            $m.Dispose()
            throw
        }
    }
    if ($owned) { return $m }
    $m.Dispose()
    return $null
}

function Exit-SteamEngineJobLock($Mutex) {
    if ($null -eq $Mutex) { return }
    try { $Mutex.ReleaseMutex() } catch { }
    try { $Mutex.Dispose() } catch { }
}

# install.lock: the PID of the install job, its app IDs and its job ID from Home Assistant (ASCII, one
# line each; the job ID may be empty)
function Write-SteamInstallMarker([string]$Path, $AppIds, [string]$Job = '') {
    [System.IO.File]::WriteAllText($Path, ("{0}`r`n{1}`r`n{2}`r`n" -f $PID, (@($AppIds) -join ','), $Job), [System.Text.Encoding]::ASCII)
}

# The install job named in install.lock: Pid, AppIds, Job, and Running = that process still runs. A PID
# can be used again after a restart, so the process must be PowerShell and older than the file.
function Read-SteamInstallMarker([string]$Path) {
    $r = [pscustomobject]@{ Pid = 0; AppIds = @(); Job = ''; Running = $false }
    $text = Read-SharedTextFile $Path
    if (-not $text) { return $r }
    $lines = @($text -split "`r?`n")
    if ($lines[0].Trim() -match '^\d+$') { $r.Pid = [int]$lines[0].Trim() }
    if ($lines.Count -gt 1) { $r.AppIds = @($lines[1] -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^\d+$' }) }
    if ($lines.Count -gt 2 -and $lines[2].Trim() -match '^[a-z0-9]{1,16}$') { $r.Job = $lines[2].Trim() }
    if ($r.Pid -gt 0) {
        try {
            $p = Get-Process -Id $r.Pid -ErrorAction SilentlyContinue
            $written = [System.IO.File]::GetLastWriteTime($Path)
            $r.Running = [bool]($p -and $p.ProcessName -eq 'powershell' -and $p.StartTime -le $written)
        } catch { }
    }
    return $r
}

# --- MQTT credentials ----------------------------------------------------------------------------

# Secrets on the PC are encrypted with DPAPI for the current Windows user (the scheduled tasks run as
# the user who ran the setup). Other Windows accounts and copies of the file cannot read them, every
# program of this user can. Base64 in the files.
function Protect-SteamEngineSecret([string]$Text) {
    Add-Type -AssemblyName System.Security
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    return [Convert]::ToBase64String([System.Security.Cryptography.ProtectedData]::Protect($bytes, $null, 'CurrentUser'))
}

function Unprotect-SteamEngineSecret([string]$Base64) {
    Add-Type -AssemblyName System.Security
    $bytes = [System.Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($Base64), $null, 'CurrentUser')
    return [System.Text.Encoding]::UTF8.GetString($bytes)
}

# mqtt_credentials.txt: "username=<user>" and "password_dpapi=<base64>" (see
# Protect-SteamEngineSecret), written by Install-SteamEngine.ps1 (UTF-8). Problem says why the login
# cannot be used, without any secret.
function Get-MqttCredentials([string]$Path) {
    $result = [ordered]@{ Username = ''; Password = ''; Problem = '' }
    $hint = 'enter it again with Install-SteamEngine.ps1 -Update -MqttLogin'
    # The setup replaces the file now and then: a read in that moment finds it missing or locked
    $bytes = $null
    for ($try = 1; $try -le 5 -and $null -eq $bytes; $try++) {
        try { $bytes = [System.IO.File]::ReadAllBytes($Path) } catch { Start-Sleep -Milliseconds 200 }
    }
    if ($null -eq $bytes) {
        $result.Problem = if (Test-Path -LiteralPath $Path) { "file not readable, $hint" } else { "file missing, $hint" }
        return $result
    }
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    $protected = ''
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -match '^\s*username\s*=\s*(.*)$') { $result.Username = $matches[1].Trim() }
        elseif ($line -match '^\s*password_dpapi\s*=\s*(.*)$') { $protected = $matches[1].Trim() }
    }
    if ($protected) {
        try {
            $result.Password = Unprotect-SteamEngineSecret $protected
        } catch {
            # Also after an administrator reset the Windows password (DPAPI keys are lost then)
            $result.Problem = "password not readable for this Windows user (other user or PC, Windows password reset, or damaged), $hint"
        }
    }
    if (-not $result.Problem -and ([string]::IsNullOrWhiteSpace($result.Username) -or [string]::IsNullOrWhiteSpace($result.Password))) {
        $result.Problem = "user or password missing, $hint"
    }
    return $result
}

# Writes mqtt_credentials.txt with the password encrypted and reads it back before it replaces the
# old file, so a failed write never leaves the scripts without a login.
function Save-MqttCredentials([string]$Path, [string]$Username, [string]$Password) {
    if ([string]::IsNullOrWhiteSpace($Username) -or [string]::IsNullOrWhiteSpace($Password)) { throw 'MQTT user and password are needed.' }
    $tmp = $Path + '.new'
    # UTF-8 without BOM, CRLF
    [System.IO.File]::WriteAllLines($tmp, [string[]]@("username=$Username", "password_dpapi=$(Protect-SteamEngineSecret $Password)"))
    $check = Get-MqttCredentials $tmp
    if ($check.Username -cne $Username.Trim() -or $check.Password -cne $Password) {
        Remove-Item -LiteralPath $tmp -Force
        throw 'The MQTT login could not be stored readably.'
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        Move-Item -LiteralPath $tmp -Destination $Path
        return
    }
    # A script reading the old file right now can block the replace for a moment
    for ($try = 1; ; $try++) {
        try {
            # [NullString]: no backup file ($null would arrive as an empty path)
            [System.IO.File]::Replace($tmp, $Path, [NullString]::Value)
            break
        } catch {
            if ($try -ge 5) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue; throw }
            Start-Sleep -Milliseconds 500
        }
    }
}

# --- MQTT 3.1.1 ----------------------------------------------------------------------------------

function Get-MqttStringBytes([string]$Text) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $len = $bytes.Length
    $out = [System.Collections.Generic.List[byte]]::new()
    $out.Add([byte](($len -shr 8) -band 0xFF))
    $out.Add([byte]($len -band 0xFF))
    $out.AddRange($bytes)
    return ,$out.ToArray()
}

function Get-MqttRemainingLengthBytes([int]$Length) {
    $out = [System.Collections.Generic.List[byte]]::new()
    do {
        $encByte = $Length % 128
        $Length = [math]::Floor($Length / 128)
        if ($Length -gt 0) { $encByte = $encByte -bor 0x80 }
        $out.Add([byte]$encByte)
    } while ($Length -gt 0)
    return ,$out.ToArray()
}

function Read-MqttExactBytes([System.IO.Stream]$Stream, [int]$Count) {
    $buffer = New-Object byte[] $Count
    $offset = 0
    while ($offset -lt $Count) {
        $read = $Stream.Read($buffer, $offset, $Count - $offset)
        if ($read -le 0) { throw "Connection closed while reading the MQTT answer." }
        $offset += $read
    }
    return $buffer
}

# Body of a CONNECT packet (clean session; user and password when given). -WillTopic adds a last will
# (QoS 1, retained), as the agent uses it for 'offline'. Holds the password: never log or show it.
function New-MqttConnectBody([string]$ClientId, [string]$Username, [string]$Password, [int]$KeepAliveSec = 30, [string]$WillTopic = '', [string]$WillMessage = '') {
    $hasUser = -not [string]::IsNullOrEmpty($Username)
    $hasPass = -not [string]::IsNullOrEmpty($Password)
    $hasWill = -not [string]::IsNullOrEmpty($WillTopic)
    $body = [System.Collections.Generic.List[byte]]::new()
    $body.AddRange((Get-MqttStringBytes 'MQTT'))
    $body.Add(4)   # Protocol Level 4 = MQTT 3.1.1
    $flags = 0x02   # Clean Session
    if ($hasWill) { $flags = $flags -bor 0x04 -bor 0x08 -bor 0x20 }   # will, will QoS 1, will retain
    if ($hasUser) { $flags = $flags -bor 0x80 }
    if ($hasPass) { $flags = $flags -bor 0x40 }
    $body.Add([byte]$flags)
    $body.Add([byte](($KeepAliveSec -shr 8) -band 0xFF))
    $body.Add([byte]($KeepAliveSec -band 0xFF))
    $body.AddRange((Get-MqttStringBytes $ClientId))
    if ($hasWill) {
        $body.AddRange((Get-MqttStringBytes $WillTopic))
        $body.AddRange((Get-MqttStringBytes $WillMessage))
    }
    if ($hasUser) { $body.AddRange((Get-MqttStringBytes $Username)) }
    if ($hasPass) { $body.AddRange((Get-MqttStringBytes $Password)) }
    return ,$body.ToArray()
}

# One message to the broker: its own connection, retained unless -Retain $false. Throws on errors
# (with the cause, never with credentials).
function Publish-MqttRetained {
    param(
        [Parameter(Mandatory)] [string]$BrokerHost,
        [int]$Port = 1883,
        [Parameter(Mandatory)] [string]$ClientId,
        [string]$Username,
        [string]$Password,
        [Parameter(Mandatory)] [string]$Topic,
        [Parameter(Mandatory)] [AllowEmptyCollection()] [byte[]]$PayloadBytes,
        [int]$Qos = 1,
        [bool]$Retain = $true,
        [int]$TimeoutMs = 10000
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connectTask = $client.ConnectAsync($BrokerHost, $Port)
        $connected = $false
        try {
            $connected = $connectTask.Wait($TimeoutMs)
        } catch {
            # A failed connect arrives wrapped twice; the innermost message names the cause
            throw "Cannot connect to ${BrokerHost}:${Port}: $($_.Exception.GetBaseException().Message)"
        }
        if (-not $connected) {
            throw "Timeout connecting to ${BrokerHost}:${Port}."
        }
        $stream = $client.GetStream()
        $stream.ReadTimeout = $TimeoutMs
        $stream.WriteTimeout = $TimeoutMs

        # --- CONNECT ---
        $connectBody = New-MqttConnectBody $ClientId $Username $Password 30
        $connectPacket = [System.Collections.Generic.List[byte]]::new()
        $connectPacket.Add(0x10)
        $connectPacket.AddRange((Get-MqttRemainingLengthBytes $connectBody.Length))
        $connectPacket.AddRange($connectBody)
        $bytes = $connectPacket.ToArray()
        $stream.Write($bytes, 0, $bytes.Length)

        $connAckHeader = Read-MqttExactBytes $stream 2
        if ($connAckHeader[0] -ne 0x20 -or $connAckHeader[1] -ne 0x02) {
            throw "Unexpected CONNACK from the broker."
        }
        $connAckBody = Read-MqttExactBytes $stream 2
        if ($connAckBody[1] -ne 0x00) {
            throw "MQTT connection refused by the broker, return code $($connAckBody[1])."
        }

        # --- PUBLISH ---
        $packetId = Get-Random -Minimum 1 -Maximum 65535
        $pubVarHeader = [System.Collections.Generic.List[byte]]::new()
        $pubVarHeader.AddRange((Get-MqttStringBytes $Topic))
        if ($Qos -gt 0) {
            $pubVarHeader.Add([byte](($packetId -shr 8) -band 0xFF))
            $pubVarHeader.Add([byte]($packetId -band 0xFF))
        }

        $pubRemaining = $pubVarHeader.Count + $PayloadBytes.Length
        $flags = 0x30
        if ($Retain) { $flags = $flags -bor 0x01 }
        if ($Qos -gt 0) { $flags = $flags -bor ($Qos -shl 1) }

        $pubPacket = [System.Collections.Generic.List[byte]]::new()
        $pubPacket.Add([byte]$flags)
        $pubPacket.AddRange((Get-MqttRemainingLengthBytes $pubRemaining))
        $pubPacket.AddRange($pubVarHeader)
        $pubPacket.AddRange($PayloadBytes)
        $pubBytes = $pubPacket.ToArray()
        $stream.Write($pubBytes, 0, $pubBytes.Length)

        if ($Qos -gt 0) {
            $pubAckHeader = Read-MqttExactBytes $stream 2
            if ($pubAckHeader[0] -ne 0x40 -or $pubAckHeader[1] -ne 0x02) {
                throw "Unexpected PUBACK from the broker."
            }
            $pubAckBody = Read-MqttExactBytes $stream 2
            $ackId = ([int]$pubAckBody[0] -shl 8) -bor [int]$pubAckBody[1]
            if ($ackId -ne $packetId) {
                throw "PUBACK packet ID does not match (expected $packetId, got $ackId)."
            }
        }

        # --- DISCONNECT ---
        $disconnect = [byte[]]@(0xE0, 0x00)
        $stream.Write($disconnect, 0, 2)
    } finally {
        $client.Close()
    }
}

# The end state a job could not send (MQTT down), kept in $Path as the JSON it would have sent
# (move.ps1: jobs\move_final.json): sent now on $Topic, retained, then the file is deleted. $true when
# it was sent, $false without a file. Throws when it cannot be sent; the file stays for a later try.
function Send-SteamPendingEnd {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$BrokerHost,
        [int]$Port = 1883,
        [Parameter(Mandatory)] [string]$ClientId,
        [string]$Username,
        [string]$Password,
        [Parameter(Mandatory)] [string]$Topic
    )
    $text = Read-SharedTextFile $Path
    if ($null -eq $text) { return $false }
    $sent = $false
    if ($text.Trim()) {
        Publish-MqttRetained -BrokerHost $BrokerHost -Port $Port -ClientId $ClientId -Username $Username -Password $Password `
            -Topic $Topic -PayloadBytes ([System.Text.Encoding]::UTF8.GetBytes($text.Trim())) -Qos 1 -Retain $true
        $sent = $true
    }
    [System.IO.File]::Delete($Path)
    return $sent
}

# Reads one packet (header byte and body) or returns $null when none starts before $DeadlineMs on
# $Clock. A packet that starts but does not finish in time throws (read timeout of the stream).
function Read-MqttPacketWithDeadline([System.Net.Sockets.NetworkStream]$Stream, [System.Diagnostics.Stopwatch]$Clock, [int]$DeadlineMs) {
    while (-not $Stream.DataAvailable) {
        if ($Clock.ElapsedMilliseconds -ge $DeadlineMs) { return $null }
        Start-Sleep -Milliseconds 20
    }
    $Stream.ReadTimeout = [int][math]::Max(100, $DeadlineMs - $Clock.ElapsedMilliseconds)
    $header = (Read-MqttExactBytes $Stream 1)[0]
    $length = 0
    $factor = 1
    for ($i = 0; $i -lt 4; $i++) {
        $b = (Read-MqttExactBytes $Stream 1)[0]
        $length += ($b -band 0x7F) * $factor
        $factor *= 128
        if (($b -band 0x80) -eq 0) { break }
    }
    $body = if ($length -gt 0) { Read-MqttExactBytes $Stream $length } else { [byte[]]@() }
    return [pscustomobject]@{ Header = [int]$header; Body = [byte[]]$body }
}

# The retained message of one topic: its own connection, SUBSCRIBE (QoS 0) and the payload of the
# first message on the topic. $null when the broker has none (or an empty one): nothing arrives within
# -WaitAfterSubAckMs after the SUBACK, or before -TimeoutMs in all. Throws on errors (with the cause,
# never with credentials).
function Get-MqttRetainedMessage {
    param(
        [Parameter(Mandatory)] [string]$BrokerHost,
        [int]$Port = 1883,
        [Parameter(Mandatory)] [string]$ClientId,
        [string]$Username,
        [string]$Password,
        [Parameter(Mandatory)] [string]$Topic,
        [int]$TimeoutMs = 3000,
        [int]$WaitAfterSubAckMs = 1000
    )

    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connected = $false
        try {
            $connected = $client.ConnectAsync($BrokerHost, $Port).Wait($TimeoutMs)
        } catch {
            throw "Cannot connect to ${BrokerHost}:${Port}: $($_.Exception.GetBaseException().Message)"
        }
        if (-not $connected) { throw "Timeout connecting to ${BrokerHost}:${Port}." }
        $stream = $client.GetStream()
        $stream.WriteTimeout = $TimeoutMs

        # --- CONNECT ---
        $connectBody = New-MqttConnectBody $ClientId $Username $Password 30
        $packet = [System.Collections.Generic.List[byte]]::new()
        $packet.Add(0x10)
        $packet.AddRange((Get-MqttRemainingLengthBytes $connectBody.Length))
        $packet.AddRange($connectBody)
        $bytes = $packet.ToArray()
        $stream.Write($bytes, 0, $bytes.Length)
        $connAck = Read-MqttPacketWithDeadline $stream $clock $TimeoutMs
        if ($null -eq $connAck) { throw "Timeout waiting for the CONNACK." }
        if ($connAck.Header -ne 0x20 -or $connAck.Body.Length -ne 2) { throw "Unexpected CONNACK from the broker." }
        if ($connAck.Body[1] -ne 0x00) { throw "MQTT connection refused by the broker, return code $($connAck.Body[1])." }

        # --- SUBSCRIBE ---
        $packetId = Get-Random -Minimum 1 -Maximum 65535
        $subBody = [System.Collections.Generic.List[byte]]::new()
        $subBody.Add([byte](($packetId -shr 8) -band 0xFF))
        $subBody.Add([byte]($packetId -band 0xFF))
        $subBody.AddRange((Get-MqttStringBytes $Topic))
        $subBody.Add(0)   # QoS 0: no PUBACK needed
        $packet = [System.Collections.Generic.List[byte]]::new()
        $packet.Add(0x82)
        $packet.AddRange((Get-MqttRemainingLengthBytes $subBody.Count))
        $packet.AddRange($subBody)
        $bytes = $packet.ToArray()
        $stream.Write($bytes, 0, $bytes.Length)

        # --- SUBACK, then the retained PUBLISH (the broker sends it right after the SUBACK) ---
        $deadline = $TimeoutMs
        $payload = $null
        while ($true) {
            $p = Read-MqttPacketWithDeadline $stream $clock $deadline
            if ($null -eq $p) { break }
            $type = $p.Header -band 0xF0
            if ($type -eq 0x90) {
                if ($p.Body.Length -ge 3 -and $p.Body[2] -eq 0x80) { throw "The broker refused the subscription." }
                $deadline = [int][math]::Min($TimeoutMs, $clock.ElapsedMilliseconds + $WaitAfterSubAckMs)
            } elseif ($type -eq 0x30 -and $p.Body.Length -ge 2) {
                $topicLength = ([int]$p.Body[0] -shl 8) -bor [int]$p.Body[1]
                $start = 2 + $topicLength
                if ((($p.Header -shr 1) -band 0x03) -gt 0) { $start += 2 }   # packet ID
                if ($start -gt $p.Body.Length) { throw "Malformed PUBLISH from the broker." }
                $name = [System.Text.Encoding]::UTF8.GetString($p.Body, 2, $topicLength)
                if ($name -ceq $Topic) {
                    if ($start -lt $p.Body.Length) { $payload = [byte[]]$p.Body[$start..($p.Body.Length - 1)] }
                    break
                }
            }
        }

        # --- DISCONNECT ---
        $disconnect = [byte[]]@(0xE0, 0x00)
        try { $stream.Write($disconnect, 0, 2) } catch { }
        return ,$payload
    } finally {
        $client.Close()
    }
}

# --- Names of a PC: MQTT topics and discovery ------------------------------------------------------
# Defined once for the agent, which announces the entities, the jobs and report.ps1, which send on the
# topics, and Uninstall-SteamEngine.ps1, which clears them (see PROTOCOL.md).
#
# A PC without a name (the only PC, the default): topics steam/..., device "Steam PC" (identifier
# steam_pc), unique IDs steam_pc_<key> and entity IDs <domain>.steam_pc_<key>. A named PC (one more PC on the same Home Assistant) gets names of its own:
# topics steam/pc/<id>/..., device <name> (steam_pc_<id>), unique IDs steam_pc_<id>_<key> and entity
# IDs <domain>.steam_pc_<id>_<key>. Its ID comes from the name: a-z and 0-9, at most 16 characters
# (Kinder-PC -> kinderpc).

# The ID of a PC name; '' for no name. Throws when a name gives no usable ID (e.g. only symbols).
function ConvertTo-SteamEnginePcId([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name)) { return '' }
    $id = $Name.Trim().ToLowerInvariant()
    # Umlauts as two letters (the script itself stays ASCII)
    foreach ($pair in @(@([char]0xE4, 'ae'), @([char]0xF6, 'oe'), @([char]0xFC, 'ue'), @([char]0xDF, 'ss'))) {
        $id = $id.Replace([string]$pair[0], $pair[1])
    }
    $id = $id -replace '[^a-z0-9]', ''
    if ($id.Length -gt 16) { $id = $id.Substring(0, 16) }
    if (-not $id) { throw "The PC name '$Name' has no letters or digits to make an ID from." }
    return $id
}

# All names of a PC (see above). PcName '' = the unnamed PC.
function Get-SteamEngineNames([string]$PcName = '') {
    $id = ConvertTo-SteamEnginePcId $PcName
    $base = if ($id) { "steam/pc/$id" } else { 'steam' }
    return [pscustomobject]@{
        PcId         = $id
        PcName       = if ($id) { $PcName.Trim() } else { '' }
        DeviceId     = if ($id) { "steam_pc_$id" } else { 'steam_pc' }
        DeviceName   = if ($id) { $PcName.Trim() } else { 'Steam PC' }
        EntityPrefix = if ($id) { "steam_pc_${id}_" } else { 'steam_pc_' }
        Topic        = [ordered]@{
            AgentState     = "$base/agent/state"
            AgentStatus    = "$base/agent/status"      # online/offline; the broker sets offline by the last will
            AgentCommand   = "$base/agent/command"
            AgentTelemetry = "$base/agent/telemetry"   # every round: last input, network rate
            AgentInfo      = "$base/agent/info"        # who runs as this PC (computer name), retained
            Job            = "$base/job"               # install.ps1, report.ps1
            Library        = "$base/library"           # report.ps1
            Depots         = "$base/depots"            # report.ps1
            History        = "$base/history"           # report.ps1
            Uninstall      = "$base/uninstall"         # uninstall.ps1, not retained
            Move           = "$base/move"              # move.ps1: progress, the result retained
            LaunchPending  = "$base/launch_pending"    # set by HA, read and cleared by lock-at-logon.ps1
        }
    }
}

# The name of this PC from settings.psd1 ('' when there is none). Throws when the file is there but
# cannot be read, or the name gives no ID: which PC this is would be unknown, and running as the
# unnamed PC could take over the jobs of another computer.
function Get-SteamEnginePcName {
    $f = Join-Path $env:LOCALAPPDATA 'SteamEngine\settings.psd1'
    if (-not (Test-Path -LiteralPath $f)) { return '' }
    try {
        $d = Import-PowerShellDataFile -LiteralPath $f -ErrorAction Stop
    } catch {
        throw "settings.psd1 not readable, so it is unknown which PC this is ($($_.Exception.Message))"
    }
    return "$($d.PcName)".Trim()
}

# The names of this PC, for every script that loads this file. When they cannot be known
# ($SteamEngineNamesProblem says why), $SteamEngineNames and $SteamEngineTopic are $null and the
# scripts stop instead of using another PC's topics.
$SteamEngineNamesProblem = ''
try {
    $SteamEngineNames = Get-SteamEngineNames (Get-SteamEnginePcName)
} catch {
    $SteamEngineNames = $null
    $SteamEngineNamesProblem = $_.Exception.Message
}
$SteamEngineTopic = if ($SteamEngineNames) { $SteamEngineNames.Topic } else { $null }

# The retained state topics (cleared by the uninstall)
function Get-SteamEngineRetainedTopics($Names = $SteamEngineNames) {
    $t = $Names.Topic
    return @($t.AgentState, $t.AgentStatus, $t.AgentInfo, $t.Job, $t.Library, $t.Depots, $t.History, $t.Move)
}

# What the agent says about itself on steam/.../agent/info (retained): the computer it runs on, so
# that a second computer set up with the same PC name can be noticed.
function Get-SteamEngineAgentInfo($Names = $SteamEngineNames) {
    return [ordered]@{ computer = $env:COMPUTERNAME; pc_id = $Names.PcId; pc_name = $Names.PcName }
}

# The unique ID of an entity of the agent: steam_pc_<key>, steam_pc_<id>_<key> for a named PC
function Get-SteamEngineUniqueId($Names, [string]$Key) {
    return $Names.EntityPrefix + $Key
}

# The discovery of the agent's own entities: a list of Topic (homeassistant/<component>/<unique_id>/
# config) and Config. The unique IDs, and with them the topics, must not change: Home Assistant knows each entity by its unique ID, a new one orphans the
# entity together with its history, area and labels. The integration finds the entities by these IDs
# (agent_entities.py). default_entity_id (HA 2025.10 or newer) only names entities new to Home
# Assistant; existing ones keep their entity ID.
function Get-SteamEngineDiscovery($Names = $SteamEngineNames) {
    $t = $Names.Topic
    # HA marks the state sensors unavailable when the agent is silent this long (it sends every 5 min);
    # telemetry comes every round, hence the short time
    $expireSec = 900
    $telemetryExpireSec = 90
    $device = [ordered]@{ identifiers = @($Names.DeviceId); name = $Names.DeviceName; manufacturer = 'Steam Engine' }
    $list = New-Object System.Collections.ArrayList
    # name, unique_id and default_entity_id first, then the rest in its order (the payload of the
    # unnamed PC must not change)
    $add = {
        param([string]$component, [string]$key, $cfg)
        $uid = Get-SteamEngineUniqueId $Names $key
        $full = [ordered]@{ name = $cfg.name; unique_id = $uid; default_entity_id = "$component.$($Names.EntityPrefix)$key" }
        foreach ($k in @($cfg.Keys)) { if ($k -ne 'name') { $full[$k] = $cfg[$k] } }
        [void]$list.Add([pscustomobject]@{ Topic = "homeassistant/$component/$uid/config"; Config = $full })
    }
    # key: the state field, also the end of the entity ID (binary_sensor.steam_pc_<key>)
    $sensors = @(
        @{ key = 'steam_downloading'; name = 'Steam downloading'; icon = 'mdi:download';        attrs = '{{ {"appids": value_json.downloading_appids, "moving": value_json.steam_moving | default(''OFF'')} | tojson }}' },
        @{ key = 'steam_running';     name = 'Steam running';     icon = 'mdi:steam';           attrs = $null },
        @{ key = 'game_running';      name = 'Game running';      icon = 'mdi:gamepad-variant'; attrs = '{{ {"appid": value_json.game_appid} | tojson }}' }
    )
    foreach ($s in $sensors) {
        $cfg = [ordered]@{
            name           = $s.name
            state_topic    = $t.AgentState
            value_template = '{{ value_json.' + $s.key + ' }}'
            payload_on     = 'ON'
            payload_off    = 'OFF'
            device_class   = 'running'
            icon           = $s.icon
            expire_after   = $expireSec
            device         = $device
        }
        if ($s.attrs) {
            $cfg['json_attributes_topic'] = $t.AgentState
            $cfg['json_attributes_template'] = $s.attrs
        }
        & $add 'binary_sensor' $s.key $cfg
    }
    # Free space: the state is the largest value of the drives with a library, the drives to install
    # to are an attribute (default([]): a state sent by an older agent has no drives). It does not
    # expire: while the PC is off it keeps the last known values (agent/state is retained)
    & $add 'sensor' 'library_free' ([ordered]@{
        name                     = 'Steam library free space'
        state_topic              = $t.AgentState
        value_template           = '{{ value_json.free_gb }}'
        unit_of_measurement      = 'GB'
        device_class             = 'data_size'
        state_class              = 'measurement'
        icon                     = 'mdi:harddisk'
        json_attributes_topic    = $t.AgentState
        json_attributes_template = '{{ {"drives": value_json.drives | default([])} | tojson }}'
        device                   = $device
    })
    & $add 'sensor' 'last_input' ([ordered]@{
        name           = 'Last input'
        state_topic    = $t.AgentTelemetry
        value_template = '{{ value_json.last_input }}'
        device_class   = 'timestamp'
        icon           = 'mdi:clock-time-three-outline'
        expire_after   = $telemetryExpireSec
        device         = $device
    })
    # A rate that could not be measured shows as 0: the integration takes a known value of this sensor
    # as a sign of life of the agent (the attribute adapter is then empty). No state_class: it changes
    # every round, and long-term statistics of it would grow forever without use.
    & $add 'sensor' 'network' ([ordered]@{
        name                     = 'Network throughput'
        state_topic              = $t.AgentTelemetry
        value_template           = '{{ value_json.network_bytes_s if value_json.network_bytes_s is not none else 0 }}'
        unit_of_measurement      = 'B/s'
        device_class             = 'data_rate'
        icon                    = 'mdi:lan-connect'
        json_attributes_topic    = $t.AgentTelemetry
        json_attributes_template = '{{ {"adapter": value_json.network_adapter} | tojson }}'
        expire_after             = $telemetryExpireSec
        device                   = $device
    })
    # Library and history from report.ps1 (retained), which only sends the states
    & $add 'sensor' 'library' ([ordered]@{
        name                     = 'Steam library'
        state_topic              = $t.Library
        value_template           = '{{ value_json.count }}'
        unit_of_measurement      = 'games'
        icon                     = 'mdi:steam'
        json_attributes_topic    = $t.Library
        json_attributes_template = '{{ {"count": value_json.count, "games": value_json.games} | tojson }}'
        device                   = $device
    })
    & $add 'sensor' 'history' ([ordered]@{
        name                     = 'Update history'
        state_topic              = $t.History
        value_template           = '{{ value_json.latest }}'
        device_class             = 'timestamp'
        icon                     = 'mdi:history'
        json_attributes_topic    = $t.History
        json_attributes_template = '{{ {"total": value_json.total, "entries": value_json.entries} | tojson }}'
        device                   = $device
    })
    # Job state from install.ps1 and report.ps1
    & $add 'sensor' 'install_status' ([ordered]@{
        name                  = 'Steam job'
        state_topic           = $t.Job
        value_template        = '{{ value_json.state }}'
        json_attributes_topic = $t.Job
        icon                  = 'mdi:tray-arrow-down'
        device                = $device
    })
    # PC buttons, available while the agent is online (key: the command, also the end of the entity
    # ID button.steam_pc_<key>).
    foreach ($k in @(
            @{ key = 'shutdown';    name = 'Shut down PC'; icon = 'mdi:power' },
            @{ key = 'restart';     name = 'Restart PC';   icon = 'mdi:restart' },
            @{ key = 'lock';        name = 'Lock PC';      icon = 'mdi:lock' },
            @{ key = 'close_games'; name = 'Close games';  icon = 'mdi:close-box-multiple' })) {
        & $add 'button' $k.key ([ordered]@{
            name                  = $k.name
            command_topic         = $t.AgentCommand
            payload_press         = $k.key
            availability_topic    = $t.AgentStatus
            payload_available     = 'online'
            payload_not_available = 'offline'
            icon                  = $k.icon
            device                = $device
        })
    }
    # Agent reachable: online on connect, offline by the last will (PC off, agent crashed)
    & $add 'binary_sensor' 'agent_online' ([ordered]@{
        name         = 'Agent online'
        state_topic  = $t.AgentStatus
        payload_on   = 'online'
        payload_off  = 'offline'
        device_class = 'connectivity'
        icon         = 'mdi:lan-connect'
        device       = $device
    })
    return @($list.ToArray())
}

# The discovery topic of a plugin entity: Kind 'command' (button "Run <name>") or 'busy' (binary
# sensor "Busy <name>"). The name in lower case, as the agent announces it.
function Get-SteamEnginePluginTopic([string]$Kind, [string]$Name, $Names = $SteamEngineNames) {
    $n = $Name.ToLowerInvariant()
    if ($Kind -eq 'command') {
        return 'homeassistant/button/' + (Get-SteamEngineUniqueId $Names "run_$n") + '/config'
    }
    return 'homeassistant/binary_sensor/' + (Get-SteamEngineUniqueId $Names "busy_$n") + '/config'
}

# The discovery of the plugin entities (Topic and Config, like Get-SteamEngineDiscovery). The names
# must be checked before (the agent takes only A-Z, a-z, 0-9 and _).
function Get-SteamEnginePluginDiscovery($CommandNames, $BusyNames, $Names = $SteamEngineNames) {
    $t = $Names.Topic
    $device = [ordered]@{ identifiers = @($Names.DeviceId); name = $Names.DeviceName; manufacturer = 'Steam Engine' }
    $list = New-Object System.Collections.ArrayList
    foreach ($name in @($CommandNames)) {
        if (-not "$name") { continue }
        $n = "$name".ToLowerInvariant()
        [void]$list.Add([pscustomobject]@{ Topic = (Get-SteamEnginePluginTopic 'command' $n $Names); Config = [ordered]@{
                name                  = 'Run ' + $n
                unique_id             = Get-SteamEngineUniqueId $Names "run_$n"
                default_entity_id     = 'button.' + $Names.EntityPrefix + 'run_' + $n
                command_topic         = $t.AgentCommand
                payload_press         = 'run:' + $n
                availability_topic    = $t.AgentStatus
                payload_available     = 'online'
                payload_not_available = 'offline'
                icon                  = 'mdi:play-box-outline'
                device                = $device
            } })
    }
    foreach ($name in @($BusyNames)) {
        if (-not "$name") { continue }
        $n = "$name".ToLowerInvariant()
        [void]$list.Add([pscustomobject]@{ Topic = (Get-SteamEnginePluginTopic 'busy' $n $Names); Config = [ordered]@{
                name              = 'Busy ' + $n
                unique_id         = Get-SteamEngineUniqueId $Names "busy_$n"
                default_entity_id = 'binary_sensor.' + $Names.EntityPrefix + 'busy_' + $n
                state_topic       = $t.AgentState
                value_template    = '{{ value_json.busy.' + $n + ' | default("OFF") }}'
                payload_on        = 'ON'
                payload_off       = 'OFF'
                device_class      = 'running'
                icon              = 'mdi:progress-wrench'
                expire_after      = 900   # as the state sensors of Get-SteamEngineDiscovery
                device            = $device
            } })
    }
    return @($list.ToArray())
}
