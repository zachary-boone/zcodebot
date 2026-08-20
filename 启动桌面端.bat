@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0codebot-desktop"

rem /silent is passed by the hidden VBS launcher (desktop shortcut):
rem no console window, no pause prompts, npm output goes to codebot.log.
set "SILENT=0"
set "LOG_FILE=%~dp0codebot-desktop\codebot.log"
if /i "%~1"=="/silent" set "SILENT=1"

rem Only treat the app as running when the sidecar port is actually LISTENING
rem (the sidecar is spawned by Electron and dies with it). Matching plain
rem ":7800" would also hit TIME_WAIT leftovers and falsely report "running".
netstat -ano | findstr ":7800" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  if "%SILENT%"=="1" exit /b 0
  echo CodeBot is already running. Please use the existing window.
  exit /b 0
)

if not exist "node_modules\electron\dist\electron.exe" (
  if "%SILENT%"=="1" (
    echo [CodeBot] Missing Electron runtime. Run `npm install` in codebot-desktop first. >>"%LOG_FILE%"
    exit /b 1
  )
  echo Missing Electron runtime. Please run these commands first:
  echo   cd /d E:\terminal-codebot\codebot-desktop
  echo   npm.cmd install
  pause
  exit /b 1
)

echo Starting CodeBot desktop...
if "%SILENT%"=="1" (
  call npm.cmd run dev:electron >>"%LOG_FILE%" 2>&1
) else (
  call npm.cmd run dev:electron
  if errorlevel 1 (
    echo.
    echo CodeBot failed to start. Check the log above.
    pause
  )
)
endlocal
