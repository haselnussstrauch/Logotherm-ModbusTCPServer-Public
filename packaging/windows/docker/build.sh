#!/bin/sh
# Läuft im Build-Container (Stufe "build" in packaging/windows/docker/Dockerfile), Arbeitsordner /src.
# Baut Gateway-Exe, Launcher und Setup mit Windows-Python und Inno Setup unter Wine.
set -eu

PY='C:\Python\python.exe'
ISCC="$WINEPREFIX/drive_c/Program Files (x86)/Inno Setup 6/ISCC.exe"
THIRD=packaging/windows/third_party
mkdir -p "$THIRD"

w() { xvfb-run -a sh -c 'wine "$@"; rc=$?; wineserver -w; exit $rc' wine "$@"; }

# Fremdkomponenten: WinSW (aus dem Image) und libusb-1.0.dll (aus dem Paket libusb-package)
cp /tmp/WinSW-x64.exe "$THIRD/WinSW-x64.exe"
LIBUSB_WIN="$(w "$PY" -c "import libusb_package, pathlib; print(pathlib.Path(libusb_package.get_library_path()))" | tr -d '\r' | tail -1)"
LIBUSB="$(winepath -u "$LIBUSB_WIN")"
echo "libusb: $LIBUSB"
cp "$LIBUSB" "$THIRD/libusb-1.0.dll"
test -f packaging/windows/driver/wdi-simple.exe

# PyInstaller
rm -rf dist/gateway dist/launcher dist/installer
w "$PY" -m PyInstaller packaging/windows/gateway.spec --noconfirm --workpath build/gateway
w "$PY" -m PyInstaller packaging/windows/launcher.spec --noconfirm --workpath build/launcher
cp "$THIRD/libusb-1.0.dll" dist/gateway/libusb-1.0.dll

# Schnelltest der gebauten Exe
w dist/gateway/gateway.exe --version

# Installer
VERSION="$(w "$PY" -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])" | tr -d '\r' | tail -1)"
echo "Version: $VERSION"
w "$ISCC" "/DAppVersion=$VERSION" 'packaging\windows\setup.iss'
ls -l dist/installer
