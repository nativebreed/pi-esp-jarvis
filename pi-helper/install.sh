#!/usr/bin/env bash
# Installs the Jarvis Pi helper as a background service for your user.
#   bash pi-helper/install.sh
set -euo pipefail
cd "$(dirname "$0")"
HERE="$PWD"
ROOT="$(dirname "$HERE")"
CONF="$HOME/.config/jarvis-helper"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$CONF" "$UNIT_DIR"

# 1. secret token shared with the assistant container
if [ ! -s "$CONF/token" ]; then
  python3 -c 'import secrets; print(secrets.token_hex(16))' > "$CONF/token"
  chmod 600 "$CONF/token"
fi
TOKEN="$(cat "$CONF/token")"

# 2. background service (runs as you, inside your desktop session so it can open apps)
cat > "$UNIT_DIR/jarvis-helper.service" <<EOF
[Unit]
Description=Jarvis Pi helper
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 $HERE/helper.py
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_RUNTIME_DIR=/run/user/%U
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

# keep user services running after reboot, even before you log in
sudo loginctl enable-linger "$USER" || true
systemctl --user daemon-reload
systemctl --user enable --now jarvis-helper.service

# 3. tell the assistant about it
ENV_FILE="$ROOT/.env"
touch "$ENV_FILE"
if grep -q '^PI_HELPER_TOKEN=' "$ENV_FILE"; then
  sed -i "s/^PI_HELPER_TOKEN=.*/PI_HELPER_TOKEN=$TOKEN/" "$ENV_FILE"
else
  printf '\n# Pi helper (added by pi-helper/install.sh)\nPI_HELPER_TOKEN=%s\n' "$TOKEN" >> "$ENV_FILE"
fi

sleep 1
if curl -s -H "X-Token: $TOKEN" http://127.0.0.1:8766/status >/dev/null; then
  echo "Pi helper is running."
else
  echo "!! The helper didn't answer. See: journalctl --user -u jarvis-helper -n 50"
fi
echo "Restart the assistant to use it:  docker compose up -d"
