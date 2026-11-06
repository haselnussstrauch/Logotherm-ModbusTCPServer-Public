# -*- mode: python ; coding: utf-8 -*-
# PyInstaller (one-folder): Konsolen-Exe gateway.exe.
# Aufruf aus dem Repo-Root:  pyinstaller packaging/windows/gateway.spec --noconfirm
# Ergebnis: dist/gateway/gateway.exe
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH)                    # packaging/windows
ROOT = HERE.parents[1]                   # Repo-Root

hiddenimports = [
    # python-can lädt Interfaces dynamisch über Entry-Points/Namen. socketcan ist Linux-only
    # und wird hier bewusst nicht gebündelt.
    "can.interfaces.gs_usb",
    "can.interfaces.pcan",
    "can.interfaces.virtual",
    "gs_usb",
    "gs_usb.gs_usb",
    "usb",
    "usb.backend.libusb1",
    "serial",
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
]
# uvicorn wählt Loop/Protokolle per Zeichenkette; pymodbus/fastapi/pydantic laden Teile lazy.
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("pymodbus")
hiddenimports += collect_submodules("gateway")
hiddenimports += ["yaml", "pydantic", "pydantic_core", "email.mime.text"]

datas = []
# Weboberfläche (HTML/JS/CSS) -> <exe-Ordner>/_internal/gateway/web/static
# Zugriff im Code über Path(__file__) (zeigt eingefroren nach _MEIPASS), siehe docs/windows.md.
static = ROOT / "gateway" / "web" / "static"
if static.is_dir():
    datas.append((str(static), "gateway/web/static"))
datas += collect_data_files("pymodbus", include_py_files=False)

binaries = []
# libusb für das gs_usb-Backend (packaging/windows/docker/build.sh legt sie nach third_party/).
libusb = HERE / "third_party" / "libusb-1.0.dll"
if libusb.is_file():
    binaries.append((str(libusb), "."))

a = Analysis(
    [str(HERE / "gateway_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "can.interfaces.socketcan"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="gateway",
    console=True,            # Konsolen-Exe: `gateway.exe scan` usw. geben Text aus
    debug=False,
    strip=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="gateway")
