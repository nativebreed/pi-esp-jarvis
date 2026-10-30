#!/usr/bin/env bash
# Build the ESP32 firmware (in Docker) and flash it to the board over USB.
#
#   ./flash-esp32.sh            # board revision 1 (SH8601 screen)
#   ./flash-esp32.sh v2         # board revision 2 (CO5300 screen)
#
# Optional environment variables:
#   WAKE_WORD=wn9_jarvis_tts    # or: hiesp ("Hi ESP"), wn9_alexa, ...
#   OTA_URL=http://<pi>:8000/xiaozhi/ota/   # default: this Pi's hostname.local
#   PORT=/dev/ttyACM0           # default: auto-detect
#   REBUILD=1                   # force a fresh firmware build
#   FLIP_OFFSET=16,0            # only if the picture sits off-centre when the screen flips
set -euo pipefail
cd "$(dirname "$0")"

VARIANT="${1:-${BOARD_VARIANT:-v1}}"
case "$VARIANT" in v1|v2) ;; *) echo "Usage: $0 [v1|v2]"; exit 1 ;; esac
WAKE_WORD="${WAKE_WORD:-wn9_jarvis_tts}"
OTA_URL="${OTA_URL:-http://$(hostname).local:8000/xiaozhi/ota/}"
OUT="$PWD/firmware/out"

# ---------------------------------------------------------------- device key
# A secret only this Pi and the board know. It's built into the firmware and kept in .env as DEVICE_KEY,
# so the Pi only talks to your board (not to anything else on the network that asks).
DEVICE_KEY="${DEVICE_KEY:-$(grep -E '^DEVICE_KEY=' .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '[:space:]\"' || true)}"
if [ -z "$DEVICE_KEY" ]; then
  DEVICE_KEY="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-32)"
  if [ -f .env ]; then
    printf '\n# Built into the board by flash-esp32.sh: only a board with this key can connect\nDEVICE_KEY=%s\n' "$DEVICE_KEY" >> .env
    echo "==> Made a device key for this board and saved it in .env"
    NEW_KEY=1
  else
    echo "!! No .env file here (run setup.sh first): building without a device key"
    DEVICE_KEY=""
  fi
fi
IMAGE="pi-esp-jarvis-firmware"
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"
mkdir -p "$OUT"

# ---------------------------------------------------------------- build
# rebuild automatically when the settings or any firmware customisation change
FW_HASH="$(cat firmware/Dockerfile firmware/fw.sh firmware/patch_firmware.py firmware/face/emoji/*.gif | sha1sum | cut -c1-12)"
KEY_HASH="$(printf '%s' "$DEVICE_KEY" | sha1sum | cut -c1-8)"
STAMP="$VARIANT|$WAKE_WORD|$OTA_URL|${FLIP_OFFSET:-}|$FW_HASH|$KEY_HASH"
if [ "${REBUILD:-0}" = "1" ] || [ ! -f "$OUT/firmware.bin" ] || [ "$(cat "$OUT/stamp" 2>/dev/null)" != "$STAMP" ]; then
  echo "==> Building firmware (first time takes 20-30 minutes on a Pi 5)"
  $DOCKER build -t "$IMAGE" firmware
  rm -f "$OUT/firmware.bin" "$OUT/stamp"
  $DOCKER run --rm \
    -e BOARD_VARIANT="$VARIANT" -e WAKE_WORD="$WAKE_WORD" -e OTA_URL="$OTA_URL" -e FLIP_OFFSET="${FLIP_OFFSET:-}" \
    -e DEVICE_KEY="$DEVICE_KEY" \
    -v "$OUT:/out" -v esp32-fw-cache:/root/.cache \
    "$IMAGE" build
  echo "$STAMP" > "$OUT/stamp"
else
  echo "==> Using existing firmware/out/firmware.bin (REBUILD=1 to rebuild)"
fi

# ---------------------------------------------------------------- flash
if [ -z "${PORT:-}" ]; then
  PORT="$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -n1 || true)"
fi
if [ -z "$PORT" ] || [ ! -e "$PORT" ]; then
  echo "!! No ESP32 found. Plug the board into the Pi with a USB data cable and run this again."
  echo "   (Still not found? Unplug it, hold BOOT while plugging it back in, then retry.)"
  exit 1
fi

echo "==> Flashing $PORT"
$DOCKER run --rm --device "$PORT" -v "$OUT:/out" "$IMAGE" flash "$PORT"

echo
echo "All done. On first boot the board opens a Wi-Fi hotspot to set up your network."
echo "Server address baked in: $OTA_URL"
if [ -n "$DEVICE_KEY" ]; then
  echo "Device key built in. Now restart Jarvis on the Pi so it expects it:  docker compose up -d --build"
fi
