# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build: one self-contained csvopt executable.

    pyinstaller packaging/csvopt.spec            # dist/csvopt(.exe)
    pyinstaller packaging/csvopt.spec -- --onedir

The web UI files are data, not code, so they are collected explicitly; the
server looks for them under sys._MEIPASS when frozen.
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ONEDIR = "--onedir" in sys.argv

a = Analysis(
    [os.path.join(ROOT, "packaging", "csvopt_launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(ROOT, "csvopt", "web"), os.path.join("csvopt", "web"))],
    hiddenimports=["csvopt.parallel"],  # imported lazily by the filter
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "pydoc_data", "lib2to3"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEDIR:
    exe = EXE(
        pyz, a.scripts, [], exclude_binaries=True, name="csvopt",
        console=True, disable_windowed_traceback=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="csvopt")
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [], name="csvopt",
        console=True, upx=False, disable_windowed_traceback=False,
        onefile=True,
    )
