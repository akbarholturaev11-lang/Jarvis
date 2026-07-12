"""Deterministic resource and writable-data paths for packaged JARVIS apps."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from core.product_version import PRODUCT_NAME, normalize_platform


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Immutable path layout; directory creation is always explicit."""

    platform: str
    resource_root: Path
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path
    update_staging_dir: Path

    @property
    def writable_directories(self) -> tuple[Path, ...]:
        return (
            self.config_dir,
            self.data_dir,
            self.cache_dir,
            self.log_dir,
            self.update_staging_dir,
        )

    def ensure(self) -> AppPaths:
        """Create writable directories without modifying the resource bundle."""

        for directory in self.writable_directories:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self


def _configured_path(
    environ: Mapping[str, str],
    variable: str,
    fallback: Path,
    *,
    allow_windows_absolute: bool = False,
) -> Path:
    value = environ.get(variable)
    if isinstance(value, str) and value.strip():
        cleaned = value.strip()
        candidate = Path(cleaned)
        if candidate.is_absolute() or (
            allow_windows_absolute and PureWindowsPath(cleaned).is_absolute()
        ):
            return candidate
    return fallback


def _resource_root(
    *,
    platform_name: str,
    resource_root: str | os.PathLike[str] | None,
    source_file: str | os.PathLike[str] | None,
    executable: str | os.PathLike[str] | None,
    frozen: bool | None,
    bundle_root: str | os.PathLike[str] | None,
) -> Path:
    if resource_root is not None:
        return Path(resource_root).resolve(strict=False)

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not is_frozen:
        module_path = Path(__file__ if source_file is None else source_file)
        return module_path.resolve(strict=False).parent.parent

    if bundle_root is not None:
        return Path(bundle_root).resolve(strict=False)

    executable_path = Path(sys.executable if executable is None else executable)
    executable_path = executable_path.resolve(strict=False)
    if (
        platform_name == "macos"
        and executable_path.parent.name == "MacOS"
        and executable_path.parent.parent.name == "Contents"
    ):
        return executable_path.parent.parent / "Resources"

    extraction_root = getattr(sys, "_MEIPASS", None)
    if isinstance(extraction_root, (str, os.PathLike)):
        return Path(extraction_root).resolve(strict=False)
    return executable_path.parent


def resolve_app_paths(
    *,
    platform_name: object | None = None,
    home: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    resource_root: str | os.PathLike[str] | None = None,
    source_file: str | os.PathLike[str] | None = None,
    executable: str | os.PathLike[str] | None = None,
    frozen: bool | None = None,
    bundle_root: str | os.PathLike[str] | None = None,
) -> AppPaths:
    """Resolve a platform-neutral app layout without touching the filesystem."""

    platform_key = normalize_platform(platform_name)
    if platform_key == "unknown":
        raise ValueError("unsupported platform")

    home_path = Path.home() if home is None else Path(home)
    environment = os.environ if environ is None else environ
    resources = _resource_root(
        platform_name=platform_key,
        resource_root=resource_root,
        source_file=source_file,
        executable=executable,
        frozen=frozen,
        bundle_root=bundle_root,
    )

    if platform_key == "macos":
        support_root = home_path / "Library" / "Application Support" / PRODUCT_NAME
        config_dir = support_root / "config"
        data_dir = support_root / "data"
        cache_dir = home_path / "Library" / "Caches" / PRODUCT_NAME
        log_dir = home_path / "Library" / "Logs" / PRODUCT_NAME
    elif platform_key == "windows":
        roaming_root = _configured_path(
            environment,
            "APPDATA",
            home_path / "AppData" / "Roaming",
            allow_windows_absolute=True,
        )
        local_root = _configured_path(
            environment,
            "LOCALAPPDATA",
            home_path / "AppData" / "Local",
            allow_windows_absolute=True,
        )
        config_dir = roaming_root / PRODUCT_NAME / "config"
        data_dir = local_root / PRODUCT_NAME / "data"
        cache_dir = local_root / PRODUCT_NAME / "cache"
        log_dir = local_root / PRODUCT_NAME / "logs"
    else:
        config_root = _configured_path(
            environment,
            "XDG_CONFIG_HOME",
            home_path / ".config",
        )
        data_root = _configured_path(
            environment,
            "XDG_DATA_HOME",
            home_path / ".local" / "share",
        )
        cache_root = _configured_path(
            environment,
            "XDG_CACHE_HOME",
            home_path / ".cache",
        )
        state_root = _configured_path(
            environment,
            "XDG_STATE_HOME",
            home_path / ".local" / "state",
        )
        config_dir = config_root / PRODUCT_NAME
        data_dir = data_root / PRODUCT_NAME
        cache_dir = cache_root / PRODUCT_NAME
        log_dir = state_root / PRODUCT_NAME / "logs"

    return AppPaths(
        platform=platform_key,
        resource_root=resources,
        config_dir=config_dir,
        data_dir=data_dir,
        cache_dir=cache_dir,
        log_dir=log_dir,
        update_staging_dir=cache_dir / "updates" / "staging",
    )
