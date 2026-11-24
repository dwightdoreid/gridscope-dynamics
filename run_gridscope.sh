#!/usr/bin/env bash
# GridScope Dynamics launcher for Ubuntu.
#
# Usage (from anywhere):
#   ./run_gridscope.sh                       # set up (first time), start the app and open it in the browser
#   ./run_gridscope.sh --no-browser          # start without opening a browser tab
#   ./run_gridscope.sh --port 8502           # another port
#   ./run_gridscope.sh --dynawo /path/to/dynawo   # folder containing dynawo.sh
#   ./run_gridscope.sh --no-venv             # use the current Python environment instead of .venv
#   ./run_gridscope.sh --update              # reinstall / upgrade the Python packages
#
# First run: creates a virtual environment in .venv next to this script, installs the Python
# packages, and writes ~/.itools/config.yml (Dynawo location) if it doesn't exist yet.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8501
USE_VENV=1
OPEN_BROWSER=1
UPDATE=0
DYNAWO_HOME="${DYNAWO_HOME:-$APP_DIR/vendor/dynawo}"   # where setup_gridscope.sh installs it

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)    PORT="$2"; shift 2 ;;
    --dynawo)  DYNAWO_HOME="$2"; shift 2 ;;
    --no-venv) USE_VENV=0; shift ;;
    --no-browser) OPEN_BROWSER=0; shift ;;
    --update)  UPDATE=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*"; exit 1; }

cd "$APP_DIR"
[[ -f app.py ]] || die "app.py not found in $APP_DIR — keep this script in the GridScope folder."

# ---------------------------------------------------------------- Python
command -v python3 >/dev/null || die "python3 not found. Install it: sudo apt install python3 python3-venv python3-pip"
PY=python3

if [[ $USE_VENV -eq 1 ]]; then
  if [[ ! -x .venv/bin/python ]]; then
    say "Creating virtual environment in $APP_DIR/.venv"
    if ! $PY -m venv .venv 2>/dev/null; then
      PYV=$($PY -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
      die "Could not create a virtual environment. Install venv support: sudo apt install python${PYV}-venv  (or run with --no-venv)"
    fi
    UPDATE=1
  fi
  PY="$APP_DIR/.venv/bin/python"
fi

if [[ $UPDATE -eq 1 ]] || ! $PY -c 'import streamlit, pypowsybl, plotly, cvxpy' 2>/dev/null; then
  say "Installing Python packages (requirements.txt)"
  PIP_FLAGS=()
  [[ $USE_VENV -eq 0 ]] && PIP_FLAGS+=(--user)
  $PY -m pip install --upgrade pip >/dev/null 2>&1 || true
  $PY -m pip install "${PIP_FLAGS[@]}" -r requirements.txt \
    || die "Package installation failed (see messages above)."
fi

# ---------------------------------------------------------------- Dynawo
CFG="$HOME/.itools/config.yml"
if [[ -f "$CFG" ]] && grep -q 'homeDir' "$CFG"; then
  CFG_HOME=$(sed -n 's/^[[:space:]]*homeDir:[[:space:]]*//p' "$CFG" | head -1)
  if [[ -f "$CFG_HOME/dynawo.sh" ]]; then
    say "Dynawo: $CFG_HOME"
  else
    warn "~/.itools/config.yml points to '$CFG_HOME', which has no dynawo.sh — Dynamic Stability will be disabled."
  fi
elif [[ -f "$DYNAWO_HOME/dynawo.sh" ]]; then
  say "Writing $CFG (Dynawo: $DYNAWO_HOME)"
  mkdir -p "$HOME/.itools"
  if [[ -f "$CFG" ]]; then
    cp "$CFG" "$CFG.bak"
    printf '\ndynawo:\n  homeDir: %s\n  debug: false\n' "$DYNAWO_HOME" >> "$CFG"
  else
    printf 'dynawo:\n  homeDir: %s\n  debug: false\n' "$DYNAWO_HOME" > "$CFG"
  fi
else
  warn "Dynawo not found (looked in $DYNAWO_HOME). Steady-state studies work; for Dynamic Stability"
  warn "rerun with --dynawo /path/to/folder/containing/dynawo.sh"
fi

# compiling new governor/AVR/PSS combinations needs a C/C++ tool chain
missing=()
for t in gcc g++ make cmake; do command -v "$t" >/dev/null || missing+=("$t"); done
if [[ ${#missing[@]} -gt 0 ]]; then
  warn "Not installed: ${missing[*]}. Combinations Dynawo doesn't ship pre-assembled can't be compiled."
  warn "To enable them: sudo apt install build-essential cmake"
fi

# ---------------------------------------------------------------- run
if command -v ss >/dev/null && ss -ltn "( sport = :$PORT )" | grep -q ":$PORT"; then
  die "Port $PORT is already in use — try: $0 --port $((PORT + 1))"
fi
say "Starting GridScope Dynamics on http://localhost:$PORT  (Ctrl+C to stop)"
# Streamlit stays headless (with headless = false it first asks for an e-mail address on the
# terminal). Instead, a background helper opens the default browser once the server answers.
URL="http://localhost:$PORT"
if [[ $OPEN_BROWSER -eq 1 ]]; then
  if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v xdg-open >/dev/null; then
    (
      for _ in $(seq 1 60); do
        if "$PY" -c "import urllib.request,sys; urllib.request.urlopen('$URL/_stcore/health', timeout=1)" 2>/dev/null; then
          xdg-open "$URL" >/dev/null 2>&1 &
          exit 0
        fi
        sleep 1
      done
    ) &
  else
    warn "No desktop session found — open $URL in a browser yourself."
  fi
fi
exec "$PY" -m streamlit run app.py --server.port "$PORT" --server.headless true \
  --browser.gatherUsageStats false
