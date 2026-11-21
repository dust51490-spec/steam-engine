@echo off
rem Removes the PC side of Steam Engine: first shows what would happen, then asks; while Windows
rem autologon signs in this account, it also asks whether to turn that off (from a folder other
rem Windows accounts can change it cannot, and says how instead). Games, the Steam client and this
rem folder stay. It takes no options.
title Steam Engine uninstall
setlocal
rem Windows PowerShell by its full path: cmd.exe would look in this folder first
set "ps=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%~dp0setup\Uninstall-SteamEngine.ps1" (
    echo setup\Uninstall-SteamEngine.ps1 is not next to this file. Extract the whole zip first, then run Uninstall.cmd in the extracted folder.
    goto end
)
"%ps%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\Uninstall-SteamEngine.ps1"
rem 3: the dry run found autologon for this account, 4: it did, but autologon cannot be turned off
rem from this folder; any other code but 0 stops here, a negative one too (e.g. the script not
rem found), which "if errorlevel 1" would miss
set code=%errorlevel%
if %code% neq 0 if %code% neq 3 if %code% neq 4 goto end
echo.
if %code% equ 4 echo Windows autologon signs in this account and cannot be turned off from this folder, see above.
if %code% equ 4 echo To turn it off too, type no now, extract the zip again into a folder under your user profile and run Uninstall.cmd there.
set answer=
set /p answer=Really remove Steam Engine from this PC? Type yes to confirm:
if /i not "%answer%"=="yes" (
    echo Nothing was removed.
    goto end
)
set off=
if %code% neq 3 goto remove
echo.
echo Without the lock of Steam Engine a PC with autologon starts with its desktop open.
set answer=
set /p answer=Turn Windows autologon off too? Windows asks for administrator rights. Type yes to confirm:
if /i "%answer%"=="yes" set off=-AutologonOff
:remove
"%ps%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\Uninstall-SteamEngine.ps1" -Yes %off%
:end
echo.
pause
