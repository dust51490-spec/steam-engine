<#
Turns Windows autologon on or off for the Windows user who runs it, the way Sysinternals Autologon
does: the password is checked with a real logon first and then stored as the LSA secret
"DefaultPassword" (encrypted by Windows, never in plain text in the registry).

  On:        powershell -ExecutionPolicy Bypass -File Set-Autologon.ps1 -State on
  Off:       powershell -ExecutionPolicy Bypass -File Set-Autologon.ps1 -State off
  Try only:  powershell -ExecutionPolicy Bypass -File Set-Autologon.ps1 -State on -DryRun
             (checks the password and shows what it would change; changes nothing, needs no admin)
  State:     & .\Set-Autologon.ps1 -Query
             (for scripts: an object with On, User and ThisAccount, whether autologon signs in the
             account that runs it, and ForeignWriter, set when -State on|off would not run from
             this folder (see below); changes nothing, needs no admin)

Install-SteamEngine.ps1 runs it with -Autologon on|off, Uninstall-SteamEngine.ps1 with
-AutologonOff, both with -Query. Writing needs administrator rights: the script finds out which
account runs it, then starts itself again elevated (UAC) for that account and asks for the password
in the elevated window. It does not do that from a folder other Windows accounts can change (e.g.
one directly under C:\ or on another drive), as their code would then run as administrator; it says
to extract the zip again into a folder under the user profile. The password is never passed on, printed or written to a file. Every wrong
password counts toward the account lockout, so it asks three times at most. The result is told from
the registry when the elevated window was closed with its X (if autologon already was as asked, it
cannot tell whether the password was stored or deleted, and says so).
Local accounts and Microsoft accounts work (a Microsoft account signs in with its e-mail address and
account password, not the PIN; a passwordless Microsoft account needs a password first). Domain and
work or school accounts are not supported. For a Microsoft account the script offers to turn off
"only allow Windows Hello sign-in", which hides the password sign-in that autologon needs (only
after the password check; if autologon then cannot be set up, it is turned on again).
Off sets AutoAdminLogon to 0 and deletes the stored password (the Disable button of Sysinternals
Autologon leaves it behind). It leaves "only allow Windows Hello sign-in" as it is.
Exit code: 0 done, 1 failed, 2 cancelled or nothing changed.
ASCII only in this file (Windows PowerShell 5.1 reads .ps1 without BOM as ANSI).
#>
[CmdletBinding(DefaultParameterSetName = 'Set')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Set')][ValidateSet('on', 'off')][string]$State,
    [Parameter(ParameterSetName = 'Set')][switch]$DryRun,
    [Parameter(Mandatory = $true, ParameterSetName = 'Query')][switch]$Query,
    # Set by the script for its elevated run: the account found by the first run
    [Parameter(ParameterSetName = 'Set')][string]$TargetName,
    [Parameter(ParameterSetName = 'Set')][string]$TargetSid,
    [Parameter(ParameterSetName = 'Set')][string]$TargetEmail,
    [Parameter(ParameterSetName = 'Set')][switch]$Elevated
)
$ErrorActionPreference = 'Stop'

$winlogonPath    = 'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$passwordLessPath = 'SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'
$maxTries = 3

function Step($text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "   $text" }
function Warn($text) { Write-Host "   $text" -ForegroundColor Yellow }

function Read-YesNo($question, [bool]$default) {
    $hint = if ($default) { 'Y/n' } else { 'y/N' }
    $a = Read-Host ("   $question [$hint]")
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return ($a.Trim() -match '^(y|yes|j|ja)$')
}

# The elevated window closes when the script ends; keep it open so the result can be read
function Exit-Script([int]$code) {
    if ($Elevated) { [void](Read-Host '   Press Enter to close this window') }
    exit $code
}

if (-not ('SteamEngineSetup.Lsa' -as [type])) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security;
using System.Security.Principal;

namespace SteamEngineSetup
{
    // The LSA secret "DefaultPassword" that Winlogon uses for autologon (same calls as Sysinternals Autologon)
    public static class Lsa
    {
        const uint POLICY_CREATE_SECRET = 0x00000020;
        const uint STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034;
        const string KeyName = "DefaultPassword";

        [StructLayout(LayoutKind.Sequential)]
        struct LSA_UNICODE_STRING { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }

        [StructLayout(LayoutKind.Sequential)]
        struct LSA_OBJECT_ATTRIBUTES
        {
            public uint Length; public IntPtr RootDirectory; public IntPtr ObjectName; public uint Attributes;
            public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService;
        }

        [DllImport("advapi32.dll")]
        static extern uint LsaOpenPolicy(IntPtr systemName, ref LSA_OBJECT_ATTRIBUTES attributes, uint access, out IntPtr policy);
        [DllImport("advapi32.dll")]
        static extern uint LsaStorePrivateData(IntPtr policy, ref LSA_UNICODE_STRING keyName, ref LSA_UNICODE_STRING data);
        // The same call with data NULL deletes the secret
        [DllImport("advapi32.dll", EntryPoint = "LsaStorePrivateData")]
        static extern uint LsaDeletePrivateData(IntPtr policy, ref LSA_UNICODE_STRING keyName, IntPtr data);
        [DllImport("advapi32.dll")]
        static extern int LsaNtStatusToWinError(uint status);
        [DllImport("advapi32.dll")]
        static extern uint LsaClose(IntPtr policy);

        static Exception Fail(string what, uint status)
        {
            int err = LsaNtStatusToWinError(status);
            return new Win32Exception(err, what + " failed: " + new Win32Exception(err).Message + " (0x" + status.ToString("X8") + ")");
        }

        // Length in bytes without the closing null, as LSA wants it
        static LSA_UNICODE_STRING Make(IntPtr buffer, int chars)
        {
            if (chars > 32766) throw new ArgumentOutOfRangeException("chars");
            LSA_UNICODE_STRING s = new LSA_UNICODE_STRING();
            s.Buffer = buffer; s.Length = (ushort)(chars * 2); s.MaximumLength = (ushort)((chars + 1) * 2);
            return s;
        }

        static IntPtr Open()
        {
            LSA_OBJECT_ATTRIBUTES a = new LSA_OBJECT_ATTRIBUTES();
            a.Length = (uint)Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
            IntPtr policy;
            uint st = LsaOpenPolicy(IntPtr.Zero, ref a, POLICY_CREATE_SECRET, out policy);
            if (st != 0) throw Fail("LsaOpenPolicy", st);
            return policy;
        }

        public static void Store(SecureString password)
        {
            if (password == null || password.Length == 0) throw new ArgumentException("The password is empty.");
            IntPtr key = IntPtr.Zero, pw = IntPtr.Zero, policy = IntPtr.Zero;
            try
            {
                key = Marshal.StringToHGlobalUni(KeyName);
                pw = Marshal.SecureStringToGlobalAllocUnicode(password);
                LSA_UNICODE_STRING k = Make(key, KeyName.Length);
                LSA_UNICODE_STRING d = Make(pw, password.Length);
                policy = Open();
                uint st = LsaStorePrivateData(policy, ref k, ref d);
                if (st != 0) throw Fail("LsaStorePrivateData", st);
            }
            finally
            {
                if (policy != IntPtr.Zero) LsaClose(policy);
                if (pw != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(pw);
                if (key != IntPtr.Zero) Marshal.FreeHGlobal(key);
            }
        }

        // true: a stored password was deleted, false: there was none
        public static bool Delete()
        {
            IntPtr key = IntPtr.Zero, policy = IntPtr.Zero;
            try
            {
                key = Marshal.StringToHGlobalUni(KeyName);
                LSA_UNICODE_STRING k = Make(key, KeyName.Length);
                policy = Open();
                uint st = LsaDeletePrivateData(policy, ref k, IntPtr.Zero);
                if (st == STATUS_OBJECT_NAME_NOT_FOUND) return false;
                if (st != 0) throw Fail("LsaStorePrivateData", st);
                return true;
            }
            finally
            {
                if (policy != IntPtr.Zero) LsaClose(policy);
                if (key != IntPtr.Zero) Marshal.FreeHGlobal(key);
            }
        }
    }

    public static class Account
    {
        [StructLayout(LayoutKind.Sequential)]
        struct USER_INFO_24
        {
            public int internet_identity; public uint flags; public IntPtr provider_name;
            public IntPtr principal_name; public IntPtr user_sid;
        }

        [DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
        static extern int NetUserGetInfo(string server, string user, int level, out IntPtr buffer);
        [DllImport("netapi32.dll")]
        static extern int NetApiBufferFree(IntPtr buffer);

        // Local account: { SID, "" }; local account connected to a Microsoft account: { SID, e-mail };
        // null if there is no such local account
        public static string[] Get(string localName)
        {
            IntPtr buf;
            if (NetUserGetInfo(null, localName, 24, out buf) != 0 || buf == IntPtr.Zero) return null;
            try
            {
                USER_INFO_24 u = (USER_INFO_24)Marshal.PtrToStructure(buf, typeof(USER_INFO_24));
                string sid = u.user_sid == IntPtr.Zero ? "" : new SecurityIdentifier(u.user_sid).Value;
                string provider = u.provider_name == IntPtr.Zero ? "" : Marshal.PtrToStringUni(u.provider_name);
                string mail = u.principal_name == IntPtr.Zero ? "" : Marshal.PtrToStringUni(u.principal_name);
                if (mail.StartsWith("MicrosoftAccount\\", StringComparison.OrdinalIgnoreCase)) mail = mail.Substring(17);
                bool msa = u.internet_identity != 0 && provider == "MicrosoftAccount" && mail.Contains("@");
                return new string[] { sid, msa ? mail.Trim() : "" };
            }
            finally { NetApiBufferFree(buf); }
        }

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool LogonUserW(string user, string domain, IntPtr password, int type, int provider, out IntPtr token);
        [DllImport("kernel32.dll")]
        static extern bool CloseHandle(IntPtr handle);

        // An interactive logon as Winlogon does it: 0 if the password is right and the logon is the
        // account with expectedSid, -1 if it is another account, else the Win32 error
        // (1326 wrong password, 1909 locked out, ...). No profile is loaded.
        public static int TestLogon(string user, string domain, SecureString password, string expectedSid)
        {
            IntPtr pw = IntPtr.Zero, token = IntPtr.Zero;
            try
            {
                pw = Marshal.SecureStringToGlobalAllocUnicode(password);
                if (!LogonUserW(user, domain, pw, 2, 0, out token)) return Marshal.GetLastWin32Error();
                using (WindowsIdentity id = new WindowsIdentity(token))
                {
                    return id.User != null && String.Equals(id.User.Value, expectedSid, StringComparison.OrdinalIgnoreCase) ? 0 : -1;
                }
            }
            finally
            {
                if (token != IntPtr.Zero) CloseHandle(token);
                if (pw != IntPtr.Zero) Marshal.ZeroFreeGlobalAllocUnicode(pw);
            }
        }
    }
}
'@
}

# Winlogon reads the 64-bit registry; a 32-bit PowerShell would otherwise write to WOW6432Node
function Open-Hklm64($path, [bool]$writable) {
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64)
    if ($writable) { return $base.CreateSubKey($path) }
    return $base.OpenSubKey($path)
}

function Get-RegValue($path, $name) {
    $k = Open-Hklm64 $path $false
    if (-not $k) { return $null }
    try { return $k.GetValue($name) } finally { $k.Close() }
}

# Winlogon's autologon and whether it signs in this account ($TargetName, $TargetEmail): by its name
# (domain none, "." or this computer) or, for a Microsoft account, by its e-mail address (domain
# "MicrosoftAccount" as this script writes it; Sysinternals Autologon may write either form)
function Get-AutologonState {
    $user = "$(Get-RegValue $winlogonPath 'DefaultUserName')"
    $domain = "$(Get-RegValue $winlogonPath 'DefaultDomainName')"
    if ($user -match '^([^\\]*)\\(.+)$') { $domain = $matches[1]; $user = $matches[2] }
    $on = "$(Get-RegValue $winlogonPath 'AutoAdminLogon')" -eq '1'
    $mine = ($user -eq $TargetName -and $domain -in '', '.', $env:USERDOMAIN) -or
        ($TargetEmail -and $user -eq $TargetEmail -and $domain -in '', '.', $env:USERDOMAIN, 'MicrosoftAccount')
    return [pscustomobject]@{ On = $on; User = $user; ThisAccount = [bool]($on -and $mine) }
}

function Show-Current {
    $s = Get-AutologonState
    $for = if (-not $s.On -or -not $s.User) { '' } elseif ($s.ThisAccount) { " for $($s.User)" } else { " for $($s.User), not for this account" }
    Info ("Now: autologon {0}{1}." -f $(if ($s.On) { 'on' } else { 'off' }), $for)
}

# The first account other than this one, SYSTEM, Administrators or TrustedInstaller that could change
# this script or put another one in its place: write or delete the file, delete or rename a folder
# above it (or what is in it), or change the permissions or owner of any of them. "<path>: <account>",
# else $null. Deny entries are not weighed (at worst it refuses a folder that is safe).
function Get-ForeignWriter([string]$file) {
    $trusted = @($TargetSid, 'S-1-5-18', 'S-1-5-32-544', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464',
        'S-1-3-0', 'S-1-3-4')
    # The file: write and append data, delete, change permissions or owner, generic write or all
    $mask = 0x500D0006
    for ($path = $file; $path; $path = [System.IO.Path]::GetDirectoryName($path)) {
        try {
            $acl = Get-Acl -LiteralPath $path
            $sids = @($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value)
            foreach ($r in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
                if ($r.AccessControlType -eq 'Allow' -and -not ($r.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and
                    ([int]$r.FileSystemRights -band $mask)) { $sids += $r.IdentityReference.Value }
            }
        } catch {
            return "${path}: who can change it is unknown ($($_.Exception.Message))"
        }
        foreach ($sid in $sids) {
            if ($trusted -contains $sid) { continue }
            $name = $sid
            try { $name = ([Security.Principal.SecurityIdentifier]$sid).Translate([Security.Principal.NTAccount]).Value } catch { }
            return "${path}: $name"
        }
        # The folders above: delete what is in them or themselves, change permissions or owner, generic all
        $mask = 0x100D0040
    }
    return $null
}

# The user name and domain Winlogon signs in with, the same pair the password check uses
function Get-LogonPair {
    if ($TargetEmail) { return @($TargetEmail, 'MicrosoftAccount') }
    return @($TargetName, $env:COMPUTERNAME)
}

# Asks for the password and checks it with a logon; returns the SecureString or $null
function Read-CheckedPassword {
    $pair = Get-LogonPair
    $what = if ($TargetEmail) { "Microsoft account password of $TargetEmail (not the PIN)" } else { "Windows password of $TargetName" }
    for ($try = 1; $try -le $maxTries; $try++) {
        $pw = Read-Host ("   $what") -AsSecureString
        if ($pw.Length -eq 0) {
            Warn 'Autologon needs the password of the account; an account without a password cannot use it.'
            return $null
        }
        # Local account: domain "." checks the local account database only
        $rc = [SteamEngineSetup.Account]::TestLogon($pair[0], $(if ($TargetEmail) { $pair[1] } else { '.' }), $pw, $TargetSid)
        if ($rc -eq 0) { return $pw }
        $pw.Dispose()
        switch ($rc) {
            1326 {
                Warn 'Wrong password.'
                if ($TargetEmail) { Warn 'It is the password of the Microsoft account, not the PIN. A passwordless Microsoft account needs a password first (account.microsoft.com > Security).' }
            }
            1909 { Warn 'The account is locked after too many wrong passwords. Wait a while (usually 10 minutes) and try again.'; return $null }
            1311 { Warn 'Windows could not check the Microsoft account. Connect the PC to the internet and try again.'; return $null }
            1385 { Warn 'This account may not sign in at this PC (logon right), so autologon would not work either.'; return $null }
            -1   { Warn 'This password signs in another account.'; return $null }
            default { Warn ("The password could not be checked: {0} ({1})." -f ([ComponentModel.Win32Exception]$rc).Message.TrimEnd('.'), $rc); return $null }
        }
    }
    Warn "$maxTries wrong passwords, stopped (every wrong one counts toward the account lockout)."
    return $null
}

# --- First run (not elevated): who signs in ------------------------------------------------
if (-not $Elevated) {
    $TargetName = $env:USERNAME
    $TargetSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    # A local account (a Microsoft account is one too, with its e-mail address)
    $acc = $null
    if ($env:USERDOMAIN -eq $env:COMPUTERNAME) { $acc = [SteamEngineSetup.Account]::Get($TargetName) }
    if ($acc -and $acc[0] -eq $TargetSid) { $TargetEmail = $acc[1] } else { $acc = $null }
    if ($Query) {
        # The folder check below, read only, so a script can tell beforehand that -State would refuse
        return (Get-AutologonState | Add-Member -NotePropertyName ForeignWriter -NotePropertyValue (Get-ForeignWriter $PSCommandPath) -PassThru)
    }
    Show-Current
    if ($env:USERDOMAIN -ne $env:COMPUTERNAME) {
        Warn "Autologon setup supports local and Microsoft accounts only, not $env:USERDOMAIN\$env:USERNAME."
        exit 1
    }
    if (-not $acc) {
        Warn "The local account $TargetName could not be found."
        exit 1
    }
    if ($State -eq 'on') {
        if ($TargetEmail) { Info "Account: $TargetName, connected to the Microsoft account $TargetEmail." } else { Info "Account: $TargetName (local account)." }
    }
    # Its elevated run must not come from a folder other accounts can change: their code would run
    # as administrator (and see the password)
    $writer = Get-ForeignWriter $PSCommandPath
    if ($writer) {
        Warn "Other Windows accounts can change this script or a folder above it ($writer), so it does not run as administrator from there."
        Warn ("Extract the zip again into a folder under {0}, e.g. {1}, and run it again from there; a clone of the repository goes under {0} too." -f $env:USERPROFILE, (Join-Path $env:USERPROFILE 'SteamEngine'))
        if (-not $DryRun) { exit 1 }
    }

    if ($DryRun) {
        if ($State -eq 'on') {
            $pw = Read-CheckedPassword
            if (-not $pw) { exit 1 }
            $pw.Dispose()
            $pair = Get-LogonPair
            Info 'The password is right. Without -DryRun the script would (as administrator):'
            Info "- store the password as the LSA secret DefaultPassword (encrypted)"
            Info "- set Winlogon DefaultUserName = $($pair[0]), DefaultDomainName = $($pair[1]), AutoAdminLogon = 1"
            Info '- delete Winlogon DefaultPassword (a password in plain text) and AutoLogonCount, if there'
            if ($TargetEmail -and (Get-RegValue $passwordLessPath 'DevicePasswordLessBuildVersion') -eq 2) {
                Info '- ask to turn off "only allow Windows Hello sign-in" (it is on)'
            }
        } else {
            Info 'Without -DryRun the script would (as administrator) set Winlogon AutoAdminLogon = 0, delete'
            Info 'DefaultPassword, AutoLogonCount and ForceAutoLogon there, and delete the LSA secret DefaultPassword.'
        }
        Info 'Nothing changed (-DryRun).'
        exit 2
    }

    # Start again as administrator; the password is asked for in that window
    $argList = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -State {1} -Elevated -TargetName "{2}" -TargetSid {3}' -f $PSCommandPath, $State, $TargetName, $TargetSid
    if ($TargetEmail) { $argList += ' -TargetEmail "{0}"' -f $TargetEmail }
    if ($State -eq 'on') { Info 'A window with administrator rights opens (confirm the Windows prompt); enter the password there.' }
    $before = Get-AutologonState
    try {
        $p = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $argList -Verb RunAs -Wait -PassThru
    } catch {
        Warn 'Cancelled: administrator rights were not given. Nothing changed.'
        exit 2
    }
    $code = $p.ExitCode
    if ($code -in 0, 1, 2) {
        if ($code -eq 0) { Info "Autologon is $State now." }
        elseif ($code -eq 2) { Warn 'Nothing changed.' }
        else { Warn 'Autologon was not changed (see the messages in the administrator window).' }
    } else {
        # The window closed with its X gives no exit code of the script: the registry tells whether it
        # got done, unless it already was as asked (e.g. on again after a new Windows password)
        $now = Get-AutologonState
        $was = if ($State -eq 'on') { $before.ThisAccount } else { -not $before.On }
        $is = if ($State -eq 'on') { $now.ThisAccount } else { -not $now.On }
        $code = 0
        if (-not $is) { $code = 1; Warn 'The administrator window was closed before it ended.' }
        elseif (-not $was) { Info "Autologon is $State now." }
        else {
            $what = if ($State -eq 'on') { 'the new password was stored' } else { 'the stored password was deleted' }
            Warn "The administrator window was closed with its X; whether $what cannot be told here, run it again if unsure."
        }
    }
    # Always the state as it is
    Show-Current
    exit $code
}

# --- Elevated run -----------------------------------------------------------------------------
Step ("Autologon " + $State)
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Warn 'This needs administrator rights.'
    Exit-Script 1
}
$helloTurnedOff = $false
try {
    if ($State -eq 'off') {
        $k = Open-Hklm64 $winlogonPath $true
        try {
            $k.SetValue('AutoAdminLogon', '0', [Microsoft.Win32.RegistryValueKind]::String)
            foreach ($n in 'DefaultPassword', 'AutoLogonCount', 'ForceAutoLogon') { $k.DeleteValue($n, $false) }
        } finally { $k.Close() }
        $deleted = [SteamEngineSetup.Lsa]::Delete()
        Info ('Autologon is off{0}.' -f $(if ($deleted) { ', the stored password is deleted' } else { '' }))
        Exit-Script 0
    }

    # The account must still be the one the first run found (the elevated window may be another admin's)
    $acc = [SteamEngineSetup.Account]::Get($TargetName)
    if (-not $acc -or $acc[0] -ne $TargetSid -or $acc[1] -ne $TargetEmail) {
        Warn "The account $TargetName has changed since the start. Run the setup again."
        Exit-Script 1
    }
    Info $(if ($TargetEmail) { "Account: $TargetName (Microsoft account $TargetEmail)" } else { "Account: $TargetName" })

    # The password first (the check works with "only Windows Hello" on, as in -DryRun): a wrong one
    # must not leave that setting changed
    $pw = Read-CheckedPassword
    if (-not $pw) {
        Warn 'Autologon not changed.'
        Exit-Script 1
    }
    # "Only allow Windows Hello sign-in for Microsoft accounts" hides the password sign-in
    if ($TargetEmail -and (Get-RegValue $passwordLessPath 'DevicePasswordLessBuildVersion') -eq 2) {
        Warn '"Only allow Windows Hello sign-in for Microsoft accounts" is on (Settings > Accounts > Sign-in options).'
        Warn 'It hides the password sign-in, so autologon cannot sign in.'
        if (-not (Read-YesNo 'Turn it off?' $true)) {
            $pw.Dispose()
            Warn 'Autologon not set up.'
            Exit-Script 2
        }
        $k = Open-Hklm64 $passwordLessPath $true
        try { $k.SetValue('DevicePasswordLessBuildVersion', 0, [Microsoft.Win32.RegistryValueKind]::DWord) } finally { $k.Close() }
        $helloTurnedOff = $true
        Info 'Turned off; Windows Hello (PIN, face, fingerprint) keeps working.'
    }
    try {
        # The secret first: if Windows refuses it, the registry stays as it was (never plain text)
        [SteamEngineSetup.Lsa]::Store($pw)
    } finally { $pw.Dispose() }
    $pair = Get-LogonPair
    $k = Open-Hklm64 $winlogonPath $true
    try {
        $k.SetValue('DefaultUserName', $pair[0], [Microsoft.Win32.RegistryValueKind]::String)
        $k.SetValue('DefaultDomainName', $pair[1], [Microsoft.Win32.RegistryValueKind]::String)
        # A password in plain text would be a leak; a countdown would turn autologon off again
        foreach ($n in 'DefaultPassword', 'AutoLogonCount') { $k.DeleteValue($n, $false) }
        $k.SetValue('AutoAdminLogon', '1', [Microsoft.Win32.RegistryValueKind]::String)
    } finally { $k.Close() }
    $helloTurnedOff = $false
    Info "Autologon is on for $($pair[0]); it signs in at the next start of Windows."
    Info 'If you change the Windows password, run this again (Install.cmd -Update -Autologon on).'

    # Things that stop autologon although it is set up (Microsoft KB 324737)
    $policy = 'SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
    if ((Get-RegValue $policy 'legalnoticecaption') -or (Get-RegValue $policy 'legalnoticetext')) {
        Warn 'A logon message (legal notice) is set by a policy; Windows waits for it at every start, so autologon stops there.'
    }
    $eas = Open-Hklm64 'SYSTEM\CurrentControlSet\Control\EAS' $false
    if ($eas) {
        $eas.Close()
        Warn 'Exchange ActiveSync password rules are set on this PC (HKLM\SYSTEM\CurrentControlSet\Control\EAS); they can block autologon.'
    }
    Exit-Script 0
} catch {
    Warn ('Failed: ' + $_.Exception.Message)
    if ($helloTurnedOff) {
        # Autologon was not set up: "only Windows Hello" goes back on, as it was
        try {
            $k = Open-Hklm64 $passwordLessPath $true
            try { $k.SetValue('DevicePasswordLessBuildVersion', 2, [Microsoft.Win32.RegistryValueKind]::DWord) } finally { $k.Close() }
            Warn '"Only allow Windows Hello sign-in" is on again, as before.'
        } catch {
            Warn '"Only allow Windows Hello sign-in" stays off (Settings > Accounts > Sign-in options).'
        }
    }
    Exit-Script 1
}
