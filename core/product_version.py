"""Platform-neutral product and release identity primitives.

This module intentionally does not declare a current release.  A concrete app
version belongs to the future signed packaging manifest, not to this foundation
layer.
"""

from __future__ import annotations

import platform as stdlib_platform
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


PRODUCT_ID = "jarvis"
PRODUCT_NAME = "JARVIS"
BUNDLE_ID = "com.jarvis.assistant"

UNKNOWN_TARGET = "unknown"
SUPPORTED_PLATFORMS = frozenset({"macos", "windows", "linux"})
SUPPORTED_ARCHITECTURES = frozenset(
    {"x86", "x86_64", "armv7", "arm64", "universal2"}
)

_SEMVER_RE = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
)

_PLATFORM_ALIASES = {
    "darwin": "macos",
    "mac": "macos",
    "macos": "macos",
    "osx": "macos",
    "win": "windows",
    "win32": "windows",
    "windows": "windows",
    "linux": "linux",
    "linux2": "linux",
}

_ARCHITECTURE_ALIASES = {
    "x86": "x86",
    "i386": "x86",
    "i486": "x86",
    "i586": "x86",
    "i686": "x86",
    "amd64": "x86_64",
    "x64": "x86_64",
    "x86-64": "x86_64",
    "x86_64": "x86_64",
    "armv7": "armv7",
    "armv7l": "armv7",
    "aarch64": "arm64",
    "arm64": "arm64",
    "universal": "universal2",
    "universal2": "universal2",
}


@dataclass(frozen=True, order=True, slots=True)
class SemanticVersion:
    """Strict ``MAJOR.MINOR.PATCH`` semantic version."""

    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        for field_name in ("major", "minor", "patch"):
            value = getattr(self, field_name)
            if type(value) is not int:
                raise TypeError(f"{field_name} must be an integer")
            if value < 0:
                raise ValueError(f"{field_name} must not be negative")

    @classmethod
    def parse(cls, value: str) -> SemanticVersion:
        if not isinstance(value, str):
            raise TypeError("version must be a string")
        match = _SEMVER_RE.fullmatch(value)
        if match is None:
            raise ValueError("version must use strict MAJOR.MINOR.PATCH format")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
        )

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True, slots=True)
class ProductVersion:
    """A semantic version paired with a positive packaging build number."""

    version: SemanticVersion
    build: int

    def __post_init__(self) -> None:
        if not isinstance(self.version, SemanticVersion):
            raise TypeError("version must be a SemanticVersion")
        _validate_build_number(self.build)

    @classmethod
    def parse(cls, version: str, build: int) -> ProductVersion:
        return cls(version=SemanticVersion.parse(version), build=build)

    def is_newer_than(self, other: ProductVersion) -> bool:
        """Compare releases using the target-stream monotonic-build policy.

        Within one platform/architecture update stream, a candidate is newer
        only when its build increases and its semantic version does not move
        backwards.  This intentionally matches
        :func:`require_monotonic_upgrade` so discovery and installation cannot
        disagree about the same artifact.
        """

        if not isinstance(other, ProductVersion):
            raise TypeError("other must be a ProductVersion")
        return self.build > other.build and self.version >= other.version


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    """Validated identity fields from a release package manifest."""

    product_id: str
    bundle_id: str
    version: SemanticVersion
    build: int
    platform: str
    architecture: str

    def __post_init__(self) -> None:
        if self.product_id != PRODUCT_ID:
            raise ValueError("release product_id does not match this product")
        if self.bundle_id != BUNDLE_ID:
            raise ValueError("release bundle_id does not match this product")
        if not isinstance(self.version, SemanticVersion):
            raise TypeError("version must be a SemanticVersion")
        _validate_build_number(self.build)
        if self.platform not in SUPPORTED_PLATFORMS:
            raise ValueError("release platform is not supported")
        if self.architecture not in SUPPORTED_ARCHITECTURES:
            raise ValueError("release architecture is not supported")

    @property
    def product_version(self) -> ProductVersion:
        return ProductVersion(version=self.version, build=self.build)

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> ReleaseIdentity:
        return validate_release_manifest(manifest)


def _validate_build_number(value: object) -> None:
    if type(value) is not int:
        raise TypeError("build must be an integer")
    if value <= 0:
        raise ValueError("build must be positive")


def require_monotonic_upgrade(
    previous: ProductVersion,
    candidate: ProductVersion,
) -> ProductVersion:
    """Return *candidate* only when version and build cannot move backwards."""

    if not isinstance(previous, ProductVersion):
        raise TypeError("previous must be a ProductVersion")
    if not isinstance(candidate, ProductVersion):
        raise TypeError("candidate must be a ProductVersion")
    if candidate.build <= previous.build:
        raise ValueError("candidate build must be greater than the previous build")
    if candidate.version < previous.version:
        raise ValueError("candidate semantic version must not move backwards")
    return candidate


def normalize_platform(value: object | None = None) -> str:
    """Normalize a platform name to ``macos``, ``windows``, or ``linux``."""

    raw = stdlib_platform.system() if value is None else value
    if not isinstance(raw, str):
        return UNKNOWN_TARGET
    return _PLATFORM_ALIASES.get(raw.strip().casefold(), UNKNOWN_TARGET)


def normalize_architecture(value: object | None = None) -> str:
    """Normalize common machine architecture aliases for release matching."""

    raw = stdlib_platform.machine() if value is None else value
    if not isinstance(raw, str):
        return UNKNOWN_TARGET
    return _ARCHITECTURE_ALIASES.get(raw.strip().casefold(), UNKNOWN_TARGET)


def validate_release_manifest(manifest: Mapping[str, Any]) -> ReleaseIdentity:
    """Validate and normalize the identity portion of a release manifest.

    Extra manifest fields are deliberately allowed so signing, hashes, release
    notes, and pricing can be layered on later without weakening identity checks.
    """

    if not isinstance(manifest, Mapping):
        raise TypeError("release manifest must be a mapping")

    required = (
        "product_id",
        "bundle_id",
        "version",
        "build",
        "platform",
        "architecture",
    )
    missing = [field for field in required if field not in manifest]
    if missing:
        raise ValueError(f"release manifest is missing: {', '.join(missing)}")

    product_id = manifest["product_id"]
    bundle_id = manifest["bundle_id"]
    version = manifest["version"]
    platform_name = manifest["platform"]
    architecture = manifest["architecture"]
    if not isinstance(product_id, str):
        raise TypeError("product_id must be a string")
    if not isinstance(bundle_id, str):
        raise TypeError("bundle_id must be a string")
    if not isinstance(version, str):
        raise TypeError("version must be a string")

    return ReleaseIdentity(
        product_id=product_id,
        bundle_id=bundle_id,
        version=SemanticVersion.parse(version),
        build=manifest["build"],
        platform=normalize_platform(platform_name),
        architecture=normalize_architecture(architecture),
    )
