# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for a local unsigned JARVIS.app build.

This spec intentionally includes only committed non-secret resources.  It does
not include local API keys, personal memory, device profiles, Zerno configuration,
TLS material, signing credentials, or update credentials.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


project_root = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(project_root))

from core.product_version import BUNDLE_ID  # noqa: E402

from PyInstaller.utils.hooks import (  # noqa: E402
    collect_data_files,
    collect_submodules,
)


release_version = os.environ.get("JARVIS_BUILD_VERSION", "")
build_number = os.environ.get("JARVIS_BUILD_NUMBER", "")
target_architecture = os.environ.get("JARVIS_TARGET_ARCH", "")
build_metadata = Path(os.environ.get("JARVIS_BUILD_METADATA", ""))
product_config = Path(os.environ.get("JARVIS_PRODUCT_CONFIG", ""))
if (
    not release_version
    or not build_number
    or not target_architecture
    or not build_metadata.is_absolute()
    or not build_metadata.is_file()
    or not product_config.is_absolute()
    or not product_config.is_file()
):
    raise RuntimeError(
        "JARVIS build identity is required; use scripts/build_macos_release.py"
    )

# Optional, development-only app icon (a rights-cleared icon is a branding gate).
_icon_env = os.environ.get("JARVIS_APP_ICON", "").strip()
app_icon = _icon_env if _icon_env and Path(_icon_env).is_file() else None

datas = [
    (str(build_metadata), "."),
    (str(product_config), "config"),
    (str(project_root / "core" / "prompt.txt"), "core"),
    (str(project_root / "config" / "settings.json"), "config"),
    (
        str(project_root / "config" / "device_profile.example.json"),
        "config",
    ),
    (
        str(project_root / "config" / "briefing_sources.example.json"),
        "config",
    ),
    (str(project_root / "dashboard" / "static"), "dashboard/static"),
]

# Bundle non-Python data shipped inside third-party packages (Google GenAI SDK,
# certifi trust store) so the frozen client is self-contained.
datas += collect_data_files("google.genai")
datas += collect_data_files("google.generativeai")
datas += collect_data_files("certifi")

# Hidden imports for code PyInstaller's static analysis can miss: the Google
# GenAI SDK, the FastAPI/uvicorn local server used by mobile remote control, and
# the update helper modules that make the frozen client self-updating.  PyQt6,
# cryptography, PIL, numpy and cv2 also get their own explicit safety net on top
# of PyInstaller's built-in hooks.
hidden_imports = [
    "PIL",
    "PIL.Image",
    "PIL.ImageQt",
    "cryptography",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    "cv2",
    "numpy",
    "sounddevice",
    "qrcode",
    "core.app_paths",
    # Updater helper: must ship so the frozen client can verify + roll back.
    "core.installer",
    "core.macos_update",
    "core.update_startup",
    "core.update_transaction",
    "core.product_updates",
]
hidden_imports += collect_submodules("google.genai")
hidden_imports += collect_submodules("google.generativeai")
hidden_imports += collect_submodules("uvicorn")
hidden_imports += collect_submodules("PyQt6")

a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JARVIS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=target_architecture,
    icon=app_icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="JARVIS",
)
app = BUNDLE(
    coll,
    name="JARVIS.app",
    icon=app_icon,
    bundle_identifier=BUNDLE_ID,
    target_arch=target_architecture,
    info_plist={
        "CFBundleDisplayName": "JARVIS",
        "CFBundleShortVersionString": release_version,
        "CFBundleVersion": build_number,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": (
            "JARVIS needs microphone access for voice commands. / "
            "JARVIS требуется доступ к микрофону для голосовых команд."
        ),
        "NSCameraUsageDescription": (
            "JARVIS needs camera access only when you request camera features. / "
            "JARVIS требуется доступ к камере только по вашему запросу."
        ),
        "NSAppleEventsUsageDescription": (
            "JARVIS needs automation access only for actions you request. / "
            "JARVIS требуется доступ к автоматизации только для запрошенных действий."
        ),
        "NSLocalNetworkUsageDescription": (
            "JARVIS uses the local network for explicitly enabled remote control. / "
            "JARVIS использует локальную сеть для явно включённого удалённого управления."
        ),
    },
)
