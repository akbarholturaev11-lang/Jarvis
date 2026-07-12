"""macOS-first local packaging plan for JARVIS.

This adapter only plans an unsigned local ``.app`` and DMG build.  It does not
sign, notarize, staple, publish, or install an update.
"""

from __future__ import annotations

import importlib.util
import platform
from collections.abc import Callable
from pathlib import Path

from core.product_version import normalize_platform

from .release_base import (
    ReleaseBuildPlan,
    ReleaseBuildRequest,
    ReleaseCapabilityStatus,
    ReleaseCommand,
    ReleasePackageFormat,
    ReleasePlatformAdapter,
    UpdateInstallRequest,
    UpdateInstallResult,
)


class MacOSReleaseAdapter(ReleasePlatformAdapter):
    target_platform = "macos"
    package_format = ReleasePackageFormat.DMG
    supported_architectures = frozenset({"arm64", "x86_64", "universal2"})

    def __init__(
        self,
        *,
        host_system: object | None = None,
        which: Callable[[str], str | None] | None = None,
        module_available: Callable[[str], bool] | None = None,
    ) -> None:
        self._host_platform = normalize_platform(
            platform.system() if host_system is None else host_system
        )
        self._which = which or self._trusted_system_tool
        self._module_available = module_available or (
            lambda name: importlib.util.find_spec(name) is not None
        )

    @staticmethod
    def _trusted_system_tool(name: str) -> str | None:
        if name != "hdiutil":
            return None
        candidate = Path("/usr/bin/hdiutil")
        return str(candidate) if candidate.is_file() else None

    def plan_build(self, request: ReleaseBuildRequest) -> ReleaseBuildPlan:
        resource_root = self._resource_root(request)
        workspace = (
            request.output_root
            / f"{request.version}+{request.build}"
            / f"macos-{request.architecture}"
        )
        dist_root = workspace / "dist"
        work_root = workspace / "work"
        staging_root = workspace / "dmg-root"
        app_path = dist_root / "JARVIS.app"
        package_path = (
            workspace
            / (
                f"JARVIS-{request.version}-build{request.build}-"
                f"macos-{request.architecture}.dmg"
            )
        )
        spec_path = resource_root / "packaging" / "macos" / "Jarvis.spec"
        required_paths = {
            "main.py": resource_root / "main.py",
            "core/prompt.txt": resource_root / "core" / "prompt.txt",
            "config/settings.json": resource_root / "config" / "settings.json",
            "dashboard/static": resource_root / "dashboard" / "static",
            "PyInstaller spec": spec_path,
            "Python executable": request.python_executable,
        }
        missing: list[str] = []
        if self._host_platform != "macos":
            missing.append("macOS build host")
        if request.architecture not in self.supported_architectures:
            missing.append("supported macOS architecture")
        for label, path in required_paths.items():
            if label == "dashboard/static":
                present = path.is_dir()
            else:
                present = path.is_file()
            if not present:
                missing.append(label)
        if not self._module_available("PyInstaller"):
            missing.append("PyInstaller module")
        hdiutil = self._which("hdiutil")
        if not hdiutil or not Path(hdiutil).is_absolute():
            missing.append("hdiutil")

        commands: tuple[ReleaseCommand, ...] = ()
        if not missing and hdiutil is not None:
            commands = (
                ReleaseCommand(
                    name="build_app",
                    argv=(
                        str(request.python_executable),
                        "-m",
                        "PyInstaller",
                        "--noconfirm",
                        "--clean",
                        "--distpath",
                        str(dist_root),
                        "--workpath",
                        str(work_root),
                        str(spec_path),
                    ),
                    cwd=resource_root,
                ),
                ReleaseCommand(
                    name="create_dmg",
                    argv=(
                        hdiutil,
                        "create",
                        "-volname",
                        "JARVIS",
                        "-srcfolder",
                        str(staging_root),
                        "-ov",
                        "-format",
                        "UDZO",
                        str(package_path),
                    ),
                    cwd=resource_root,
                ),
            )

        status = (
            ReleaseCapabilityStatus.NOT_AVAILABLE
            if missing
            else ReleaseCapabilityStatus.AVAILABLE
        )
        return ReleaseBuildPlan(
            status=status,
            target_platform=self.target_platform,
            package_format=self.package_format,
            request=request,
            resource_root=resource_root,
            workspace_root=workspace,
            app_path=app_path,
            package_path=package_path,
            staging_root=staging_root,
            commands=commands,
            missing_requirements=tuple(missing),
            message=(
                "Unsigned local macOS packaging prerequisites are available."
                if not missing
                else "Unsigned local macOS packaging prerequisites are incomplete."
            ),
        )

    def install_update(self, request: UpdateInstallRequest) -> UpdateInstallResult:
        return self._unavailable_update()


__all__ = ["MacOSReleaseAdapter"]
