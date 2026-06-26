#!/usr/bin/env bash
#
# Uninstaller for the headphone idle-hiss fix. Removes per-user files and
# disables the service. Does NOT touch /etc/modprobe.d (remove that by hand
# if you added it).
#
set -euo pipefail

BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
PULSE_FILE="$HOME/.config/pulse/default.pa"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

say "Stopping and disabling services"
systemctl --user disable --now headphone-hiss-park.service 2>/dev/null || true
systemctl --user disable --now headphone-hum-mask.service 2>/dev/null || true
"$BIN_DIR/headphone-hiss-park.py" stop 2>/dev/null || true

say "Removing scripts and units"
rm -f "$BIN_DIR/headphone-hiss-park.py" "$BIN_DIR/headphone-hum-mask.py"
rm -f "$UNIT_DIR/headphone-hiss-park.service" "$UNIT_DIR/headphone-hum-mask.service"
systemctl --user daemon-reload

if [[ -f "$PULSE_FILE" ]]; then
  say "Leaving $PULSE_FILE in place."
  echo "  To restore stock behaviour, delete it (or your latest default.pa.bak.*) and run: pulseaudio -k"
fi

say "Uninstalled (per-user files)."
echo "If you added /etc/modprobe.d/audio_powersave.conf, remove it with:"
echo "    sudo rm /etc/modprobe.d/audio_powersave.conf && sudo update-initramfs -u"
