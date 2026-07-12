"""Authoritative product identity for source and packaged runtimes."""

from __future__ import annotations

import json
import platform as stdlib_platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.app_paths import resolve_app_paths
from core.product_version import (
    BUNDLE_ID,
    PRODUCT_ID,
    ProductVersion,
    normalize_architecture,
    normalize_platform,
)


SOURCE_PRODUCT_VERSION: Final = ProductVersion.parse("0.3.1", 1)
BUILD_METADATA_NAME: Final = "product_build.json"
_MAX_METADATA_BYTES: Final = 4096


@dataclass(frozen=True, slots=True)
class RuntimeProductIdentity:
    product_version: ProductVersion
    platform: str
    architecture: str
    packaged: bool


def load_runtime_product_identity(
    *,
    resource_root: Path | None = None,
    frozen: bool | None = None,
    system: str | None = None,
    machine: str | None = None,
) -> RuntimeProductIdentity:
    packaged = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    root = (
        resolve_app_paths().resource_root
        if resource_root is None
        else Path(resource_root).resolve(strict=False)
    )
    target_platform = normalize_platform(
        stdlib_platform.system() if system is None else system
    )
    target_architecture = normalize_architecture(
        stdlib_platform.machine() if machine is None else machine
    )
    if target_platform == "unknown" or target_architecture == "unknown":
        raise RuntimeError("Runtime product target is unsupported.")

    metadata = root / BUILD_METADATA_NAME
    if not metadata.exists():
        if packaged:
            raise RuntimeError("Packaged product build metadata is missing.")
        version = SOURCE_PRODUCT_VERSION
    else:
        version = _read_build_metadata(metadata)
    return RuntimeProductIdentity(
        version,
        target_platform,
        target_architecture,
        packaged,
    )


def _read_build_metadata(path: Path) -> ProductVersion:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("metadata path is invalid")
        if not 1 <= path.stat().st_size <= _MAX_METADATA_BYTES:
            raise ValueError("metadata size is invalid")
        data = json.loads(path.read_text(encoding="utf-8"))
        if type(data) is not dict or frozenset(data) != {
            "product_id",
            "bundle_id",
            "version",
            "build",
        }:
            raise ValueError("metadata schema is invalid")
        if data["product_id"] != PRODUCT_ID or data["bundle_id"] != BUNDLE_ID:
            raise ValueError("metadata product identity is invalid")
        return ProductVersion.parse(data["version"], data["build"])
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Product build metadata is invalid.") from exc


__all__ = [
    "BUILD_METADATA_NAME",
    "RuntimeProductIdentity",
    "SOURCE_PRODUCT_VERSION",
    "load_runtime_product_identity",
]
