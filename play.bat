@echo off
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"
title Windows PowerShell

REM Legacy CLI entry. Preferred: double-click 启动器.lnk (XP-style GUI launcher).
REM Lines starting with REM are COMMENTS: they do NOT set anything.
REM To go online, create a SEPARATE file named poker-config.bat in this folder.

REM   --- TypeSafe Jev (key from console.typesafe.ai/keys, /v1/systemone) ---
REM   @echo off
REM   set "JEV_API_KEY=apikey_PASTE_KEY_HERE"
REM   set "JEV_BASE_URL=https://api.typesafe.ai/v1"
REM   set "JEV_MODEL=jev-latest"
REM   Only the key is required: apikey_ keys auto-fill the other two.
REM   Never paste a real key into this file; put it in poker-config.bat instead.

REM   --- Any chat-model gateway (DeepSeek official / relay) ---
REM   @echo off
REM   set "JEV_API_KEY=PASTE_KEY_HERE"
REM   set "JEV_BASE_URL=https://PASTE_GATEWAY_HOST/v1"
REM   set "JEV_MODEL=PASTE_MODEL_ID"
REM   Then run:  play.bat --models    lists real model ids on that gateway
REM              play.bat --check     verifies key + endpoint + model

REM Never leave a real key in a file you might share or commit.
REM Args: --bots 5 --llm-bots 1 --cards ascii --no-reasons --provider jev
if exist "%~dp0poker-config.bat" call "%~dp0poker-config.bat"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3 and add it to PATH.
  pause >nul
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0play.ps1" %*
if errorlevel 1 (
  echo.
  echo Launch failed. Press any key to close.
  pause >nul
)
endlocal
