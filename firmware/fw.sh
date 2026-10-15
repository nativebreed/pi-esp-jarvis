#!/usr/bin/env bash
# Runs inside the builder container.
#   fw.sh build          -> /out/firmware.bin
#   fw.sh flash <port>   -> writes /out/firmware.bin to the board
set -euo pipefail
# shellcheck disable=SC1091
source "${IDF_PATH}/export.sh" >/dev/null 2>&1

cmd="${1:-build}"

case "$cmd" in
  build)
    : "${BOARD_VARIANT:=v1}" "${OTA_URL:?OTA_URL is required}" "${WAKE_WORD:=wn9_jarvis_tts}"
    if [ "$BOARD_VARIANT" = "v2" ]; then
      name="esp32-s3-touch-amoled-1.8-v2"
    else
      name="esp32-s3-touch-amoled-1.8"
    fi
    board_dir="waveshare/$name"
    echo ">>> Board: $name   Wake word: $WAKE_WORD"
    echo ">>> Server: $OTA_URL"

    # Point the firmware at the Pi, black "big face" screen with our animated
    # character, and double-tap to talk.  (see patch_firmware.py)
    python3 /opt/patch_firmware.py "$name" "$OTA_URL" /opt/face-emoji

    python3 scripts/build.py "$board_dir" --name "$name" \
        --language en-US --wake-word "$WAKE_WORD"

    cp build/merged-binary.bin /out/firmware.bin
    echo ">>> Built /out/firmware.bin ($(du -h /out/firmware.bin | cut -f1))"
    ;;

  flash)
    port="${2:?usage: fw.sh flash <port>}"
    echo ">>> Flashing /out/firmware.bin to $port (erasing the whole chip first)"
    python3 -m esptool --chip esp32s3 -p "$port" -b 921600 \
        write_flash --erase-all 0x0 /out/firmware.bin
    echo ">>> Done. The board restarts by itself."
    ;;

  *)
    exec "$@"
    ;;
esac
