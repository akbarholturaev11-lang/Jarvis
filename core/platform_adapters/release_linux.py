"""Honest Linux release adapter placeholder."""

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


class LinuxReleaseAdapter(ReleasePlatformAdapter):
    target_platform = "linux"
    package_format = ReleasePackageFormat.LINUX_PACKAGE

    def plan_build(self, request: ReleaseBuildRequest) -> ReleaseBuildPlan:
        resource_root = self._resource_root(request)
        workspace = (
            request.output_root
            / f"{request.version}+{request.build}"
            / f"linux-{request.architecture}"
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
            missing_requirements=("verified Linux package toolchain",),
            message="Linux package creation is not available in this build.",
        )

    def install_update(self, request: UpdateInstallRequest) -> UpdateInstallResult:
        return self._unavailable_update()


__all__ = ["LinuxReleaseAdapter"]
