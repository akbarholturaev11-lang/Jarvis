"""Factory for platform-neutral release packaging adapters."""

from __future__ import annotations

from core.product_version import UNKNOWN_TARGET, normalize_platform

from .release_base import ReleasePlatformAdapter, UnavailableReleaseAdapter
from .release_linux import LinuxReleaseAdapter
from .release_macos import MacOSReleaseAdapter
from .release_windows import WindowsReleaseAdapter


def create_release_adapter(
    target_platform: object | None = None,
    *,
    host_platform: object | None = None,
) -> ReleasePlatformAdapter:
    normalized = normalize_platform(target_platform)
    if normalized == "macos":
        return MacOSReleaseAdapter(host_system=host_platform)
    if normalized == "windows":
        return WindowsReleaseAdapter()
    if normalized == "linux":
        return LinuxReleaseAdapter()
    return UnavailableReleaseAdapter(
        UNKNOWN_TARGET if normalized == UNKNOWN_TARGET else normalized
    )


__all__ = ["create_release_adapter"]
