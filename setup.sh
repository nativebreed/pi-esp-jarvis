#!/usr/bin/env bash
# Pi-Esp Jarvis: one-command setup for the Raspberry Pi side.
#
#   bash setup.sh
#
# Installs Docker if needed, asks a few questions, writes .env, and starts the
# assistant. Safe to run again at any time (it keeps your existing settings).
set -euo pipefail
cd "$(dirname "$0")"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
esc() { printf '%s' "$1" | sed 's/[&|\\]/\\&/g'; }   # safe for sed replacements

bold "Pi-Esp Jarvis setup"
echo

# ------------------------------------------------------------------ checks
ARCH="$(uname -m)"
if [ "$ARCH" != "aarch64" ] && [ "$ARCH" != "x86_64" ]; then
  echo "!! This needs a 64-bit OS (found $ARCH). On a Raspberry Pi, install 'Raspberry Pi OS (64-bit)'."
  exit 1
fi
MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024 + 0.5}' /proc/meminfo)
if [ "$MEM_GB" -lt 6 ]; then
  note "Your Pi has ${MEM_GB} GB of RAM. I'll use the smaller local model (llama3.2:1b) so it fits."
  SMALL_MODEL=1
fi

# ------------------------------------------------------------------ docker
DOCKER="docker"
if ! command -v docker >/dev/null 2>&1; then
  bold "Installing Docker (this takes a few minutes)..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
  note "Docker installed. (Log out and back in later so you can use 'docker' without sudo.)"
fi
if ! docker info >/dev/null 2>&1; then
  DOCKER="sudo docker"   # group membership not active in this login yet
fi

# ------------------------------------------------------------------ settings
if [ ! -f .env ]; then
  cp .env.example .env
  echo
  bold "A few questions (press Enter to skip any of them)"

  echo
  note "1. Claude API key. Optional: without it everything runs locally."
  note "   Get one at https://console.anthropic.com/ (it starts with sk-ant-)."
  read -r -s -p "   Key: " KEY; echo
  if [ -n "$KEY" ]; then
    sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$(esc "$KEY")|" .env
  fi

  echo
  note "2. Your town, for the weather (e.g. 'London, GB' or 'Austin, US')."
  read -r -p "   Town: " TOWN
  if [ -n "$TOWN" ]; then
    sed -i "s|^WEATHER_LOCATION=.*|WEATHER_LOCATION=$(esc "$TOWN")|" .env
  fi

  echo
  note "3. Units: metric (°C, km/h), uk (°C, mph) or imperial (°F, mph)."
  read -r -p "   Units [metric]: " UNITS
  case "${UNITS:-metric}" in
    metric|uk|imperial) sed -i "s|^WEATHER_UNITS=.*|WEATHER_UNITS=${UNITS:-metric}|" .env ;;
    *) note "   Not recognised, keeping metric." ;;
  esac

  if [ "${SMALL_MODEL:-0}" = "1" ]; then
    sed -i "s|^LOCAL_MODEL=.*|LOCAL_MODEL=llama3.2:1b|" .env
  fi
  note "Saved your answers in .env (edit it any time, then run: docker compose up -d)"
else
  note "Keeping your existing .env settings."
fi
mkdir -p music

# ------------------------------------------------------------------ start
echo
bold "Building and starting the assistant (the first time takes 10-20 minutes)..."
$DOCKER compose up -d --build

# ------------------------------------------------------------------ Pi helper
echo
read -r -p "Let Jarvis control this Pi (status, open websites/apps, reboot with confirmation)? [y/N] " HELPER
if [[ "${HELPER:-n}" =~ ^[Yy] ]]; then
  bash pi-helper/install.sh
  $DOCKER compose up -d
fi

# ------------------------------------------------------------------ done
HOST="$(hostname).local"
echo
bold "All set on the Pi side!"
note "The models are downloading in the background (about 2 GB the first time)."
note "Watch progress:   $DOCKER compose logs -f assistant"
note "Ready when you see:  Local model ... ready"
note "Your lists page:  http://$HOST:8000/lists"
echo
bold "Next: flash the ESP32 board"
note "Plug the board into this Pi with a USB-C data cable, then run:"
note "   bash flash-esp32.sh        (board revision 1)"
note "   bash flash-esp32.sh v2     (board revision 2: 'V2' printed on the board or box)"
