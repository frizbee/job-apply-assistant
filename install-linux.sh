#!/bin/bash
# Linux source installation: Python environment, dependencies, Chrome and desktop shortcut.
# Usage: ./install-linux.sh, then launch "Auto Apply" from the app menu or run ./run.sh.
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "This installer is for Linux. Use ./run.sh on Mac or run.bat on Windows."

# Find a package manager for missing system packages.
if command -v apt-get >/dev/null; then PM="sudo apt-get install -y"; PY_PKGS="python3 python3-venv python3-pip"
elif command -v dnf >/dev/null; then PM="sudo dnf install -y"; PY_PKGS="python3 python3-pip"
elif command -v pacman >/dev/null; then PM="sudo pacman -S --needed --noconfirm"; PY_PKGS="python python-pip"
elif command -v zypper >/dev/null; then PM="sudo zypper install -y"; PY_PKGS="python3 python3-pip"
else PM=""; fi

say "Checking Python"
if ! command -v python3 >/dev/null || ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  [ -n "$PM" ] || die "Python 3.10+ with venv is missing; install it using your distribution's package manager."
  command -v apt-get >/dev/null && sudo apt-get update
  $PM $PY_PKGS
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || die "Python 3.10 or later is required (found: $(python3 -V 2>&1))."

say "Installing the Python environment and dependencies"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check --upgrade pip
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

say "Checking Google Chrome"
if command -v google-chrome >/dev/null || command -v google-chrome-stable >/dev/null || [ -x /opt/google/chrome/chrome ] || command -v microsoft-edge-stable >/dev/null; then
  echo "Chrome/Edge is installed."
elif [ "$(uname -m)" = "x86_64" ] && command -v apt-get >/dev/null; then
  # Playwright installs official Chrome; this may request a sudo password.
  .venv/bin/python -m playwright install chrome
elif [ "$(uname -m)" = "x86_64" ] && command -v dnf >/dev/null; then
  sudo dnf install -y https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm
else
  die "Google Chrome is missing and cannot be installed automatically on this system (automatic installation supports x86_64 Debian/Ubuntu/Fedora).
Install it from https://www.google.com/chrome and run this installer again."
fi

say "Adding an application-menu shortcut"
chmod +x run.sh
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/autoapply.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Auto Apply
Comment=Remote job application assistant
Exec="$DIR/run.sh"
Path=$DIR
Icon=google-chrome
Terminal=true
Categories=Office;Network;
EOF
chmod +x "$APPS/autoapply.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true

say "Installation complete"
echo "To start: select \"Auto Apply\" in the application menu or run $DIR/run.sh"
echo "Terminal mode: $DIR/run.sh --terminal"
echo "Note: the GUI requires a graphical desktop. Sign in to LinkedIn in the Chrome window when prompted."
