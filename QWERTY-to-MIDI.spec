# -*- mode: python ; coding: utf-8 -*-
import re
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct
)

# Single source of truth for the version: __version__ in the script
with open('QWERTY-to-MIDI.py', encoding='utf-8') as f:
    version = re.search(r'^__version__ = "([^"]+)"', f.read(), re.M).group(1)
version_tuple = tuple(int(part) for part in (version.split('.') + ['0'] * 4)[:4])

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple),
    kids=[
        StringFileInfo([StringTable('040904B0', [
            StringStruct('ProductName', 'QWERTY-to-MIDI'),
            StringStruct('FileDescription', 'QWERTY-to-MIDI'),
            StringStruct('FileVersion', version),
            StringStruct('ProductVersion', version),
            StringStruct('OriginalFilename', 'QWERTY-to-MIDI.exe'),
            StringStruct('LegalCopyright', 'GPL-3.0'),
        ])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])]),
    ],
)

datas = []
binaries = []
hiddenimports = ['mido.backends.rtmidi']
tmp_ret = collect_all('rtmidi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['QWERTY-to-MIDI.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
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
    name='QWERTY-to-MIDI',
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
    version=version_info,
)
