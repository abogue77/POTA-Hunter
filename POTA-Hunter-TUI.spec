# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['potahunter-tui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        '_curses',
        '_curses_panel',
        'curses',
        'curses.panel',
        'curses.textpad',
        'curses.ascii',
    ],
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
    a.binaries,
    a.datas,
    [],
    name='POTA-Hunter-TUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets\\icon.ico',
)
