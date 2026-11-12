# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller description for the EMA Lightning Desktop GUI.

Build it with ``scripts/build_exe.ps1``. Set ``EMA_ONEFILE=1`` for a single
executable instead of the default folder build. Set ``EMA_WITH_WEIGHTS=1`` only
when a ``weights`` folder already exists; the build script handles both.
"""
import os
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent
ONEFILE = os.environ.get("EMA_ONEFILE") == "1"
NAME = "EMA-Lightning-Desktop-GUI"

version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
numbers = [int(part) for part in version.split(".") if part.isdigit()][:4]
parts = tuple(numbers + [0] * (4 - len(numbers)))
version_file = ROOT / "build" / "version_info.txt"
version_file.parent.mkdir(parents=True, exist_ok=True)
version_file.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={parts}, prodvers={parts}, mask=0x3f, flags=0x0, OS=0x40004,
    fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'EMA Lightning'),
        StringStruct('FileDescription', 'EMA Lightning Desktop GUI - Turkish text to speech'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', '{NAME}'),
        StringStruct('LegalCopyright', 'Apache-2.0'),
        StringStruct('OriginalFilename', '{NAME}.exe'),
        StringStruct('ProductName', 'EMA Lightning Desktop GUI'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
    encoding="utf-8",
)

datas = []
binaries = []
hiddenimports = [
    "ema_lightning",
    "ema_lightning.api",
    "ema_lightning.audio",
    "ema_lightning.chunker",
    "ema_lightning.decoder",
    "ema_lightning.engine",
    "ema_lightning.frontend",
    "ema_lightning.graphs",
    "ema_lightning.gui",
    "ema_lightning.model",
    "ema_lightning.scheduler",
    "ema_lightning.voice",
    "huggingface_hub",
    "normalizer_tr",
    "torch",
]

normalizer_datas, normalizer_binaries, normalizer_hidden = collect_all("normalizer_tr")
datas += normalizer_datas
binaries += normalizer_binaries
hiddenimports += normalizer_hidden

assets = ROOT / "src" / "ema_lightning" / "assets"
if assets.is_dir():
    datas.append((str(assets), "assets"))

weights = ROOT / "weights"
if weights.is_dir():
    datas.append((str(weights), "weights"))

icon = assets / "ema-lightning.ico"

a = Analysis(
    [str(ROOT / "packaging" / "gui_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "PyQt5",
        "PySide6",
        "jupyter",
        "matplotlib",
        "notebook",
        "pandas",
        "pytest",
        "scipy",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

common = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon) if icon.is_file() else None,
    version=str(version_file),
)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
