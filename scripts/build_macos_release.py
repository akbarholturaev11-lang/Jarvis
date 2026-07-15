#!/usr/bin/env python3
"""Plan or build an unsigned local JARVIS.app + DMG without installing tools.

The default mode is read-only and prints a prerequisite/build plan.  Actual
execution requires ``--execute-local-unsigned`` and still does not sign,
notarize, staple, publish, or claim distribution readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.platform_adapters.release_base import (  # noqa: E402
    ReleaseBuildPlan,
    ReleaseBuildRequest,
)
from core.platform_adapters.release_macos import MacOSReleaseAdapter  # noqa: E402
from core.product_config import load_product_client_config  # noqa: E402
from core.product_version import BUNDLE_ID, PRODUCT_ID  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or create an unsigned local JARVIS macOS artifact.",
    )
    parser.add_argument("--version", required=True, help="Strict MAJOR.MINOR.PATCH")
    parser.add_argument("--build", required=True, type=int, help="Positive global build")
    parser.add_argument(
        "--architecture",
        default=platform.machine(),
        help="arm64, x86_64, or universal2 when the dependency set supports it",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "build" / "local-release",
    )
    parser.add_argument(
        "--product-config",
        type=Path,
        help="Validated non-secret product.json containing API origin and public keys",
    )
    parser.add_argument(
        "--execute-local-unsigned",
        action="store_true",
        help="Run the plan and create a local unsigned DMG.",
    )
    return parser


def _plan_document(plan: ReleaseBuildPlan) -> dict[str, object]:
    return {
        "status": plan.status.value,
        "target_platform": plan.target_platform,
        "package_format": plan.package_format.value,
        "version": str(plan.request.version),
        "build": plan.request.build,
        "architecture": plan.request.architecture,
        "resource_root": str(plan.resource_root),
        "workspace_root": str(plan.workspace_root),
        "app_path": None if plan.app_path is None else str(plan.app_path),
        "package_path": None if plan.package_path is None else str(plan.package_path),
        "missing_requirements": list(plan.missing_requirements),
        "commands": [list(command.argv) for command in plan.commands],
        "signing": "not_performed",
        "notarization": "not_performed",
        "distribution_ready": False,
        "message": plan.message,
    }


def _run(command: tuple[str, ...], *, cwd: Path, environment: dict[str, str]) -> None:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=environment,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("release command failed")


def _prepare_dmg_staging(plan: ReleaseBuildPlan) -> None:
    if plan.app_path is None or plan.staging_root is None:
        raise RuntimeError("macOS packaging paths are incomplete")
    if not plan.app_path.is_dir():
        raise RuntimeError("PyInstaller did not produce JARVIS.app")
    try:
        plan.staging_root.relative_to(plan.workspace_root)
    except ValueError as exc:
        raise RuntimeError("DMG staging path escaped the release workspace") from exc
    if plan.staging_root.name != "dmg-root":
        raise RuntimeError("DMG staging path is not recognized")
    if plan.staging_root.exists():
        shutil.rmtree(plan.staging_root)
    plan.staging_root.mkdir(mode=0o700, parents=True)
    shutil.copytree(
        plan.app_path,
        plan.staging_root / "JARVIS.app",
        symlinks=True,
    )
    os.symlink("/Applications", plan.staging_root / "Applications")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_product_config(path: Path) -> None:
    if path.name != "product.json" or path.is_symlink() or not path.is_file():
        raise RuntimeError("product client configuration path is invalid")
    if not 1 <= path.stat().st_size <= 64 * 1024:
        raise RuntimeError("product client configuration size is invalid")
    result = load_product_client_config(
        source_config=path,
        packaged=True,
    )
    if not result.ok:
        raise RuntimeError("product client configuration is invalid")


def prepare_build_metadata(plan: ReleaseBuildPlan) -> Path:
    """Write the non-secret ``product_build.json`` identity for the frozen app."""

    plan.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    build_metadata = plan.workspace_root / "product_build.json"
    build_metadata.write_text(
        json.dumps(
            {
                "product_id": PRODUCT_ID,
                "bundle_id": BUNDLE_ID,
                "version": str(plan.request.version),
                "build": plan.request.build,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        build_metadata.chmod(0o600)
    return build_metadata


def build_environment(
    plan: ReleaseBuildPlan,
    *,
    build_metadata: Path,
    product_config: Path,
) -> dict[str, str]:
    """Build the PyInstaller environment shared by every packaging driver."""

    environment = dict(os.environ)
    environment.update(
        {
            "JARVIS_BUILD_VERSION": str(plan.request.version),
            "JARVIS_BUILD_NUMBER": str(plan.request.build),
            "JARVIS_TARGET_ARCH": plan.request.architecture,
            "JARVIS_BUILD_METADATA": str(build_metadata),
            "JARVIS_PRODUCT_CONFIG": str(product_config.resolve(strict=True)),
        }
    )
    return environment


def _execute_local_unsigned(
    plan: ReleaseBuildPlan,
    *,
    product_config: Path,
) -> dict[str, object]:
    if not plan.executable or len(plan.commands) != 2:
        raise RuntimeError("macOS packaging plan is not executable")
    _validate_product_config(product_config)
    build_metadata = prepare_build_metadata(plan)
    environment = build_environment(
        plan,
        build_metadata=build_metadata,
        product_config=product_config,
    )
    _run(
        plan.commands[0].argv,
        cwd=plan.commands[0].cwd,
        environment=environment,
    )
    _prepare_dmg_staging(plan)
    _run(
        plan.commands[1].argv,
        cwd=plan.commands[1].cwd,
        environment=environment,
    )
    if plan.package_path is None or not plan.package_path.is_file():
        raise RuntimeError("hdiutil did not produce a DMG")
    byte_size = plan.package_path.stat().st_size
    if byte_size <= 0:
        raise RuntimeError("generated DMG is empty")
    return {
        **_plan_document(plan),
        "local_unsigned_build": "created",
        "package_sha256": _sha256_file(plan.package_path),
        "package_byte_size": byte_size,
        "distribution_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = ReleaseBuildRequest.create(
            project_root=PROJECT_ROOT,
            output_root=args.output_root,
            version=args.version,
            build=args.build,
            architecture=args.architecture,
            python_executable=sys.executable,
        )
        plan = MacOSReleaseAdapter().plan_build(request)
        if not args.execute_local_unsigned:
            print(json.dumps(_plan_document(plan), indent=2, sort_keys=True))
            return 0 if plan.executable else 2
        if args.product_config is None:
            raise RuntimeError("--product-config is required for an executable build")
        result = _execute_local_unsigned(plan, product_config=args.product_config)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "message": str(exc),
                    "distribution_ready": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
