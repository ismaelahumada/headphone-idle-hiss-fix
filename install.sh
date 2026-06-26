#!/usr/bin/env bash
#
# Installer for the headphone idle-hiss fix.
# Installs per-user files only. The one root-level step (snd_hda_intel
# power_save) is printed at the end for you to apply manually with sudo.
#
# Usage:
#   ./install.sh          Install + enable the park daemon
#   ./install.sh --help    Show this help
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
PULSE_DIR="$HOME/.config/pulse"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

# 1. Scripts -----------------------------------------------------------------
say "Installing scripts to $BIN_DIR"
mkdir -p "$BIN_DIR"
install -m 0755 "$REPO_DIR/bin/headphone-hiss-park.py" "$BIN_DIR/"
install -m 0755 "$REPO_DIR/bin/headphone-hum-mask.py"  "$BIN_DIR/"

# 2. systemd user units ------------------------------------------------------
say "Installing systemd user units to $UNIT_DIR"
mkdir -p "$UNIT_DIR"
install -m 0644 "$REPO_DIR/systemd/headphone-hiss-park.service" "$UNIT_DIR/"
install -m 0644 "$REPO_DIR/systemd/headphone-hum-mask.service"  "$UNIT_DIR/"

# 3. PulseAudio suspend-on-idle override -------------------------------------
if command -v pulseaudio >/dev/null 2>&1 && pactl info 2>/dev/null | grep -qi "Server Name: pulseaudio"; then
  say "Installing PulseAudio suspend-on-idle override"
  mkdir -p "$PULSE_DIR"
  if [[ -f "$PULSE_DIR/default.pa" ]] && ! cmp -s "$REPO_DIR/config/pulse/default.pa" "$PULSE_DIR/default.pa"; then
    backup="$PULSE_DIR/default.pa.bak.$(date +%Y%m%d%H%M%S)"
    warn "Existing $PULSE_DIR/default.pa found -> backing up to $backup"
    cp "$PULSE_DIR/default.pa" "$backup"
  fi
  install -m 0644 "$REPO_DIR/config/pulse/default.pa" "$PULSE_DIR/default.pa"
  say "Restarting PulseAudio"
  pulseaudio -k 2>/dev/null || true
  sleep 1
else
  warn "PulseAudio not detected as the active server."
  warn "Skipping the suspend-on-idle override (it only applies to PulseAudio)."
  warn "If you use PipeWire/WirePlumber, set session.suspend-timeout-seconds instead."
fi

# 4. Enable the park daemon --------------------------------------------------
say "Enabling and starting the park daemon"
systemctl --user daemon-reload
systemctl --user enable --now headphone-hiss-park.service

say "Done."
echo
echo "  Status:  systemctl --user status headphone-hiss-park"
echo "  Logs:    journalctl --user -u headphone-hiss-park -f"
echo "  Stop:    systemctl --user stop headphone-hiss-park"
echo "  Disable: systemctl --user disable --now headphone-hiss-park"
echo
warn "ONE manual root step (Intel HD Audio codec power-down, optional but recommended):"
echo "    sudo install -m 0644 \"$REPO_DIR/config/modprobe/audio_powersave.conf\" /etc/modprobe.d/audio_powersave.conf"
echo "    sudo update-initramfs -u   # or reboot"
echo "  Verify after reboot:  cat /sys/module/snd_hda_intel/parameters/power_save   # -> 1"
