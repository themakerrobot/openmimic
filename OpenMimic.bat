@echo off
rem OpenMimic launcher for Windows - double-click to start OpenMimic in this window.
rem Stop: Ctrl+C (collection/training jobs keep running). Closing the window also stops running jobs.
rem Other commands from cmd: OpenMimic.bat update ^| restart ^| stop ^| status ^| background ^| log ^| autostart on
cd /d "%~dp0"
set "OM_CMD=%*"
if "%OM_CMD%"=="" set "OM_CMD=start"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0openmimic.ps1" %OM_CMD%
if errorlevel 1 pause
