#requires -Version 5.1
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$VenvScripts = Join-Path $Venv "Scripts"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$Uv = Get-Command uv -ErrorAction SilentlyContinue

if ($null -ne $Uv) {
    Write-Host "Syncing the Python 3.11+ environment with uv ..."
    & $Uv.Source sync --project $Root --extra dev
    if ($LASTEXITCODE -ne 0) {
        throw "uv sync failed with exit code $LASTEXITCODE"
    }

    $RunCommand = "uv run --project `"$Root`" rabbithole"
}
else {
    $PythonExecutable = $Python
    $PythonArguments = @()
    $PythonCommand = Get-Command $PythonExecutable -ErrorAction SilentlyContinue

    # The Windows Store and python.org normally expose `python`; a clean
    # python.org install may expose only the Python launcher.
    if ($null -eq $PythonCommand -and $Python -eq "python") {
        $Launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($null -ne $Launcher) {
            $PythonExecutable = $Launcher.Source
            $PythonArguments = @("-3")
            $PythonCommand = $Launcher
        }
    }
    if ($null -eq $PythonCommand) {
        throw (
            "uv was not found and Python executable '$Python' is unavailable. " +
            "Install uv or Python 3.11+, or pass -Python <path>."
        )
    }

    & $PythonExecutable @PythonArguments -c (
        "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) " +
        "else 'Python 3.11+ is required')"
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.11 or newer is required to create .venv"
    }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Host "Creating .venv with $PythonExecutable ..."
        & $PythonExecutable @PythonArguments -m venv $Venv
        if ($LASTEXITCODE -ne 0) {
            throw "Python virtual-environment creation failed with exit code $LASTEXITCODE"
        }
    }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        throw "Virtual-environment Python was not created at $VenvPython"
    }

    & $VenvPython -c (
        "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) " +
        "else 'Existing .venv uses Python older than 3.11')"
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Recreate .venv with Python 3.11 or newer"
    }

    & $VenvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        throw "ensurepip failed with exit code $LASTEXITCODE"
    }
    & $VenvPython -m pip install -e "${Root}[dev]"
    if ($LASTEXITCODE -ne 0) {
        throw "Editable project installation failed with exit code $LASTEXITCODE"
    }

    # Prepending Scripts makes the environment's yt-dlp console entry point
    # visible to child processes without requiring activation or global uv.
    $RunCommand = (
        "`$env:Path=`"$VenvScripts;`$env:Path`"; " +
        "& `"$VenvPython`" -m rabbithole.cli"
    )
}

$EnvExample = Join-Path $Root ".env.example"
$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path -LiteralPath $EnvFile)) {
    Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
    Write-Host "Created .env from .env.example."
}

Write-Host "Bootstrap complete."
Write-Host "No application was launched and no render queue was touched."
Write-Host "Add ELEVENLABS_API_KEY and RABBITHOLE_VOICE_ID to .env only for services that need them."
Write-Host "Prepared-project Resolve host: Resolve plus FFmpeg/ffprobe with libass; no browser, Poppler, or API key is needed."
Write-Host "End-to-end acquisition host: also install Chrome/Edge/Chromium and Poppler's pdftoppm."
Write-Host "Run: $RunCommand resolve doctor --mode free"
