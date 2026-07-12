"""Honest Windows release adapter placeholder."""

from __future__ import annotations

from .release_base import (
    ReleaseBuildPlan,
    ReleaseBuildRequest,
    ReleaseCapabilityStatus,
    ReleasePackageFormat,
    ReleasePlatformAdapter,
    UpdateInstallRequest,
    UpdateInstallResult,
)


class WindowsReleaseAdapter(ReleasePlatformAdapter):
    target_platform = "windows"
    package_format = ReleasePackageFormat.WINDOWS_INSTALLER

    def plan_build(self, request: ReleaseBuildRequest) -> ReleaseBuildPlan:
        resource_root = self._resource_root(request)
        workspace = (
            request.output_root
            / f"{request.version}+{request.build}"
            / f"windows-{request.architecture}"
        )
        return ReleaseBuildPlan(
            status=ReleaseCapabilityStatus.NOT_AVAILABLE,
            target_platform=self.target_platform,
            package_format=self.package_format,
            request=request,
            resource_root=resource_root,
            workspace_root=workspace,
            app_path=None,
            package_path=None,
            staging_root=None,
            commands=(),
            missing_requirements=("verified Windows installer toolchain",),
            message="Windows installer packaging is not available in this build.",
        )

    def install_update(self, request: UpdateInstallRequest) -> UpdateInstallResult:
        return self._unavailable_update()


__all__ = ["WindowsReleaseAdapter"]
