@echo off
rem Sets up the PC side of Steam Engine, or updates it with the scripts of this folder.
rem Double-click it. Options of Install-SteamEngine.ps1 can be added on the command line,
rem e.g. Install.cmd -Update -MqttLogin (without -Update it runs the whole setup again)
title Steam Engine setup
if not exist "%~dp0setup\Install-SteamEngine.ps1" (
    echo setup\Install-SteamEngine.ps1 is not next to this file. Extract the whole zip first, then run Install.cmd in the extracted folder.
    goto end
)
rem Windows PowerShell by its full path: cmd.exe would look in this folder first
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\Install-SteamEngine.ps1" %*
rem Not "if errorlevel 1": that misses negative exit codes (e.g. the script not found)
if %errorlevel% neq 0 (
    echo.
    echo The setup stopped with an error, see the red text above. Fix the cause and run it again.
)
:end
echo.
pause
