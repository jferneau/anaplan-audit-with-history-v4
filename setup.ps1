# One-command setup for Anaplan Audit History (Windows, PowerShell).
#
#   1. Installs uv (the Python package manager) if it isn't already present.
#   2. Installs Python 3.13 and every dependency into a local .venv.
#   3. Optionally launches the interactive configuration wizard.
#
# Run it from PowerShell in the project folder:
#     powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Safe to re-run — it only installs what's missing.

$ErrorActionPreference = "Stop"

# Always operate from the folder this script lives in.
Set-Location -Path $PSScriptRoot

Write-Host "=================================================="
Write-Host " Anaplan Audit History - setup"
Write-Host "=================================================="
Write-Host ""

# --- 1. Ensure uv is installed --------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[1/3] Installing uv (Python package manager)..."
    powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    # The installer drops uv in %USERPROFILE%\.local\bin; add it for this run.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
else {
    Write-Host "[1/3] uv already installed."
}

# Belt-and-suspenders PATH refresh.
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "ERROR: uv was installed but isn't on your PATH yet." -ForegroundColor Red
    Write-Host "Close and reopen PowerShell, then re-run: powershell -ExecutionPolicy Bypass -File setup.ps1"
    exit 1
}

Write-Host ("      uv " + (uv --version))
Write-Host ""

# --- 2. Install Python 3.13 + dependencies --------------------------------
Write-Host "[2/3] Installing Python 3.13 and dependencies (first run can take a minute)..."
uv sync
Write-Host "      Done."
Write-Host ""

# --- 3. Confirm the CLI works ---------------------------------------------
Write-Host "[3/3] Verifying the install..."
uv run anaplan-audit version
Write-Host ""

Write-Host "=================================================="
Write-Host " Setup complete."
Write-Host "=================================================="
Write-Host ""
Write-Host "This installed everything needed to RUN the tool."
Write-Host "You do NOT need pytest, mypy, ruff, or any other developer"
Write-Host "tools - those are only for running the test suite."
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Configure your tenant:   uv run anaplan-audit init"
Write-Host "  2. (OAuth) register once:   uv run anaplan-audit register --client-id <YOUR_CLIENT_ID>"
Write-Host "  3. Validate everything:     uv run anaplan-audit validate-config"
Write-Host "  4. Safe test run:           uv run anaplan-audit run --dry-run --limit 500 --verbose"
Write-Host ""

# --- Optional: launch the config wizard now -------------------------------
$ans = Read-Host "Run the configuration wizard now? [y/N]"
if ($ans -match '^[Yy]') {
    Write-Host ""
    uv run anaplan-audit init
}
