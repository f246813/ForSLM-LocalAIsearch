@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start.ps1" %*
set "FORSLM_EXIT=%errorlevel%"
if not "%FORSLM_EXIT%"=="0" if "%~1"=="" pause
exit /b %FORSLM_EXIT%
