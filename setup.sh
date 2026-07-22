#!/usr/bin/env bash
#
# One-command setup for Anaplan Audit History (macOS / Linux).
#
#   1. Installs uv (the Python package manager) if it isn't already present.
#   2. Installs Python 3.13 and every dependency into a local .venv.
#   3. Optionally launches the interactive configuration wizard.
#
# Run it from a terminal:
#     bash setup.sh
# …or make it executable and double-click / run directly:
#     chmod +x setup.sh && ./setup.sh
#
# Safe to re-run — it only installs what's missing.

set -euo pipefail

# Always operate from the repo root (the directory this script lives in),
# so it works whether run from the terminal or double-clicked.
cd "$(dirname "$0")"

echo "=================================================="
echo " Anaplan Audit History — setup"
echo "=================================================="
echo

# --- 1. Ensure uv is installed --------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    echo "[1/3] Installing uv (Python package manager)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer drops uv in ~/.local/bin; add it to PATH for this run.
    export PATH="$HOME/.local/bin:$PATH"
else
    echo "[1/3] uv already installed."
fi

# Belt-and-suspenders: make sure uv is reachable even if PATH was odd.
if ! command -v uv >/dev/null 2>&1; then
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
    echo
    echo "ERROR: uv was installed but isn't on your PATH yet."
    echo "Close and reopen your terminal, then re-run: bash setup.sh"
    exit 1
fi

echo "      uv $(uv --version | awk '{print $2}')"
echo

# --- 2. Install Python 3.13 + dependencies --------------------------------
echo "[2/3] Installing Python 3.13 and dependencies (first run can take a minute)…"
uv sync
echo "      Done."
echo

# --- 3. Confirm the CLI works ---------------------------------------------
echo "[3/3] Verifying the install…"
uv run anaplan-audit version
echo

echo "=================================================="
echo " Setup complete."
echo "=================================================="
echo
echo "This installed everything needed to RUN the tool."
echo "You do NOT need pytest, mypy, ruff, or any other developer"
echo "tools — those are only for running the test suite."
echo
echo "Next steps:"
echo "  1. Configure your tenant:   uv run anaplan-audit init"
echo "  2. (OAuth) register once:   uv run anaplan-audit register --client-id <YOUR_CLIENT_ID>"
echo "  3. Validate everything:     uv run anaplan-audit validate-config"
echo "  4. Safe test run:           uv run anaplan-audit run --dry-run --limit 500 --verbose"
echo

# --- Optional: launch the config wizard now -------------------------------
ans=""
read -r -p "Run the configuration wizard now? [y/N] " ans || ans=""
if [[ "$ans" =~ ^[Yy]$ ]]; then
    echo
    uv run anaplan-audit init
fi
