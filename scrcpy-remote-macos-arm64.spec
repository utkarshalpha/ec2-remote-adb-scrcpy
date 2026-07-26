# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path


project_root = Path(SPECPATH)
runtime_root = project_root / "scrcpy-runtime-macos-arm64"
icon_path = project_root / "assets" / "convrse-logo.icns"
entitlements_path = project_root / "entitlements-macos.plist"

required_runtime_files = ("scrcpy", "scrcpy-server", "adb")
missing_runtime_files = [
    name for name in required_runtime_files if not (runtime_root / name).is_file()
]
if missing_runtime_files:
    raise SystemExit(
        "The Apple Silicon scrcpy runtime is incomplete. Run "
        "./build-macos-arm64.sh to download and verify it. Missing: "
        + ", ".join(missing_runtime_files)
    )
if not icon_path.is_file():
    raise SystemExit(
        "The macOS icon is missing. Run ./build-macos-arm64.sh so iconutil can create it."
    )

# Mark the Mach-O tools as binaries so PyInstaller preserves executable mode
# and includes them in its macOS code-signing pass. Everything else remains a
# data file beside the portable scrcpy executable.
runtime_binaries = [
    (str(runtime_root / "scrcpy"), "scrcpy-runtime"),
    (str(runtime_root / "adb"), "scrcpy-runtime"),
]
runtime_datas = []
for source in sorted(path for path in runtime_root.rglob("*") if path.is_file()):
    if source.name in {"scrcpy", "adb"}:
        continue
    relative_parent = source.relative_to(runtime_root).parent
    destination = str(Path("scrcpy-runtime") / relative_parent)
    runtime_datas.append((str(source), destination))

codesign_identity = os.environ.get("CDC_CODESIGN_IDENTITY") or None

a = Analysis(
    ["cdc_macos.py"],
    pathex=[str(project_root)],
    binaries=runtime_binaries,
    datas=[("assets/convrse-logo.png", "assets"), *runtime_datas],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "_tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Convrse Device Control",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=codesign_identity,
    entitlements_file=str(entitlements_path),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Convrse Device Control",
)
app = BUNDLE(
    coll,
    name="Convrse Device Control.app",
    icon=str(icon_path),
    bundle_identifier="ai.convrse.device-control",
    version="2.4.0",
    info_plist={
        "CFBundleDisplayName": "Convrse Device Control",
        "CFBundleName": "Convrse Device Control",
        "CFBundleShortVersionString": "2.4.0",
        "CFBundleVersion": "2.4.0",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "LSMinimumSystemVersion": "12.0",
        "LSArchitecturePriority": ["arm64"],
        "NSHighResolutionCapable": True,
        "NSSupportsAutomaticGraphicsSwitching": True,
        "NSHumanReadableCopyright": "Copyright © 2026 Convrse",
    },
)
