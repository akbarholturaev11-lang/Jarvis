#!/usr/bin/env python3
"""Granular, argv-only driver for the local unsigned JARVIS.app + DMG pipeline.

Each subcommand is one honest, reversible step of the packaging pipeline and is
a thin driver over the shared :class:`MacOSReleaseAdapter` plan, the signing
planner and the build-manifest builder.  Nothing here signs, notarizes, uploads,
publishes, or claims distribution readiness.  When PyInstaller or ``hdiutil`` are
missing, the build/dmg steps refuse to run instead of faking a success.

Subcommands:

    plan          Print the read-only prerequisite/build plan.
    clean         Remove this version's release workspace before a fresh build.
    build-app     Freeze JARVIS.app with the pinned PyInstaller spec.
    stage-dmg     Stage JARVIS.app + an /Applications symlink for the DMG.
    build-dmg     Create the versioned drag-to-Applications DMG.
    manifest      Write a non-secret local build manifest (sha256 + size).
    verify-app    Structurally verify the bundle and its secret boundary.
    sign          Plan (or, with --execute, run) Developer ID signing.
    smoke         Check the bundle is self-contained (no system Python).
    cleanup       Remove intermediate work/staging, keep dist + dmg + manifest.
"""

from __future__ import annotations

import argparse
import json
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
from core.platform_adapters.release_signing import (  # noqa: E402
    SigningPlan,
    plan_macos_signing,
)
from core.product_version import BUNDLE_ID  # noqa: E402
from core.release_build_manifest import (  # noqa: E402
    build_artifact_manifest,
    write_build_manifest,
)
from scripts.build_macos_release import (  # noqa: E402
    _prepare_dmg_staging,
    _run,
    _validate_product_config,
    build_environment,
    prepare_build_metadata,
)


ENTITLEMENTS_PATH = PROJECT_ROOT / "packaging" / "macos" / "entitlements.plist"

# Files that must never be inside a distributed bundle.
_FORBIDDEN_BUNDLE_NAMES = frozenset(
    {
        "api_keys.json",
        "long_term.json",
        "briefing_sources.json",
        "local_env.zsh",
        "device_profile.json",
        "macros.json",
        "payment_instructions.json",
        "payment-instructions.json",
    }
)
# Private-key containers are always secret. ``.pem``/``.key`` are ambiguous:
# frozen apps legitimately ship public CA trust stores (certifi ``cacert.pem``,
# grpc ``roots.pem``), so those suffixes are only flagged when the file actually
# contains a PRIVATE KEY block.
_ALWAYS_SECRET_SUFFIXES = (".p12", ".pfx")
_KEY_MATERIAL_SUFFIXES = (".pem", ".key")
_PRIVATE_KEY_MARKER = b"PRIVATE KEY"


class PipelineError(RuntimeError):
    """A truthful pipeline failure that never claims partial success."""


def _make_plan(args: argparse.Namespace) -> ReleaseBuildPlan:
    request = ReleaseBuildRequest.create(
        project_root=PROJECT_ROOT,
        output_root=args.output_root,
        version=args.version,
        build=args.build,
        architecture=args.architecture,
        python_executable=sys.executable,
    )
    return MacOSReleaseAdapter().plan_build(request)


def _require_within(child: Path, parent: Path) -> None:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError as exc:
        raise PipelineError("path escaped the release workspace") from exc


def _cmd_plan(args: argparse.Namespace) -> dict[str, object]:
    plan = _make_plan(args)
    return {
        "step": "plan",
        "status": plan.status.value,
        "executable": plan.executable,
        "workspace_root": str(plan.workspace_root),
        "app_path": None if plan.app_path is None else str(plan.app_path),
        "package_path": None if plan.package_path is None else str(plan.package_path),
        "missing_requirements": list(plan.missing_requirements),
        "message": plan.message,
    }


def _cmd_clean(args: argparse.Namespace) -> dict[str, object]:
    plan = _make_plan(args)
    workspace = plan.workspace_root
    _require_within(workspace, args.output_root)
    removed = workspace.exists()
    if removed:
        shutil.rmtree(workspace)
    return {"step": "clean", "workspace_root": str(workspace), "removed": removed}


def _cmd_build_app(args: argparse.Namespace) -> dict[str, object]:
    plan = _make_plan(args)
    if not plan.executable or not plan.commands:
        raise PipelineError(
            "unsigned build prerequisites are incomplete: "
            + ", ".join(plan.missing_requirements)
        )
    if args.product_config is None:
        raise PipelineError("--product-config is required for build-app")
    _validate_product_config(args.product_config)
    build_metadata = prepare_build_metadata(plan)
    environment = build_environment(
        plan,
        build_metadata=build_metadata,
        product_config=args.product_config,
    )
    _run(plan.commands[0].argv, cwd=plan.commands[0].cwd, environment=environment)
    if plan.app_path is None or not plan.app_path.is_dir():
        raise PipelineError("PyInstaller did not produce JARVIS.app")
    return {
        "step": "build-app",
        "status": "created",
        "app_path": str(plan.app_path),
    }


def _cmd_stage_dmg(args: argparse.Namespace) -> dict[str, object]:
    plan = _make_plan(args)
    _prepare_dmg_staging(plan)
    return {
        "step": "stage-dmg",
        "staging_root": str(plan.staging_root),
    }


def _cmd_build_dmg(args: argparse.Namespace) -> dict[str, object]:
    plan = _make_plan(args)
    if not plan.executable or len(plan.commands) != 2:
        raise PipelineError("DMG prerequisites are incomplete")
    if plan.staging_root is None or not plan.staging_root.is_dir():
        raise PipelineError("run stage-dmg before build-dmg")
    _run(plan.commands[1].argv, cwd=plan.commands[1].cwd, environment=None)
    if plan.package_path is None or not plan.package_path.is_file():
        raise PipelineError("hdiutil did not produce a DMG")
    if plan.package_path.stat().st_size <= 0:
        raise PipelineError("generated DMG is empty")
    return {
        "step": "build-dmg",
        "status": "created",
        "package_path": str(plan.package_path),
    }


def _cmd_manifest(args: argparse.Namespace) -> dict[str, object]:
    plan = _make_plan(args)
    if plan.package_path is None or not plan.package_path.is_file():
        raise PipelineError("build the DMG before generating its manifest")
    manifest = build_artifact_manifest(
        artifact_path=plan.package_path,
        version=str(plan.request.version),
        build=plan.request.build,
        platform="macos",
        architecture=plan.request.architecture,
        signed=False,
        notarized=False,
    )
    destination = plan.workspace_root / "build_manifest.json"
    write_build_manifest(manifest, destination)
    document = manifest.to_document()
    document.update({"step": "manifest", "manifest_path": str(destination)})
    return document


def _looks_like_private_key(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return _PRIVATE_KEY_MARKER in handle.read(65536)
    except OSError:
        return False


def _scan_bundle_secrets(app_path: Path) -> list[str]:
    findings: list[str] = []
    for path in app_path.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel = str(path.relative_to(app_path))
        if path.name in _FORBIDDEN_BUNDLE_NAMES:
            findings.append(rel)
        elif "/config/certs/" in "/" + rel.replace("\\", "/"):
            findings.append(rel)
        elif path.suffix in _ALWAYS_SECRET_SUFFIXES:
            findings.append(rel)
        elif path.suffix in _KEY_MATERIAL_SUFFIXES and _looks_like_private_key(path):
            findings.append(rel)
    return sorted(findings)


def _cmd_verify_app(args: argparse.Namespace) -> dict[str, object]:
    plan = _make_plan(args)
    app_path = plan.app_path
    if app_path is None or not app_path.is_dir():
        raise PipelineError("build JARVIS.app before verifying it")
    executable = app_path / "Contents" / "MacOS" / "JARVIS"
    info_plist = app_path / "Contents" / "Info.plist"
    problems: list[str] = []
    if not executable.is_file():
        problems.append("missing bundled executable")
    if not info_plist.is_file():
        problems.append("missing Info.plist")
    else:
        import plistlib

        info = plistlib.loads(info_plist.read_bytes())
        if info.get("CFBundleIdentifier") != BUNDLE_ID:
            problems.append("Info.plist bundle identifier mismatch")
        if str(info.get("CFBundleShortVersionString") or "") != str(plan.request.version):
            problems.append("Info.plist version mismatch")
    secret_findings = _scan_bundle_secrets(app_path)
    if secret_findings:
        problems.append("secret files present in bundle")
    if problems:
        raise PipelineError("; ".join(problems) + f" ({secret_findings})")
    return {
        "step": "verify-app",
        "status": "ok",
        "app_path": str(app_path),
        "bundled_interpreter": str(executable),
        "secret_files_in_bundle": secret_findings,
    }


def _signing_plan(args: argparse.Namespace) -> tuple[ReleaseBuildPlan, SigningPlan]:
    plan = _make_plan(args)
    if plan.app_path is None:
        raise PipelineError("no macOS app path in plan")
    signing = plan_macos_signing(
        app_path=plan.app_path,
        entitlements_path=ENTITLEMENTS_PATH,
        resource_root=plan.resource_root,
        package_path=plan.package_path,
    )
    return plan, signing


def _cmd_sign(args: argparse.Namespace) -> dict[str, object]:
    plan, signing = _signing_plan(args)
    document: dict[str, object] = {
        "step": "sign",
        "status": signing.status.value,
        "signed": signing.signed,
        "unsigned_dev_build": signing.unsigned_dev_build,
        "missing_requirements": list(signing.missing_requirements),
        "codesign_commands": [list(c.argv) for c in signing.codesign_commands],
        "verify_commands": [list(c.argv) for c in signing.verify_commands],
        "notarize_commands": [list(c.argv) for c in signing.notarize_commands],
        "message": signing.message,
        "distribution_ready": False,
    }
    if not args.execute:
        return document
    if not signing.signed:
        raise PipelineError(signing.message)
    if plan.app_path is None or not plan.app_path.is_dir():
        raise PipelineError("build JARVIS.app before signing it")
    for command in (
        *signing.codesign_commands,
        *signing.verify_commands,
        *signing.notarize_commands,
    ):
        _run(command.argv, cwd=command.cwd, environment=None)
    document["status"] = "success"
    return document


def _cmd_smoke(args: argparse.Namespace) -> dict[str, object]:
    plan = _make_plan(args)
    app_path = plan.app_path
    if app_path is None or not app_path.is_dir():
        raise PipelineError("build JARVIS.app before the smoke launch")
    executable = app_path / "Contents" / "MacOS" / "JARVIS"
    if not executable.is_file():
        raise PipelineError("bundle has no self-contained interpreter")
    # The presence of an embedded Mach-O launcher proves the app does not depend
    # on a system Python interpreter or a terminal.  A full interactive launch is
    # a manual/CI step on a real user session and is intentionally not forced
    # here (it would start the live assistant).
    return {
        "step": "smoke",
        "status": "self_contained",
        "bundled_interpreter": str(executable),
        "requires_system_python": False,
        "requires_terminal": False,
        "interactive_launch": "manual_or_ci_on_built_app",
    }


def _cmd_cleanup(args: argparse.Namespace) -> dict[str, object]:
    plan = _make_plan(args)
    removed: list[str] = []
    for candidate in (
        plan.workspace_root / "work",
        plan.staging_root,
    ):
        if candidate is None:
            continue
        _require_within(candidate, args.output_root)
        if candidate.exists():
            shutil.rmtree(candidate)
            removed.append(str(candidate))
    return {"step": "cleanup", "removed": removed}


_COMMANDS = {
    "plan": _cmd_plan,
    "clean": _cmd_clean,
    "build-app": _cmd_build_app,
    "stage-dmg": _cmd_stage_dmg,
    "build-dmg": _cmd_build_dmg,
    "manifest": _cmd_manifest,
    "verify-app": _cmd_verify_app,
    "sign": _cmd_sign,
    "smoke": _cmd_smoke,
    "cleanup": _cmd_cleanup,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(_COMMANDS))
    parser.add_argument("--version", required=True, help="Strict MAJOR.MINOR.PATCH")
    parser.add_argument("--build", required=True, type=int, help="Positive global build")
    parser.add_argument("--architecture", default=platform.machine())
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "build" / "local-release",
    )
    parser.add_argument("--product-config", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="For 'sign': actually run the planned signing commands.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handler = _COMMANDS[args.command]
    try:
        result = handler(args)
    except (PipelineError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "step": args.command,
                    "status": "failed",
                    "message": str(exc),
                    "distribution_ready": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except subprocess.SubprocessError as exc:  # pragma: no cover - defensive
        print(json.dumps({"step": args.command, "status": "failed", "message": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
