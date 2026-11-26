@echo off
REM ============================================================
REM  Template only - this file contains NO real key.
REM  Copy it to poker-config.bat, then fill in YOUR OWN key.
REM  The launcher reads poker-config.bat automatically.
REM ============================================================
REM TypeSafe Jev (decision model). Only the key is required;
REM endpoint and model are auto-filled as:
REM   https://api.typesafe.ai/v1  +  jev-latest
set "JEV_API_KEY="

REM OpenAI-compatible chat gateway (optional, for another provider):
REM set "JEV_BASE_URL=https://YOUR_HOST/v1"
REM set "JEV_MODEL=your-model-id"
