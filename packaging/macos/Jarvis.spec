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

a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
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
    icon=None,
    bundle_identifier=BUNDLE_ID,
    target_arch=target_architecture,
    info_plist={
        "CFBundleDisplayName": "JARVIS",
        "CFBundleShortVersionString": release_version,
        "CFBundleVersion": build_number,
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
