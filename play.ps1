# poker-deepseek launcher for Windows PowerShell
# Usage:  powershell -ExecutionPolicy Bypass -File .\play.ps1
# Or inside PowerShell:  $env:POKER_API_KEY='sk-...'; .\play.ps1
# Args forwarded: --bots 1..5 (default 3), --llm-seats 1,3,5 (which seats use the model),
#                 --llm-bots N (legacy), --check, --models, --no-reasons,
#                 --provider jev|openai, --cards ascii|pretty
# Preferred entry: double-click 启动器.lnk (XP-style PyQt5 GUI). play.bat is kept for CLI use.
# TypeSafe Jev: only $env:JEV_API_KEY='apikey_...' is needed (endpoint/model auto-filled).
# Chat gateway: set JEV_BASE_URL='https://HOST/v1' then run --models to list model ids.
# Card faces: default Chinese (方9 / 红A). Add --cards ascii for 9d/As, --cards pretty for 9♦/A♠.
# In-game press ? for a plain-language reading of your cards and the board.
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
try { $Host.UI.RawUI.WindowTitle = 'Windows PowerShell' } catch { }

if (-not $env:POKER_API_KEY -and -not $env:JEV_API_KEY -and -not $env:DEEPSEEK_API_KEY) {
    Write-Host 'No API key -> offline bot.'
}
$officialJev = ($env:JEV_API_KEY -like 'apikey_*') -or ($env:JEV_API_KEY -like 'tsf_*')
if (($env:JEV_API_KEY -or $env:POKER_API_KEY) -and -not ($env:JEV_BASE_URL -or $env:POKER_BASE_URL) -and -not $officialJev) {
    Write-Host 'WARNING: custom key but no base URL. Set $env:JEV_BASE_URL (https://HOST/v1), then run --models.'
}
if ($officialJev -and -not $env:JEV_BASE_URL) {
    Write-Host 'TypeSafe Jev detected -> https://api.typesafe.ai/v1 + jev-latest (/v1/systemone).'
}
if ($env:JEV_API_KEY -match '[^\x00-\x7F]' -or $env:JEV_BASE_URL -match '[^\x00-\x7F]') {
    Write-Host 'WARNING: key/base URL contains non-ASCII text (placeholder?). Replace with real values.'
}
$cfgBase = if ($env:JEV_BASE_URL) { $env:JEV_BASE_URL } else { $env:POKER_BASE_URL }
if ($cfgBase -and $cfgBase -notmatch '^https?://') {
    Write-Host 'WARNING: API address is not a URL (API key pasted into the address field?). Leave it empty for official Jev.'
}

$pyCmd = 'python'
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    $venvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path $venvPy) { $pyCmd = $venvPy }
}
& $pyCmd (Join-Path $PSScriptRoot 'poker.py') @args
exit $LASTEXITCODE
