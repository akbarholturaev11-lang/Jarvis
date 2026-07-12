"""Platform-neutral release packaging and update-installation contracts.

The contracts in this module are intentionally side-effect free.  Adapters may
describe a build plan, but executing that plan belongs to an explicit release
tool.  Update installation remains unavailable until a platform implementation
can prove atomic replacement, health verification, and rollback.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from core.app_paths import resolve_app_paths
from core.product_version import (
    UNKNOWN_TARGET,
    ProductVersion,
    SemanticVersion,
    normalize_architecture,
    normalize_platform,
)
from core.update_transaction import VerifiedArtifactHandle


class ReleaseCapabilityStatus(StrEnum):
    SUCCESS = "success"
    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"
    INVALID = "invalid"
    FAILED = "failed"


class ReleasePackageFormat(StrEnum):
    DMG = "dmg"
    WINDOWS_INSTALLER = "windows_installer"
    LINUX_PACKAGE = "linux_package"
    UNKNOWN = "unknown"


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ReleaseBuildRequest:
    """Validated inputs shared by every platform release adapter."""

    project_root: Path
    output_root: Path
    version: SemanticVersion
    build: int
    architecture: str
    python_executable: Path

    def __post_init__(self) -> None:
        if not all(
            isinstance(path, Path) and path.is_absolute()
            for path in (self.project_root, self.output_root, self.python_executable)
        ):
            raise ValueError("release paths must be absolute Path values")
        if not isinstance(self.version, SemanticVersion):
            raise TypeError("release version must be a SemanticVersion")
        ProductVersion(self.version, self.build)
        if normalize_architecture(self.architecture) != self.architecture:
            raise ValueError("release architecture must be canonical")

    @classmethod
    def create(
        cls,
        *,
        project_root: str | Path,
        output_root: str | Path,
        version: str | SemanticVersion,
        build: int,
        architecture: object,
        python_executable: str | Path,
    ) -> ReleaseBuildRequest:
        parsed_version = (
            version if isinstance(version, SemanticVersion) else SemanticVersion.parse(version)
        )
        product_version = ProductVersion(parsed_version, build)
        normalized_architecture = normalize_architecture(architecture)
        if normalized_architecture == UNKNOWN_TARGET:
            raise ValueError("release architecture is not supported")
        return cls(
            project_root=Path(project_root).expanduser().resolve(strict=False),
            output_root=Path(output_root).expanduser().resolve(strict=False),
            version=product_version.version,
            build=product_version.build,
            architecture=normalized_architecture,
            python_executable=Path(python_executable).expanduser().resolve(strict=False),
        )

    @property
    def product_version(self) -> ProductVersion:
        return ProductVersion(self.version, self.build)


@dataclass(frozen=True, slots=True)
class ReleaseCommand:
    """One argv-only command in a release plan."""

    name: str
    argv: tuple[str, ...]
    cwd: Path

    def __post_init__(self) -> None:
        if not self.name or not self.argv or any(not item for item in self.argv):
            raise ValueError("release command is invalid")
        if not self.cwd.is_absolute():
            raise ValueError("release command working directory must be absolute")


@dataclass(frozen=True, slots=True)
class ReleaseBuildPlan:
    """A truthful, non-executing packaging assessment."""

    status: ReleaseCapabilityStatus
    target_platform: str
    package_format: ReleasePackageFormat
    request: ReleaseBuildRequest
    resource_root: Path
    workspace_root: Path
    app_path: Path | None
    package_path: Path | None
    staging_root: Path | None
    commands: tuple[ReleaseCommand, ...]
    missing_requirements: tuple[str, ...]
    message: str

    @property
    def executable(self) -> bool:
        return self.status is ReleaseCapabilityStatus.AVAILABLE and bool(self.commands)


@dataclass(frozen=True, slots=True)
class UpdateInstallRequest:
    """Verified metadata plus a pinned package handle for platform installation.

    ``staged_artifact`` is valid only while its verification context remains
    open.  Platform adapters must use it to copy the package into their own
    inaccessible or privileged temporary descriptor, independently verify that
    copied digest and size, and mutate only from that copy.  They must never
    reopen a staged package pathname.
    """

    platform: str
    architecture: str
    staged_artifact: VerifiedArtifactHandle
    installed_app: Path
    backup_root: Path
    expected_version: SemanticVersion
    expected_build: int
    expected_sha256: str
    expected_byte_size: int

    def __post_init__(self) -> None:
        if normalize_platform(self.platform) != self.platform:
            raise ValueError("update platform must be canonical")
        if normalize_architecture(self.architecture) != self.architecture:
            raise ValueError("update architecture must be canonical")
        if not isinstance(self.staged_artifact, VerifiedArtifactHandle):
            raise TypeError("update artifact must be a verified pinned handle")
        if self.staged_artifact.closed:
            raise ValueError("verified update artifact is closed")
        if not all(
            isinstance(path, Path) and path.is_absolute()
            for path in (self.installed_app, self.backup_root)
        ):
            raise ValueError("update paths must be absolute Path values")
        if not isinstance(self.expected_version, SemanticVersion):
            raise TypeError("expected update version must be a SemanticVersion")
        ProductVersion(self.expected_version, self.expected_build)
        if (
            type(self.expected_sha256) is not str
            or _SHA256_RE.fullmatch(self.expected_sha256) is None
        ):
            raise ValueError("update digest must be lowercase SHA-256")
        if type(self.expected_byte_size) is not int or self.expected_byte_size <= 0:
            raise ValueError("update byte size must be positive")
        if (
            self.staged_artifact.sha256 != self.expected_sha256
            or self.staged_artifact.byte_size != self.expected_byte_size
        ):
            raise ValueError("pinned update artifact does not match expected metadata")

    @classmethod
    def create(
        cls,
        *,
        platform: object,
        architecture: object,
        staged_artifact: VerifiedArtifactHandle,
        installed_app: str | Path,
        backup_root: str | Path,
        expected_version: str | SemanticVersion,
        expected_build: int,
        expected_sha256: str,
        expected_byte_size: int,
    ) -> UpdateInstallRequest:
        normalized_platform = normalize_platform(platform)
        normalized_architecture = normalize_architecture(architecture)
        if normalized_platform == UNKNOWN_TARGET:
            raise ValueError("update platform is not supported")
        if normalized_architecture == UNKNOWN_TARGET:
            raise ValueError("update architecture is not supported")
        if not isinstance(staged_artifact, VerifiedArtifactHandle):
            raise TypeError("update artifact must be a verified pinned handle")
        version = (
            expected_version
            if isinstance(expected_version, SemanticVersion)
            else SemanticVersion.parse(expected_version)
        )
        product_version = ProductVersion(version, expected_build)
        if type(expected_sha256) is not str or _SHA256_RE.fullmatch(expected_sha256) is None:
            raise ValueError("update digest must be lowercase SHA-256")
        if type(expected_byte_size) is not int or expected_byte_size <= 0:
            raise ValueError("update byte size must be positive")
        paths = tuple(
            Path(value).expanduser().resolve(strict=False)
            for value in (installed_app, backup_root)
        )
        return cls(
            platform=normalized_platform,
            architecture=normalized_architecture,
            staged_artifact=staged_artifact,
            installed_app=paths[0],
            backup_root=paths[1],
            expected_version=product_version.version,
            expected_build=product_version.build,
            expected_sha256=expected_sha256,
            expected_byte_size=expected_byte_size,
        )


@dataclass(frozen=True, slots=True)
class UpdateInstallResult:
    """Updater result; success would require mechanical post-install proof."""

    status: ReleaseCapabilityStatus
    platform: str
    verified: bool
    message: str

    def __post_init__(self) -> None:
        if (self.status is ReleaseCapabilityStatus.SUCCESS) != self.verified:
            raise ValueError("only verified installation may report success")


class ReleasePlatformAdapter(ABC):
    target_platform = "unknown"
    package_format = ReleasePackageFormat.UNKNOWN

    @abstractmethod
    def plan_build(self, request: ReleaseBuildRequest) -> ReleaseBuildPlan:
        raise NotImplementedError

    @abstractmethod
    def install_update(self, request: UpdateInstallRequest) -> UpdateInstallResult:
        """Copy+verify the pinned artifact privately before any synchronous mutation."""

        raise NotImplementedError

    def _resource_root(self, request: ReleaseBuildRequest) -> Path:
        """Resolve source resources through the same AppPaths contract as runtime."""

        return resolve_app_paths(
            platform_name=self.target_platform,
            home=request.project_root / ".release-home-unused",
            environ={},
            resource_root=request.project_root,
            frozen=False,
        ).resource_root

    def _unavailable_update(self) -> UpdateInstallResult:
        return UpdateInstallResult(
            status=ReleaseCapabilityStatus.NOT_AVAILABLE,
            platform=self.target_platform,
            verified=False,
            message=(
                "Atomic update installation and rollback are not available "
                "for this platform in this build."
            ),
        )


class UnavailableReleaseAdapter(ReleasePlatformAdapter):
    """Honest adapter for unknown or not-yet-implemented targets."""

    def __init__(self, target_platform: str = "unknown") -> None:
        self.target_platform = target_platform

    def plan_build(self, request: ReleaseBuildRequest) -> ReleaseBuildPlan:
        resource_root = request.project_root
        workspace = request.output_root / f"{request.version}+{request.build}"
        return ReleaseBuildPlan(
            status=ReleaseCapabilityStatus.NOT_AVAILABLE,
            target_platform=self.target_platform,
            package_format=ReleasePackageFormat.UNKNOWN,
            request=request,
            resource_root=resource_root,
            workspace_root=workspace,
            app_path=None,
            package_path=None,
            staging_root=None,
            commands=(),
            missing_requirements=("platform release adapter",),
            message="Release packaging is not available for this platform.",
        )

    def install_update(self, request: UpdateInstallRequest) -> UpdateInstallResult:
        return self._unavailable_update()


__all__ = [
    "ReleaseBuildPlan",
    "ReleaseBuildRequest",
    "ReleaseCapabilityStatus",
    "ReleaseCommand",
    "ReleasePackageFormat",
    "ReleasePlatformAdapter",
    "UnavailableReleaseAdapter",
    "UpdateInstallRequest",
    "UpdateInstallResult",
]
