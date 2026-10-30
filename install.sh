#!/usr/bin/env bash
# Installs SquintThru on the Steam Frame: a background service that starts with
# your session, an app-menu entry, and the `squintthru` command.
#
# One line, nothing to download first (also how you update):
#   curl -fsSL https://raw.githubusercontent.com/huntergcook/SquintThru/main/install.sh | bash
# From a clone:
#   ./install.sh               install or update (safe to run again after git pull)
#   ./install.sh --uninstall   remove it (your settings are kept)
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user, without sudo. SquintThru installs into your own home folder" >&2
  echo "and runs as you, so it can see Steam and the eye tracker." >&2
  exit 1
fi

# Run through curl | bash, there's no checkout next to this script: fetch one
# into ~/.local/share/squintthru/app and run its installer. The braces make bash
# read this whole block before running it, so a cut-off download does nothing.
{
SRC="${BASH_SOURCE[0]:-}"
if [[ -z "$SRC" || ! -f "$(dirname "$SRC")/squint.py" ]]; then
  REPO="${SQUINTTHRU_REPO:-https://github.com/huntergcook/SquintThru}"
  REF="${SQUINTTHRU_REF:-main}"
  DEST="${XDG_DATA_HOME:-$HOME/.local/share}/squintthru/app"
  command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
  echo "Downloading SquintThru ($REF)..."
  mkdir -p "$(dirname "$DEST")"
  if command -v git >/dev/null; then
    if [[ -d "$DEST/.git" ]]; then
      git -C "$DEST" fetch -q --depth 1 "$REPO" "$REF"
      git -C "$DEST" reset -q --hard FETCH_HEAD
    else
      rm -rf "$DEST"
      git clone -q --depth 1 --branch "$REF" "$REPO" "$DEST"
    fi
  else
    # No git: unpack GitHub's tarball of the branch instead.
    rm -rf "$DEST"
    python3 - "$REPO/archive/$REF.tar.gz" "$DEST" <<'PY'
import io, os, sys, tarfile, urllib.request
url, dest = sys.argv[1:]
data = urllib.request.urlopen(url, timeout=60).read()
with tarfile.open(fileobj=io.BytesIO(data)) as t:
    top = t.getmembers()[0].name.split("/")[0]
    for m in t.getmembers():
        m.name = m.name[len(top) + 1:]
        if m.name:
            t.extract(m, dest, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
PY
  fi
  exec bash "$DEST/install.sh" "$@"
fi
}
HERE="$(cd "$(dirname "$(readlink -f "$SRC")")" && pwd)"
UNIT="squintthru.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
BIN="$HOME/.local/bin"
CEF_FLAG=".cef-enable-remote-debugging"
STEAM_DIRS=("$HOME/.local/share/Steam" "$HOME/.steam/steam")

AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/squintthru.desktop"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# The background service runs under your user's systemd. Shells that don't carry the
# desktop session's environment can't reach it ("Failed to connect to user scope bus"),
# so point them at the session the usual way before giving up on it.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "$XDG_RUNTIME_DIR/bus" ]]; then
  export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi
user_systemd() { systemctl --user show-environment >/dev/null 2>&1; }

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  pkill -f "python3 -u $HERE/squint.py" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT" "$APPS/squintthru.desktop" "$BIN/squintthru" "$AUTOSTART"
  systemctl --user daemon-reload 2>/dev/null || true
  say "SquintThru removed."
  echo "Settings are still in ~/.config/squintthru. Steam debugging is still on;"
  echo "to turn it off, delete $HOME/.local/share/Steam/$CEF_FLAG and restart Steam."
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "python3 3.8 or newer is required" >&2; exit 1; }
chmod +x "$HERE/squintthru" "$HERE/install.sh"

# Optional: the compiled eye reader. squint.py reads eye data itself and only
# falls back to this if that fails, so a missing compiler is fine.
if command -v gcc >/dev/null; then
  gcc -O2 -o "$HERE/eyestream" "$HERE/eyestream.c" -lpthread && echo "Built eyestream fallback."
fi

# Steam's CEF debug port is how SquintThru presses the Room View button.
cef_open() { python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/json', timeout=2)" 2>/dev/null; }
restart_steam=0
if ! cef_open; then
  for d in "${STEAM_DIRS[@]}"; do
    if [[ -d "$d" ]]; then
      touch "$d/$CEF_FLAG"
      restart_steam=1
      echo "Turned on Steam debugging ($d/$CEF_FLAG)."
      break
    fi
  done
  [[ $restart_steam == 1 ]] || echo "Couldn't find your Steam folder. Start Steam with -cef-enable-debugging instead."
fi

# Stop a copy started by hand the old way, so the service can take the port.
pkill -f "python3 -u squint.py" 2>/dev/null || true
pkill -f "python3 -u $HERE/squint.py" 2>/dev/null || true

mkdir -p "$APPS" "$BIN"

if user_systemd; then
use_systemd=1
rm -f "$AUTOSTART"
mkdir -p "$UNIT_DIR"
cat >"$UNIT_DIR/$UNIT" <<EOF
[Unit]
Description=SquintThru: squint to see your room
After=default.target

[Service]
ExecStart="$(command -v python3)" -u "$HERE/squint.py"
WorkingDirectory=$HERE
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
else
# No reachable systemd user session: start SquintThru when you log in to the desktop instead.
use_systemd=0
mkdir -p "$(dirname "$AUTOSTART")"
cat >"$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=SquintThru service
Comment=Starts SquintThru in the background
Exec="$HERE/squintthru" start
Icon=$HERE/icon.svg
Terminal=false
NoDisplay=true
X-KDE-autostart-phase=2
EOF
fi

cat >"$APPS/squintthru.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SquintThru
Comment=Squint to see your room
Exec="$HERE/squintthru" open
Icon=$HERE/icon.svg
Terminal=false
Categories=Utility;Game;
StartupWMClass=SquintThru
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true

ln -sf "$HERE/squintthru" "$BIN/squintthru"

if [[ $use_systemd == 1 ]]; then
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT" >/dev/null 2>&1
  systemctl --user restart "$UNIT"
else
  "$HERE/squintthru" start >/dev/null
fi

say "SquintThru is installed and running."
# Show the app right away when there's a screen to show it on.
if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  nohup "$HERE/squintthru" open >/dev/null 2>&1 &
fi
echo "  Open the app:   SquintThru in your app menu, or run: squintthru"
echo "  Update later:   run the same install command again"
if [[ $use_systemd == 1 ]]; then
  echo "  It starts by itself whenever you log in."
else
  echo "  It starts by itself when you log in to the desktop. (This terminal couldn't reach"
  echo "  systemd, so it uses desktop autostart instead.) If it isn't running, run: squintthru start"
fi
[[ ":$PATH:" == *":$BIN:"* ]] || echo "  (Add $BIN to your PATH to use the squintthru command.)"
if [[ $restart_steam == 1 ]]; then
  say "One last step: restart your Steam Frame once. After that, SquintThru starts by itself."
fi
