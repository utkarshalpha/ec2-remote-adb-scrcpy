# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH)
runtime_root = project_root / "scrcpy-runtime"
required_runtime_files = ("scrcpy.exe", "scrcpy-server", "adb.exe")
missing_runtime_files = [
    name for name in required_runtime_files if not (runtime_root / name).is_file()
]
if missing_runtime_files:
    missing = ", ".join(missing_runtime_files)
    raise SystemExit(
        "V2.3 runtime is incomplete. Place the complete Windows scrcpy runtime "
        f"in {runtime_root} (missing: {missing})."
    )

runtime_binaries = []
runtime_datas = []
for source in sorted(path for path in runtime_root.rglob("*") if path.is_file()):
    relative_parent = source.relative_to(runtime_root).parent
    destination = str(Path("scrcpy-runtime") / relative_parent)
    item = (str(source), destination)
    if source.suffix.lower() in {".exe", ".dll"}:
        runtime_binaries.append(item)
    else:
        runtime_datas.append(item)


a = Analysis(
    ['cdc_v2.py'],
    pathex=[],
    binaries=runtime_binaries,
    datas=[('assets/convrse-logo.png', 'assets'), *runtime_datas],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', '_tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Convrse-Device-Control-V2.3',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Do not repack the upstream scrcpy/ADB executables and DLLs. Apart from
    # avoiding needless build work, this reduces runtime and antivirus issues.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/convrse-logo.ico',
    version='version_info_v2.3.txt',
)
