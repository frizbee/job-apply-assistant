#!/bin/bash
# Linux kurulumu (kaynak koddan): Python ortamı, bağımlılıklar, Google Chrome ve uygulama menüsü kısayolu.
# Kullanım: ./install-linux.sh   → sonra menüden "Auto Apply" ya da ./run.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mHata: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "Bu betik Linux içindir. Mac'te ./run.sh, Windows'ta run.bat kullan."

# Paket yöneticisi (eksik sistem paketlerini kurmak için)
if command -v apt-get >/dev/null; then PM="sudo apt-get install -y"; PY_PKGS="python3 python3-venv python3-pip"
elif command -v dnf >/dev/null; then PM="sudo dnf install -y"; PY_PKGS="python3 python3-pip"
elif command -v pacman >/dev/null; then PM="sudo pacman -S --needed --noconfirm"; PY_PKGS="python python-pip"
elif command -v zypper >/dev/null; then PM="sudo zypper install -y"; PY_PKGS="python3 python3-pip"
else PM=""; fi

say "Python kontrol ediliyor"
if ! command -v python3 >/dev/null || ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  [ -n "$PM" ] || die "Python 3.10+ (venv ile) kurulu değil; dağıtımının paket yöneticisiyle kur."
  command -v apt-get >/dev/null && sudo apt-get update
  $PM $PY_PKGS
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || die "Python 3.10 veya üstü gerekli (bulunan: $(python3 -V 2>&1))."

say "Python ortamı ve bağımlılıklar kuruluyor"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check --upgrade pip
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

say "Google Chrome kontrol ediliyor"
if command -v google-chrome >/dev/null || command -v google-chrome-stable >/dev/null || [ -x /opt/google/chrome/chrome ] || command -v microsoft-edge-stable >/dev/null; then
  echo "Chrome/Edge kurulu."
elif [ "$(uname -m)" = "x86_64" ] && command -v apt-get >/dev/null; then
  # Playwright resmi Chrome'u indirip kurar (sudo şifresi sorabilir)
  .venv/bin/python -m playwright install chrome
elif [ "$(uname -m)" = "x86_64" ] && command -v dnf >/dev/null; then
  sudo dnf install -y https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm
else
  die "Google Chrome bulunamadı ve bu sistemde otomatik kurulamıyor (otomatik kurulum x86_64 Debian/Ubuntu/Fedora içindir).
https://www.google.com/chrome adresinden kurup betiği tekrar çalıştır."
fi

say "Uygulama menüsüne kısayol ekleniyor"
chmod +x run.sh
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/autoapply.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Auto Apply
Comment=Remote işlere otomatik başvuru
Exec="$DIR/run.sh"
Path=$DIR
Icon=google-chrome
Terminal=true
Categories=Office;Network;
EOF
chmod +x "$APPS/autoapply.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true

say "Kurulum tamam"
echo "Başlatmak için: uygulama menüsünden \"Auto Apply\" ya da  $DIR/run.sh"
echo "Terminal modu:  $DIR/run.sh --terminal"
echo "Not: Arayüz için grafik masaüstü gerekir (Chrome penceresi açılır, ilk seferde LinkedIn'e o pencereden giriş yaparsın)."
