# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from PyInstaller.utils.hooks import collect_submodules

project_root = os.path.abspath(SPECPATH)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Uvicorn loads the ASGI target from a string, so keep explicit anchors in
# addition to collecting the complete renamed packages.
hiddenimports = (
    ['flow_server.api', 'flow_server.mcp_server']
    + collect_submodules('flow_server')
    + collect_submodules('flow_engine')
)

a = Analysis(
    [os.path.join(project_root, 'main.py')],
    pathex=[project_root],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
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
    name='flow',
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
