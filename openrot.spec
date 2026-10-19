# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['openrot/__main__.py'],
    pathex=['.'],
    binaries=[],
    # Package data read via Path(__file__).parent at import time
    # (openrot/core/opencode_tools.py) must be shipped next to the module.
    datas=[('openrot/core/data', 'openrot/core/data')],
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
    name='openrot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='openrot',
)