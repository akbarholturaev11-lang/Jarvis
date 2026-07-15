"""Local build-manifest record for a JARVIS release artifact.

This is the *local* description of a freshly built artifact (its identity, size
and SHA-256), not the signed release envelope consumed by the updater.  The
signed, canonical :mod:`core.release_manifest` payload is produced by the release
server with the trusted Ed25519 key; this module never signs and never claims
distribution readiness.

The record reuses the shared product identity and version validation so a local
build cannot drift from the release contract.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.product_version import (
    BUNDLE_ID,
    PRODUCT_ID,
    PRODUCT_NAME,
    UNKNOWN_TARGET,
    ProductVersion,
    SemanticVersion,
    normalize_architecture,
    normalize_platform,
)

BUILD_MANIFEST_SCHEMA = "jarvis.local-build-manifest"
BUILD_MANIFEST_SCHEMA_VERSION = 1
PACKAGE_FORMAT_DMG = "dmg"

_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class BuildArtifactManifest:
    """A canonical, non-secret description of one locally built artifact."""

    product_id: str
    product_name: str
    bundle_id: str
    version: SemanticVersion
    build: int
    platform: str
    architecture: str
    package_format: str
    artifact_name: str
    sha256: str
    byte_size: int
    signed: bool
    notarized: bool

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": BUILD_MANIFEST_SCHEMA,
            "schema_version": BUILD_MANIFEST_SCHEMA_VERSION,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "bundle_id": self.bundle_id,
            "version": str(self.version),
            "build": self.build,
            "platform": self.platform,
            "architecture": self.architecture,
            "package_format": self.package_format,
            "artifact_name": self.artifact_name,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "signed": self.signed,
            "notarized": self.notarized,
            "distribution_ready": False,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_document(), indent=2, sort_keys=True) + "\n"


def sha256_file(path: Path) -> str:
    """Stream a file's lowercase hex SHA-256 without loading it into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_artifact_manifest(
    *,
    artifact_path: Path,
    version: str | SemanticVersion,
    build: int,
    platform: object,
    architecture: object,
    package_format: str = PACKAGE_FORMAT_DMG,
    signed: bool = False,
    notarized: bool = False,
) -> BuildArtifactManifest:
    """Describe *artifact_path* using the shared release identity contract."""

    artifact = Path(artifact_path)
    if not artifact.is_file():
        raise ValueError("artifact does not exist")
    byte_size = artifact.stat().st_size
    if byte_size <= 0:
        raise ValueError("artifact is empty")

    parsed_version = (
        version if isinstance(version, SemanticVersion) else SemanticVersion.parse(version)
    )
    # Reuse the release build-number/version validation.
    product_version = ProductVersion(parsed_version, build)

    normalized_platform = normalize_platform(platform)
    if normalized_platform == UNKNOWN_TARGET:
        raise ValueError("unsupported build platform")
    normalized_architecture = normalize_architecture(architecture)
    if normalized_architecture == UNKNOWN_TARGET:
        raise ValueError("unsupported build architecture")
    if not isinstance(package_format, str) or not package_format:
        raise ValueError("package_format is required")
    if notarized and not signed:
        raise ValueError("a notarized artifact must also be signed")

    return BuildArtifactManifest(
        product_id=PRODUCT_ID,
        product_name=PRODUCT_NAME,
        bundle_id=BUNDLE_ID,
        version=product_version.version,
        build=product_version.build,
        platform=normalized_platform,
        architecture=normalized_architecture,
        package_format=package_format,
        artifact_name=artifact.name,
        sha256=sha256_file(artifact),
        byte_size=byte_size,
        signed=bool(signed),
        notarized=bool(notarized),
    )


def write_build_manifest(manifest: BuildArtifactManifest, destination: Path) -> Path:
    """Write *manifest* as pretty canonical JSON and return its path."""

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(manifest.to_json(), encoding="utf-8")
    return target


def load_build_manifest_document(path: Path) -> Mapping[str, Any]:
    """Read back a build manifest document (used by verification tooling)."""

    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("build manifest must be a JSON object")
    return document


__all__ = [
    "BUILD_MANIFEST_SCHEMA",
    "BUILD_MANIFEST_SCHEMA_VERSION",
    "PACKAGE_FORMAT_DMG",
    "BuildArtifactManifest",
    "build_artifact_manifest",
    "load_build_manifest_document",
    "sha256_file",
    "write_build_manifest",
]
