# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

project_dir = Path(SPECPATH)
datas = []

# Configuration and default parameter files are created by the app at runtime.
# Bundle only the window icon as a data file; the executable icon is set below.
for relative_path in ("icon.ico",):
    source = project_dir / relative_path
    if source.is_dir():
        for file_path in source.rglob("*"):
            if file_path.is_file():
                target_dir = Path(relative_path) / file_path.relative_to(source).parent
                datas.append((str(file_path), str(target_dir)))
    elif source.is_file():
        datas.append((str(source), "."))

a = Analysis(
    [str(project_dir / "main.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Source Compile Tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project_dir / "icon.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Source Compile Tool",
)
