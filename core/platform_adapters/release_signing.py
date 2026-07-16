"""macOS Developer ID signing / notarization planner for JARVIS.app.

This module is deliberately side-effect free.  It only *plans* the argv-only
commands that a future audited implementation would need to sign, verify,
notarize and staple a locally built ``JARVIS.app`` / DMG.  It never runs them,
never loads a private key, and never embeds a credential.  A ready plan is not
evidence that either artifact has actually been signed or notarized.

The signing identity, team id and notarytool keychain-profile name are read from
the environment.  Those values are public labels — the actual Developer ID
private key and the app-specific/App Store Connect credentials stay inside the
macOS keychain and are referenced only by name.  The planner requires the exact
``Developer ID Application`` label, its matching Team ID, a notary profile, and
both built artifacts.  Missing or inconsistent input is reported as
:data:`ReleaseCapabilityStatus.NOT_AVAILABLE`; the artifact always remains an
unsigned development build until a separate audited executor proves otherwise.

The review plan covers only inner-before-outer app signing and subsequent
verification/notarization commands.  It deliberately does not pretend to be a
complete executor: rebuilding the DMG from the signed app, signing that DMG,
parsing an ``Accepted`` notary result, and final verification remain missing.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .release_base import ReleaseCapabilityStatus, ReleaseCommand


# Environment interface (public labels only; never secrets).
ENV_SIGN_IDENTITY = "JARVIS_MACOS_SIGN_IDENTITY"
ENV_TEAM_ID = "JARVIS_MACOS_TEAM_ID"
ENV_NOTARY_PROFILE = "JARVIS_MACOS_NOTARY_PROFILE"

_CODESIGN = "/usr/bin/codesign"
_SPCTL = "/usr/sbin/spctl"
_XCRUN = "/usr/bin/xcrun"

# Production distribution requires the human-readable Developer ID Application
# certificate label so the configured Team ID can be matched mechanically.  A
# bare hash is deliberately not accepted by this planner.
_TEAM_ID_RE = re.compile(r"[A-Z0-9]{10}")
_DEVELOPER_ID_RE = re.compile(
    r"Developer ID Application: [^\r\n]{1,200} \(([A-Z0-9]{10})\)"
)
_NOTARY_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}")

# Inner Mach-O code that must be signed before the outer bundle.  Directories
# are matched by suffix; loose files inside ``Contents/MacOS`` are treated as
# executables.
_NESTED_CODE_SUFFIXES = (".dylib", ".so")
_NESTED_BUNDLE_SUFFIXES = (".framework", ".app", ".bundle", ".xpc")


@dataclass(frozen=True, slots=True)
class SigningConfig:
    """Public signing labels resolved from the environment."""

    identity: str | None
    team_id: str | None
    notary_profile: str | None

    @property
    def can_sign(self) -> bool:
        identity_team_id = _identity_team_id(self.identity)
        return (
            identity_team_id is not None
            and self.team_id is not None
            and identity_team_id == self.team_id
        )

    @property
    def can_notarize(self) -> bool:
        cleaned = _clean_notary_profile(self.notary_profile)
        return self.can_sign and cleaned is not None and cleaned == self.notary_profile

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SigningConfig:
        source = os.environ if environ is None else environ
        return cls(
            identity=_clean_identity(source.get(ENV_SIGN_IDENTITY)),
            team_id=_clean_team_id(source.get(ENV_TEAM_ID)),
            notary_profile=_clean_notary_profile(source.get(ENV_NOTARY_PROFILE)),
        )


@dataclass(frozen=True, slots=True)
class SigningPlan:
    """A truthful, non-executing signing / notarization assessment."""

    status: ReleaseCapabilityStatus
    unsigned_dev_build: bool
    codesign_commands: tuple[ReleaseCommand, ...]
    verify_commands: tuple[ReleaseCommand, ...]
    notarize_commands: tuple[ReleaseCommand, ...]
    missing_requirements: tuple[str, ...]
    message: str = field(repr=False)

    @property
    def signed(self) -> bool:
        """Planning never proves that a filesystem artifact was signed."""

        return False

    @property
    def plan_ready(self) -> bool:
        """Whether all public inputs for the non-executing plan are present."""

        return (
            self.status is ReleaseCapabilityStatus.AVAILABLE
            and bool(self.codesign_commands)
            and bool(self.verify_commands)
            and bool(self.notarize_commands)
            and not self.missing_requirements
        )


def _identity_team_id(value: object) -> str | None:
    if type(value) is not str:
        return None
    match = _DEVELOPER_ID_RE.fullmatch(value)
    return None if match is None else match.group(1)


def _clean_identity(value: object) -> str | None:
    if type(value) is not str:
        return None
    cleaned = value.strip()
    return cleaned if _identity_team_id(cleaned) is not None else None


def _clean_team_id(value: object) -> str | None:
    if type(value) is not str:
        return None
    cleaned = value.strip()
    return cleaned if _TEAM_ID_RE.fullmatch(cleaned) else None


def _clean_notary_profile(value: object) -> str | None:
    if type(value) is not str:
        return None
    cleaned = value.strip()
    return cleaned if _NOTARY_PROFILE_RE.fullmatch(cleaned) else None


def enumerate_signable_paths(app_path: Path) -> tuple[Path, ...]:
    """Return nested Mach-O code inside *app_path*, deepest-first.

    The outer bundle itself is intentionally excluded; callers sign it last.
    Ordering is deterministic (depth descending, then lexicographic) so the
    nested-before-outer contract is stable and testable.  Only used at execution
    time against a real bundle; returns an empty tuple when the bundle is absent.
    """

    root = Path(app_path)
    if not root.is_dir():
        return ()

    macos_dir = root / "Contents" / "MacOS"
    candidates: set[Path] = set()

    for current, directories, files in os.walk(root):
        current_path = Path(current)
        for name in list(directories):
            child = current_path / name
            if child.suffix in _NESTED_BUNDLE_SUFFIXES and child != root:
                candidates.add(child)
        for name in files:
            child = current_path / name
            if child.is_symlink():
                continue
            if child.suffix in _NESTED_CODE_SUFFIXES:
                candidates.add(child)
            elif not child.suffix and macos_dir in child.parents:
                # Loose helper executables shipped next to the main binary.
                candidates.add(child)

    return tuple(
        sorted(
            candidates,
            key=lambda item: (-len(item.relative_to(root).parts), str(item)),
        )
    )


def _codesign_command(
    target: Path,
    *,
    identity: str,
    entitlements: Path,
    resource_root: Path,
) -> ReleaseCommand:
    return ReleaseCommand(
        name="codesign",
        argv=(
            _CODESIGN,
            "--force",
            "--options",
            "runtime",
            "--timestamp",
            "--entitlements",
            str(entitlements),
            "--sign",
            identity,
            str(target),
        ),
        cwd=resource_root,
    )


def plan_macos_signing(
    *,
    app_path: Path,
    entitlements_path: Path,
    resource_root: Path,
    package_path: Path | None = None,
    config: SigningConfig | None = None,
    codesign_tool: Path = Path(_CODESIGN),
    spctl_tool: Path = Path(_SPCTL),
    xcrun_tool: Path = Path(_XCRUN),
) -> SigningPlan:
    """Plan Developer ID signing, verification, notarization and stapling.

    Returns an honest ``NOT_AVAILABLE`` unsigned-dev-build plan unless every
    public planning input exists and agrees.  Even a complete plan has
    ``signed == False``: it produces no filesystem mutations and no commands
    containing secret material.
    """

    signing = SigningConfig.from_env() if config is None else config
    missing: list[str] = []
    identity_team_id = _identity_team_id(signing.identity)
    if identity_team_id is None:
        missing.append(
            f"{ENV_SIGN_IDENTITY} (exact Developer ID Application identity label)"
        )
    if signing.team_id is None:
        missing.append(f"{ENV_TEAM_ID} (10-character Apple Team ID)")
    elif identity_team_id is not None and identity_team_id != signing.team_id:
        missing.append("Developer ID identity and configured Team ID must match")
    notary_profile = _clean_notary_profile(signing.notary_profile)
    if notary_profile is None or notary_profile != signing.notary_profile:
        missing.append(f"{ENV_NOTARY_PROFILE} (notarytool keychain profile)")
    if app_path.suffix != ".app" or app_path.is_symlink() or not app_path.is_dir():
        missing.append("built JARVIS.app bundle")
    if (
        package_path is None
        or package_path.suffix.lower() != ".dmg"
        or package_path.is_symlink()
        or not package_path.is_file()
        or package_path.stat().st_size <= 0
    ):
        missing.append("built JARVIS DMG")
    if not entitlements_path.is_file():
        missing.append("hardened-runtime entitlements plist")
    if not Path(codesign_tool).is_file():
        missing.append("codesign tool")
    if not Path(spctl_tool).is_file():
        missing.append("spctl tool")
    if not Path(xcrun_tool).is_file():
        missing.append("xcrun tool")

    if missing:
        return SigningPlan(
            status=ReleaseCapabilityStatus.NOT_AVAILABLE,
            unsigned_dev_build=True,
            codesign_commands=(),
            verify_commands=(),
            notarize_commands=(),
            missing_requirements=tuple(missing),
            message=(
                "Production signing planning requirements are incomplete; the "
                "artifacts remain unsigned local development artifacts and are "
                "not distribution ready."
            ),
        )

    identity = signing.identity
    assert identity is not None  # guarded by signing.can_sign above

    codesign_commands: list[ReleaseCommand] = [
        _codesign_command(
            nested,
            identity=identity,
            entitlements=entitlements_path,
            resource_root=resource_root,
        )
        for nested in enumerate_signable_paths(app_path)
    ]
    # Sign the outer bundle last (inner-to-outer order).
    codesign_commands.append(
        _codesign_command(
            app_path,
            identity=identity,
            entitlements=entitlements_path,
            resource_root=resource_root,
        )
    )

    verify_commands: tuple[ReleaseCommand, ...] = (
        ReleaseCommand(
            name="codesign_verify",
            argv=(
                _CODESIGN,
                "--verify",
                "--deep",
                "--strict",
                "--verbose=2",
                str(app_path),
            ),
            cwd=resource_root,
        ),
        ReleaseCommand(
            name="spctl_assess_app",
            argv=(
                _SPCTL,
                "--assess",
                "--type",
                "execute",
                "--verbose=4",
                str(app_path),
            ),
            cwd=resource_root,
        ),
    )

    profile = notary_profile
    assert profile is not None  # guarded by the complete requirement check
    assert package_path is not None  # guarded by the complete requirement check
    notarize_commands: tuple[ReleaseCommand, ...] = (
        ReleaseCommand(
            name="notarytool_submit",
            argv=(
                _XCRUN,
                "notarytool",
                "submit",
                str(package_path),
                "--keychain-profile",
                profile,
                "--wait",
            ),
            cwd=resource_root,
        ),
        ReleaseCommand(
            name="stapler_staple",
            argv=(_XCRUN, "stapler", "staple", str(package_path)),
            cwd=resource_root,
        ),
        ReleaseCommand(
            name="stapler_validate",
            argv=(_XCRUN, "stapler", "validate", str(package_path)),
            cwd=resource_root,
        ),
        ReleaseCommand(
            name="spctl_assess_install",
            argv=(
                _SPCTL,
                "--assess",
                "--type",
                "install",
                "--verbose=4",
                str(package_path),
            ),
            cwd=resource_root,
        ),
    )

    return SigningPlan(
        status=ReleaseCapabilityStatus.AVAILABLE,
        unsigned_dev_build=True,
        codesign_commands=tuple(codesign_commands),
        verify_commands=verify_commands,
        notarize_commands=notarize_commands,
        missing_requirements=(),
        message=(
            "Developer ID signing/notarization planning inputs are complete. "
            "Execution remains unavailable until the production sequence is "
            "implemented and independently audited."
        ),
    )


__all__ = [
    "ENV_NOTARY_PROFILE",
    "ENV_SIGN_IDENTITY",
    "ENV_TEAM_ID",
    "SigningConfig",
    "SigningPlan",
    "enumerate_signable_paths",
    "plan_macos_signing",
]
