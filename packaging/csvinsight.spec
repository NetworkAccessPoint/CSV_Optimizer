# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build: one self-contained csvinsight executable.

    pyinstaller packaging/csvinsight.spec            # dist/csvinsight(.exe)
    pyinstaller packaging/csvinsight.spec -- --onedir

The web UI files are data, not code, so they are collected explicitly; the
server looks for them under sys._MEIPASS when frozen.
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ONEDIR = "--onedir" in sys.argv

# Windows wants .ico, macOS .icns; elsewhere the field is ignored. Built from
# packaging/icons/source.webp by tools/make_icons.py.
ICON = os.path.join(
    ROOT, "packaging", "icons",
    "csvinsight.ico" if sys.platform == "win32" else "csvinsight.icns",
)
if not os.path.exists(ICON):
    ICON = None

a = Analysis(
    [os.path.join(ROOT, "packaging", "csvinsight_launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(ROOT, "csvinsight", "web"), os.path.join("csvinsight", "web"))],
    hiddenimports=["csvinsight.parallel"],  # imported lazily by the filter
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "pydoc_data", "lib2to3"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEDIR:
    exe = EXE(
        pyz, a.scripts, [], exclude_binaries=True, name="csvinsight",
        console=True, disable_windowed_traceback=False, icon=ICON,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="csvinsight")
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [], name="csvinsight",
        console=True, upx=False, disable_windowed_traceback=False,
        onefile=True, icon=ICON,
    )
