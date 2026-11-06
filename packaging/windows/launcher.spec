# -*- mode: python ; coding: utf-8 -*-
# PyInstaller (one-folder): GUI-Exe LogothermGateway.exe (Statusfenster, ohne Konsole).
# Aufruf aus dem Repo-Root:  pyinstaller packaging/windows/launcher.spec --noconfirm
# Ergebnis: dist/launcher/LogothermGateway.exe
from pathlib import Path

HERE = Path(SPECPATH)
ROOT = HERE.parents[1]

a = Analysis(
    [str(ROOT / "launcher" / "launcher.py")],
    pathex=[str(ROOT / "launcher")],
    binaries=[],
    datas=[],
    hiddenimports=["updater"],   # wird erst bei Bedarf importiert
    excludes=["yaml", "pytest", "can", "pymodbus", "uvicorn", "fastapi", "pydantic"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LogothermGateway",
    console=False,           # GUI-Exe
    debug=False,
    strip=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="launcher")
