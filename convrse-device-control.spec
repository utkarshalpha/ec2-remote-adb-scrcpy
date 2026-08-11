# -*- mode: python ; coding: utf-8 -*-
"""Production Windows build for Convrse Device Control.

This replaces the one-file build used up to V2.3.4.

That build packed the interpreter, PySide6, adb.exe, scrcpy.exe and the FFmpeg
and OpenSSL DLLs into a single unsigned executable.  Every launch unpacked
about 123 MB into %TEMP%\\_MEI<pid> and executed from there, and the folders
were frequently left behind -- 287 MB of them had accumulated on the reference
machine.  Writing that many executables and crypto libraries into a temporary
directory and running them is behaviourally indistinguishable from a dropper,
which is why Windows Defender scored it as a threat, and re-scanning all of it
on a cold machine is what made startup appear to hang.

A one-folder build removes the mechanism entirely: the files sit on disk where
they were installed, Defender scans them once at install time, and startup does
no extraction at all.  Nothing here weakens or evades any security control --
the application simply stops behaving like something worth flagging.
"""

import os
from pathlib import Path


project_root = Path(SPECPATH)

# Windows can keep a handle on the previous output folder -- Defender, the
# search indexer and Explorer preview all do it -- long after the app exits.
# The release script stages into a fresh name when that happens, since
# PyInstaller cannot be told a folder name on the command line alongside a spec.
dist_name = os.environ.get("CDC_DIST_NAME", "ConvrseDeviceControl")
runtime_root = project_root / "scrcpy-runtime"

required_runtime_files = ("scrcpy.exe", "scrcpy-server", "adb.exe")
missing = [name for name in required_runtime_files
           if not (runtime_root / name).is_file()]
if missing:
    raise SystemExit(
        "The Windows scrcpy runtime is incomplete. Place it in "
        f"{runtime_root} (missing: {', '.join(missing)})."
    )

# The scrcpy runtime ships as ordinary files beside the executable rather than
# being embedded.  adb.exe and scrcpy.exe keep their own identity and reputation
# on disk, instead of appearing at a fresh temporary path on every run.
runtime_datas = []
for source in sorted(path for path in runtime_root.rglob("*") if path.is_file()):
    destination = str(Path("scrcpy-runtime") / source.relative_to(runtime_root).parent)
    runtime_datas.append((str(source), destination))


analysis = Analysis(
    ["cdc_v2.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets/convrse-logo.png", "assets"),
        *runtime_datas,
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Tcl/Tk is only referenced by the retired Tk shell, which scrcpy_remote
    # already guards with an ImportError fallback.
    excludes=["tkinter", "_tkinter", "test", "unittest", "pydoc_data"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,          # one-folder: nothing is embedded
    name="Convrse Device Control",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-compressed sections are themselves a common malware heuristic and buy
    # nothing once the payload is no longer inside the executable.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/convrse-logo.ico",
    version="version_info.txt",
    uac_admin=False,                # the app never needs elevation
)

COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=dist_name,
)
