# Rebuild the XP-style launcher into a single-file exe.
# Run after editing launcher.pyw:
#   powershell -ExecutionPolicy Bypass -File .\build-exe.ps1
# Output: PokerLauncher.exe next to this script (windowed, no console flash).
$ErrorActionPreference = 'Stop'
$pyi = Join-Path $PSScriptRoot '.venv\Scripts\pyinstaller.exe'
if (-not (Test-Path $pyi)) {
    Write-Host 'PyInstaller missing. Run: uv pip install --python .venv\Scripts\python.exe pyinstaller'
    exit 1
}
$build = Join-Path $PSScriptRoot '_build'
& $pyi --noconfirm --onefile --windowed `
    --icon (Join-Path $PSScriptRoot 'launcher.ico') `
    --name PokerLauncher `
    --distpath $PSScriptRoot `
    --workpath $build `
    --specpath $build `
    (Join-Path $PSScriptRoot 'launcher.pyw')
$code = $LASTEXITCODE
if ($code -eq 0) { Write-Host ('Build OK: ' + (Join-Path $PSScriptRoot 'PokerLauncher.exe')) }
exit $code
