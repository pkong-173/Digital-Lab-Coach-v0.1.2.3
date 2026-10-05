#!/usr/bin/env bash
# Digital Lab Coach launcher (macOS / Linux).
# Windows users: double-click START_HERE.bat instead.
set -e
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing the uv package manager - one-time setup..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if curl -s --max-time 1 -o /dev/null http://127.0.0.1:8765/; then
  echo "Digital Lab Coach is already running - opening your browser."
  if command -v open >/dev/null 2>&1; then open http://127.0.0.1:8765
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open http://127.0.0.1:8765
  fi
  exit 0
fi

echo "Preparing packages - the first run can take a few minutes..."
uv sync

export DLC_ENFORCE_LIMITS=1
# The server opens the browser itself, the moment it is ready to answer,
# and stops itself when the last DLC tab is closed.
export DLC_OPEN_BROWSER=1
export DLC_AUTO_EXIT=1
echo "Starting Digital Lab Coach at http://127.0.0.1:8765 ..."
echo "Your browser opens by itself when the app is ready (the first start can take a minute)."
echo "Closing the DLC page in the browser stops the app; this window can then be closed."
uv run python -m dlc.web.server
