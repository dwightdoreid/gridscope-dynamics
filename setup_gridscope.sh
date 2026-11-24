#!/usr/bin/env bash
# GridScope Dynamics — one-shot setup for Ubuntu/Debian.
#
# Usage (from anywhere):
#   ./setup_gridscope.sh                         # install everything, then run a quick Dynawo check
#   ./setup_gridscope.sh --tests                 # ... and run the test suite afterwards
#   ./setup_gridscope.sh --start                 # ... and start the app afterwards
#   ./setup_gridscope.sh --dynawo-dir /opt/dynawo_1.7.0   # install Dynawo somewhere else (default: vendor/dynawo)
#   ./setup_gridscope.sh --no-apt                # don't install system packages (no sudo)
#   ./setup_gridscope.sh --update                # reinstall / upgrade the Python packages
#
# What it does (each step is skipped when already done, so it is safe to re-run):
#   1. System packages: python3-venv, python3-pip, build-essential, cmake, curl, unzip (via sudo apt).
#   2. Python virtual environment in .venv next to this script, with requirements.txt.
#   3. Dynawo v1.7.0 (the version GridScope is tested with): downloads the Linux release from GitHub
#      and unpacks it into vendor/dynawo (or --dynawo-dir). The archive is kept in vendor/downloads.
#   4. ~/.itools/config.yml pointing PowSyBl at Dynawo (an existing file is backed up first).
#      This is the one file outside the project: pypowsybl only reads its config from ~/.itools.
#   5. A check: Dynawo's version and GridScope's own diagnostics (tools/diagnose.py).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DYNAWO_VERSION="1.7.0"
DYNAWO_ZIP="Dynawo_Linux_v${DYNAWO_VERSION}.zip"
DYNAWO_URL="https://github.com/dynawo/dynawo/releases/download/v${DYNAWO_VERSION}/${DYNAWO_ZIP}"
# Everything lives inside the project: Dynawo in vendor/dynawo (run_gridscope.sh looks there too),
# the downloaded archive in vendor/downloads, compiled custom models in vendor/gridscope_cache.
DYNAWO_HOME="${DYNAWO_HOME:-$APP_DIR/vendor/dynawo}"
CACHE_DIR="$APP_DIR/vendor/downloads"
DO_APT=1
UPDATE=0
RUN_TESTS=0
START=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dynawo-dir) DYNAWO_HOME="$2"; shift 2 ;;
    --no-apt)     DO_APT=0; shift ;;
    --update)     UPDATE=1; shift ;;
    --tests)      RUN_TESTS=1; shift ;;
    --start)      START=1; shift ;;
    -h|--help)    sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done
DYNAWO_HOME="${DYNAWO_HOME%/}"

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*"; exit 1; }

cd "$APP_DIR"
[[ -f app.py && -f requirements.txt ]] || die "app.py / requirements.txt not found in $APP_DIR — keep this script in the GridScope folder."
[[ "$(uname -s)" == Linux ]] || die "This script is for Linux. On Windows, download Dynawo_Windows_v${DYNAWO_VERSION}.zip by hand (see README)."

# ---------------------------------------------------------------- 1. system packages
say "1/5  System packages"
PYV=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || true)
need_pkgs=()
command -v python3 >/dev/null || need_pkgs+=(python3)
python3 -c 'import ensurepip, venv' 2>/dev/null || need_pkgs+=("python${PYV:-3}-venv")
for t in gcc g++ make; do command -v "$t" >/dev/null || { need_pkgs+=(build-essential); break; }; done
command -v cmake >/dev/null || need_pkgs+=(cmake)
command -v curl  >/dev/null || need_pkgs+=(curl)
command -v unzip >/dev/null || need_pkgs+=(unzip)

if [[ ${#need_pkgs[@]} -eq 0 ]]; then
  ok "python3 $PYV, venv, gcc/g++/make, cmake, curl and unzip are installed"
elif [[ $DO_APT -eq 1 ]]; then
  command -v apt-get >/dev/null || die "Missing: ${need_pkgs[*]}. No apt-get here — install them with your package manager and re-run with --no-apt."
  say "Installing with apt (sudo will ask for your password): ${need_pkgs[*]}"
  sudo apt-get update
  sudo apt-get install -y "${need_pkgs[@]}"
else
  die "Missing: ${need_pkgs[*]}. Install them (sudo apt install ${need_pkgs[*]}) or re-run without --no-apt."
fi

# ---------------------------------------------------------------- 2. Python environment
say "2/5  Python environment ($APP_DIR/.venv)"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || die "Could not create the virtual environment (python${PYV}-venv missing?)."
  UPDATE=1
fi
PY="$APP_DIR/.venv/bin/python"
if [[ $UPDATE -eq 1 ]] || ! "$PY" -c 'import streamlit, pypowsybl, plotly, pandas, numpy, cvxpy, pytest, mcp.server.mcpserver' 2>/dev/null; then
  echo "Installing requirements.txt (pypowsybl alone is ~70 MB — this can take a while on a slow link)…"
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt || die "Package installation failed (see messages above)."
fi
ok "Python packages: $("$PY" -c 'import pypowsybl, streamlit; print(f"pypowsybl {pypowsybl.__version__}, streamlit {streamlit.__version__}")' 2>/dev/null)"

# ---------------------------------------------------------------- 3. Dynawo
say "3/5  Dynawo v$DYNAWO_VERSION ($DYNAWO_HOME)"
if [[ -x "$DYNAWO_HOME/dynawo.sh" ]]; then
  ok "Already installed"
else
  [[ -e "$DYNAWO_HOME" ]] && die "$DYNAWO_HOME exists but has no dynawo.sh. Remove it or pick another --dynawo-dir."
  mkdir -p "$CACHE_DIR"
  ZIP="$CACHE_DIR/$DYNAWO_ZIP"
  if [[ -f "$ZIP" ]] && unzip -tqq "$ZIP" >/dev/null 2>&1; then
    ok "Using the already downloaded $ZIP"
  else
    echo "Downloading $DYNAWO_URL (~165 MB; an interrupted download resumes on re-run)…"
    # GitHub's CDN sometimes resets long downloads; curl's own --retry doesn't cover a reset
    # mid-transfer, so resume (-C -) in a loop from wherever the partial file ended.
    for attempt in $(seq 1 10); do
      curl -fL --retry 5 --retry-delay 3 --retry-all-errors -C - -o "$ZIP" "$DYNAWO_URL" && break
      [[ $attempt -eq 10 ]] && die "Download failed after 10 attempts. Re-run the script to resume."
      warn "Download interrupted (attempt $attempt/10) — resuming in 5 s…"
      sleep 5
    done
    unzip -tqq "$ZIP" >/dev/null 2>&1 || { rm -f "$ZIP"; die "The downloaded archive is corrupt and was deleted. Re-run the script."; }
  fi

  echo "Unpacking…"
  PARENT="$(dirname "$DYNAWO_HOME")"
  mkdir -p "$PARENT"
  TMP="$(mktemp -d "$PARENT/.dynawo_unpack.XXXXXX")"
  trap 'rm -rf "$TMP"' EXIT
  unzip -q "$ZIP" -d "$TMP"
  SH="$(find "$TMP" -maxdepth 3 -name dynawo.sh -type f | head -1)"
  [[ -n "$SH" ]] || die "dynawo.sh not found in $DYNAWO_ZIP — the release layout changed."
  mv "$(dirname "$SH")" "$DYNAWO_HOME"
  rm -rf "$TMP"; trap - EXIT
  chmod +x "$DYNAWO_HOME/dynawo.sh"
  ok "Installed to $DYNAWO_HOME"
fi
[[ -x "$DYNAWO_HOME/sbin/generate-preassembled" ]] || [[ -f "$DYNAWO_HOME/sbin/generate-preassembled" ]] \
  || warn "$DYNAWO_HOME lacks sbin/generate-preassembled — custom governor/AVR/PSS combinations won't compile."

# ---------------------------------------------------------------- 4. PowSyBl config
say "4/5  PowSyBl configuration (~/.itools/config.yml)"
CFG="$HOME/.itools/config.yml"
mkdir -p "$HOME/.itools"
if [[ ! -f "$CFG" ]]; then
  printf 'dynawo:\n  homeDir: %s\n  debug: false\n' "$DYNAWO_HOME" > "$CFG"
  ok "Wrote $CFG"
else
  CUR=$(awk '/^dynawo:/{d=1;next} /^[^[:space:]#]/{d=0} d && /^[[:space:]]+homeDir:/{sub(/^[[:space:]]+homeDir:[[:space:]]*/,""); print; exit}' "$CFG")
  if [[ "$CUR" == "$DYNAWO_HOME" ]]; then
    ok "Already points to $DYNAWO_HOME"
  else
    cp "$CFG" "$CFG.bak.$(date +%Y%m%d%H%M%S)"
    if grep -q '^dynawo:' "$CFG"; then
      if [[ -n "$CUR" ]]; then
        # replace the homeDir line inside the dynawo: block only
        awk -v h="$DYNAWO_HOME" '/^dynawo:/{d=1;print;next} /^[^[:space:]#]/{d=0}
             d && /^[[:space:]]+homeDir:/{print "  homeDir: " h; next} {print}' "$CFG" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
      else
        awk -v h="$DYNAWO_HOME" '{print} /^dynawo:/{print "  homeDir: " h}' "$CFG" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
      fi
    else
      printf '\ndynawo:\n  homeDir: %s\n  debug: false\n' "$DYNAWO_HOME" >> "$CFG"
    fi
    ok "Updated $CFG (previous version backed up next to it)"
  fi
fi

# ---------------------------------------------------------------- 5. checks
say "5/5  Checks"
if "$DYNAWO_HOME/dynawo.sh" version >/dev/null 2>&1; then
  ok "Dynawo runs: $("$DYNAWO_HOME/dynawo.sh" version 2>/dev/null | head -1)"
else
  warn "'$DYNAWO_HOME/dynawo.sh version' failed. Output:"
  "$DYNAWO_HOME/dynawo.sh" version || true
fi
echo "Running GridScope diagnostics (IEEE 9-bus reference simulations)…"
"$PY" tools/diagnose.py || warn "Diagnostics reported a problem (see above)."

if [[ $RUN_TESTS -eq 1 ]]; then
  say "Test suite"
  "$PY" -m pytest tests -q -p no:warnings -rs || warn "Some tests failed (see above)."
fi

say "Setup complete"
echo "Start the app with:   $APP_DIR/run_gridscope.sh   (then open http://localhost:8501)"
echo "Run the tests with:   $PY -m pytest tests"
if [[ "$DYNAWO_HOME" != "$APP_DIR/vendor/dynawo" ]]; then
  echo "(run_gridscope.sh reads the Dynawo location from ~/.itools/config.yml, so the custom path is picked up.)"
fi

if [[ $START -eq 1 ]]; then
  exec "$APP_DIR/run_gridscope.sh"
fi
