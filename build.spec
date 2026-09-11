# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for VidEditor.

    pyinstaller build.spec --noconfirm

ONEDIR, not onefile. A one-file build is one executable that unpacks its whole
payload into a temporary directory on every launch, and this payload includes
two FFmpeg binaries and the Qt libraries: it is slower to start every single
time in exchange for looking tidier once. A folder is what ships.

The two FFmpeg binaries are bundled as data rather than as binaries. They are
not linked against anything in the application and PyInstaller must not try to
analyse or rewrite them; they are files this program executes, exactly as the
vendored ``bin/`` directory holds them in a source checkout.
core.binaries resolves them under ``sys._MEIPASS`` first, which is where they
land here, so nothing in the application needs to know it is frozen.
"""

from pathlib import Path

# __file__ is not defined while a spec is being exec'd, so the project root is
# taken from the directory PyInstaller was started in.
ROOT = Path(SPECPATH)

# Qt modules this application never touches. Excluding them is worth roughly
# half the bundle: QtWebEngine alone is a browser. If a widget ever needs one
# of these, the symptom is an ImportError at startup naming the module, and
# the fix is to take it out of this list rather than to guess.
EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtQml",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtNetworkAuth",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtNfc",
    "PySide6.QtTest",
    "PySide6.QtSql",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtSerialPort",
    "PySide6.QtSensors",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSpatialAudio",
    "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebSockets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    # Not Qt, but pulled in by numpy's and Pillow's optional paths and never
    # used here.
    "tkinter",
    "matplotlib",
    "IPython",
    "pytest",
    "PyInstaller",
]

# Shipped alongside, not linked: see the module docstring.
DATAS = [
    (str(ROOT / "bin" / "ffmpeg.exe"), "bin"),
    (str(ROOT / "bin" / "ffprobe.exe"), "bin"),
    (str(ROOT / "assets" / "videditor.ico"), "assets"),
]

a = Analysis(
    ["app.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# Qt libraries the PySide6 hook copies whether or not anything imports them.
# Excluding a Python module stops PySide6.QtQuick being importable; it does not
# stop Qt6Quick.dll being copied, because the hook ships a base set of Qt
# libraries by name.
#
# Every entry here was checked against the built folder first: nothing outside
# this group links to any of them. Qt6Qml, Qt6QmlMeta, Qt6QmlModels and
# Qt6QmlWorkerScript are used only by Qt6Quick and Qt6VirtualKeyboard;
# Qt6VirtualKeyboard only by its own input-method plugin; Qt6Pdf only by the
# qpdf image format plugin. Qt6Multimedia, Qt6Widgets and the application
# reference none of it.
#
# opengl32sw.dll is deliberately NOT in this list. It is 20MB and nothing
# links to it, because Qt loads it by name at runtime when the machine has no
# usable OpenGL driver. That machine exists, and a video editor that will not
# start on it is worse than a folder 20MB larger.
PRUNE_BINARIES = {
    "qt6quick.dll",
    "qt6qml.dll",
    "qt6qmlmeta.dll",
    "qt6qmlmodels.dll",
    "qt6qmlworkerscript.dll",
    "qt6pdf.dll",
    "qt6virtualkeyboard.dll",
    "qpdf.dll",
    "qtvirtualkeyboardplugin.dll",
}

a.binaries = [
    entry for entry in a.binaries
    if Path(entry[0]).name.lower() not in PRUNE_BINARIES
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="videditor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console window. Every subprocess this application starts already
    # passes CREATE_NO_WINDOW, so nothing needs one.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "videditor.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="videditor",
)
