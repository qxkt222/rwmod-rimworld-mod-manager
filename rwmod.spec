# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.building.datastruct import Tree


a = Analysis(
    ['src\\rwmod\\cli.py'],
    pathex=['src'],
    binaries=[],
    datas=[('static', 'static')],
    hiddenimports=[
        # Web UI (started via `rwmod web` → uvicorn.run("rwmod.server:app"))
        'rwmod.server',
        'uvicorn',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.wsproto_impl',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        # Core modules
        'rwmod.save_parser',
        'rwmod.tags',
        'rwmod.utils',
        'rwmod.rimpy_db',
        'rwmod.cache_db',
        'rwmod.i18n',
        'rwmod.mod_cache',
        'rwmod.compatibility',
        'rwmod.load_order',
        'rwmod.profile',
        'rwmod.offline',
        'rwmod.skymods',
        'rwmod.app_state',
        'rwmod.metadata',
        'rwmod.routers.saves',
        'rwmod.routers.tags',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Bundle the steamcmd *program* only — exclude the steamapps/ download cache
# (downloaded mods, user data) and userdata/ to keep the EXE small (~130MB
# instead of ~890MB).
a.datas += Tree('steamcmd', prefix='steamcmd', excludes=['steamapps', 'userdata'])

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='rwmod',
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
)
