#requires -Version 5.1
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"

if (-not (Test-Path -LiteralPath $Venv)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Host "Syncing the Python 3.11+ environment with uv ..."
        & uv sync --extra dev --project $Root
    }
    else {
        Write-Host "Creating .venv with $Python ..."
        & $Python -m venv $Venv
    }
}

$Py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) {
    throw "Virtual-environment Python was not created at $Py"
}

& $Py -m ensurepip --upgrade
& $Py -m pip install -e "${Root}[dev]"

$EnvExample = Join-Path $Root ".env.example"
$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path -LiteralPath $EnvFile)) {
    Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
    Write-Host "Created .env from .env.example."
}

Write-Host "Bootstrap complete."
Write-Host "No application was launched and no render queue was touched."
Write-Host "Add ELEVENLABS_API_KEY and RABBITHOLE_VOICE_ID to .env."
Write-Host "FFmpeg/FFprobe and a Chromium browser remain external prerequisites."
Write-Host "Run: uv run rabbithole resolve doctor"
