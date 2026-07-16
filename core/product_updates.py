"""Signed update discovery and private, digest-verified artifact staging."""

from __future__ import annotations

import hmac
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import unquote, urlsplit

from core.device_identity import (
    STATUS_NOT_AVAILABLE as IDENTITY_NOT_AVAILABLE,
    STATUS_SUCCESS as IDENTITY_SUCCESS,
    DeviceIdentityManager,
)
from core.entitlement_cache import SignedEntitlementCache
from core.product_api_client import ApiErrorCode, ProductApiClient, ProductApiError
from core.product_offer import (
    MAX_PAYMENT_INSTRUCTIONS_TEXT_LENGTH,
    MAX_PAYMENT_METHOD_TEXT_LENGTH,
    MAX_PAYMENT_RECIPIENT_TEXT_LENGTH,
)
from core.product_version import (
    PRODUCT_ID,
    ProductVersion,
    normalize_architecture,
    normalize_platform,
)
from core.release_manifest import (
    MAX_ARTIFACT_BYTES,
    STATUS_SUCCESS as MANIFEST_SUCCESS,
    ArtifactKind,
    VerifiedReleaseManifest,
    verify_release_manifest,
)


STATUS_CURRENT: Final = "current"
STATUS_PURCHASE_REQUIRED: Final = "purchase_required"
STATUS_ENTITLED: Final = "entitled"
STATUS_SUCCESS: Final = "success"
STATUS_ENTITLEMENT_REQUIRED: Final = "entitlement_required"
STATUS_INVALID: Final = "invalid"
STATUS_OFFLINE: Final = "offline"
STATUS_SERVER_UNAVAILABLE: Final = "server_unavailable"
STATUS_NOT_AVAILABLE: Final = "not_available"
STATUS_FAILED: Final = "failed"

UPDATE_CHECK_PATH: Final = "/v1/client/updates/check"
DEVICE_CHALLENGE_PATH: Final = "/api/device-challenges"
AUTHORIZE_INSTALL_ACTION: Final = "authorize_install"

_OPAQUE_ID_RE: Final = re.compile(r"[a-z0-9](?:[a-z0-9._-]{1,126}[a-z0-9])")
_FINGERPRINT_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
_CURRENCY_RE: Final = re.compile(r"[A-Z]{3}")
_CHECK_STATUSES: Final = frozenset(
    {
        STATUS_CURRENT,
        STATUS_PURCHASE_REQUIRED,
        STATUS_ENTITLED,
        STATUS_INVALID,
        STATUS_OFFLINE,
        STATUS_SERVER_UNAVAILABLE,
        STATUS_NOT_AVAILABLE,
        STATUS_ENTITLEMENT_REQUIRED,
        STATUS_FAILED,
    }
)
_DOWNLOAD_STATUSES: Final = frozenset(
    {
        STATUS_SUCCESS,
        STATUS_ENTITLEMENT_REQUIRED,
        STATUS_INVALID,
        STATUS_OFFLINE,
        STATUS_SERVER_UNAVAILABLE,
        STATUS_NOT_AVAILABLE,
        STATUS_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class ReleaseDisplayInfo:
    version: str
    price_minor: int
    currency: str
    supported_platforms: tuple[str, ...]
    features_en: str
    features_ru: str
    fixes_en: str
    fixes_ru: str


@dataclass(frozen=True, slots=True)
class PaymentInstructionsDisplay:
    status: str
    method_en: str = ""
    method_ru: str = ""
    recipient: str = field(default="", repr=False)
    instructions_en: str = ""
    instructions_ru: str = ""

    @property
    def configured(self) -> bool:
        return self.status == "configured"


@dataclass(frozen=True, slots=True)
class VerifiedUpdateCandidate:
    source: ProductVersion
    manifest: VerifiedReleaseManifest = field(repr=False)
    manifest_envelope: str = field(repr=False)
    artifact_id: str = field(repr=False)
    release_id: str = field(repr=False)
    release_info: ReleaseDisplayInfo = field(repr=False)
    payment_instructions: PaymentInstructionsDisplay | None = field(
        default=None,
        repr=False,
    )
    download_path: str | None = field(default=None, repr=False)
    download_grant: str | None = field(default=None, repr=False)
    entitlement_verified: bool = False

    @property
    def target(self) -> ProductVersion:
        return self.manifest.product_version


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    status: str
    message: str = field(repr=False)
    candidate: VerifiedUpdateCandidate | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status not in _CHECK_STATUSES:
            raise ValueError("unsupported update-check status")
        should_have_candidate = self.status in {
            STATUS_PURCHASE_REQUIRED,
            STATUS_ENTITLED,
        }
        if should_have_candidate != (self.candidate is not None):
            raise ValueError("candidate does not match update-check status")

    @property
    def ok(self) -> bool:
        return self.status in {
            STATUS_CURRENT,
            STATUS_PURCHASE_REQUIRED,
            STATUS_ENTITLED,
        }

    def __repr__(self) -> str:
        candidate = "verified" if self.candidate is not None else "none"
        return f"UpdateCheckResult(status={self.status!r}, candidate={candidate!r})"


@dataclass(frozen=True, slots=True)
class VerifiedStagedUpdate:
    path: Path = field(repr=False)
    source: ProductVersion
    target: ProductVersion
    sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class UpdateDownloadResult:
    status: str
    message: str = field(repr=False)
    staged: VerifiedStagedUpdate | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status not in _DOWNLOAD_STATUSES:
            raise ValueError("unsupported update-download status")
        if (self.status == STATUS_SUCCESS) != (self.staged is not None):
            raise ValueError("only success may carry a staged artifact")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS and self.staged is not None

    def __repr__(self) -> str:
        staged = "verified" if self.staged is not None else "none"
        return f"UpdateDownloadResult(status={self.status!r}, staged={staged!r})"


def _check_result(
    status: str,
    message: str,
    candidate: VerifiedUpdateCandidate | None = None,
) -> UpdateCheckResult:
    return UpdateCheckResult(status, message, candidate)


def _download_result(
    status: str,
    message: str,
    staged: VerifiedStagedUpdate | None = None,
) -> UpdateDownloadResult:
    return UpdateDownloadResult(status, message, staged)


def _normalized_target(value: object, normalizer) -> str:
    if type(value) is not str:
        raise ValueError("target is invalid")
    normalized = normalizer(value)
    if normalized == "unknown":
        raise ValueError("target is unsupported")
    return normalized


def _download_api_path(value: object) -> str:
    if type(value) is not str or not value.startswith("/") or len(value) > 1024:
        raise ValueError("download path is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path.startswith("//")
        or "\\" in parsed.path
        or any(part in {".", ".."} for part in unquote(parsed.path).split("/"))
    ):
        raise ValueError("download path is invalid")
    return parsed.path


def _download_grant(value: object) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if (
        type(value) is not str
        or not 20 <= len(value) <= 128
        or any(character not in alphabet for character in value)
    ):
        raise ValueError("download grant is invalid")
    return value


def _display_text(value: object, *, maximum: int, empty: bool) -> str:
    if (
        type(value) is not str
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "<" in value
        or ">" in value
        or (not empty and not value.strip())
    ):
        raise ValueError("display text is invalid")
    return value


def _release_display_info(
    value: object,
    *,
    expected_version: str,
    expected_platform: str,
) -> ReleaseDisplayInfo:
    if type(value) is not dict or frozenset(value) != {
        "version",
        "price_minor",
        "currency",
        "supported_platforms",
        "features",
        "fixes",
    }:
        raise ValueError("release information is invalid")
    version = value["version"]
    price_minor = value["price_minor"]
    currency = value["currency"]
    supported = value["supported_platforms"]
    features = value["features"]
    fixes = value["fixes"]
    if (
        version != expected_version
        or type(price_minor) is not int
        or not 1 <= price_minor <= 10**15
        or type(currency) is not str
        or _CURRENCY_RE.fullmatch(currency) is None
        or type(supported) is not list
        or not 1 <= len(supported) <= 3
        or supported != sorted(set(supported))
        or expected_platform not in supported
        or any(item not in {"macos", "windows", "linux"} for item in supported)
        or type(features) is not dict
        or frozenset(features) != {"en", "ru"}
        or type(fixes) is not dict
        or frozenset(fixes) != {"en", "ru"}
    ):
        raise ValueError("release information is invalid")
    return ReleaseDisplayInfo(
        version,
        price_minor,
        currency,
        tuple(supported),
        _display_text(features["en"], maximum=4000, empty=True),
        _display_text(features["ru"], maximum=4000, empty=True),
        _display_text(fixes["en"], maximum=4000, empty=True),
        _display_text(fixes["ru"], maximum=4000, empty=True),
    )


def _payment_instructions(value: object) -> PaymentInstructionsDisplay:
    if type(value) is not dict:
        raise ValueError("payment instructions are invalid")
    if value == {"status": "not_configured"}:
        return PaymentInstructionsDisplay("not_configured")
    if frozenset(value) != {"status", "method", "recipient", "instructions"}:
        raise ValueError("payment instructions are invalid")
    method = value["method"]
    instructions = value["instructions"]
    if (
        value["status"] != "configured"
        or type(method) is not dict
        or frozenset(method) != {"en", "ru"}
        or type(instructions) is not dict
        or frozenset(instructions) != {"en", "ru"}
    ):
        raise ValueError("payment instructions are invalid")
    return PaymentInstructionsDisplay(
        "configured",
        _display_text(
            method["en"],
            maximum=MAX_PAYMENT_METHOD_TEXT_LENGTH,
            empty=False,
        ),
        _display_text(
            method["ru"],
            maximum=MAX_PAYMENT_METHOD_TEXT_LENGTH,
            empty=False,
        ),
        _display_text(
            value["recipient"],
            maximum=MAX_PAYMENT_RECIPIENT_TEXT_LENGTH,
            empty=False,
        ),
        _display_text(
            instructions["en"],
            maximum=MAX_PAYMENT_INSTRUCTIONS_TEXT_LENGTH,
            empty=False,
        ),
        _display_text(
            instructions["ru"],
            maximum=MAX_PAYMENT_INSTRUCTIONS_TEXT_LENGTH,
            empty=False,
        ),
    )


def _api_check_failure(error: ProductApiError) -> UpdateCheckResult:
    if error.code is ApiErrorCode.NETWORK_UNAVAILABLE:
        return _check_result(STATUS_OFFLINE, "Update check is offline.")
    if error.code is ApiErrorCode.SERVER_UNAVAILABLE:
        return _check_result(
            STATUS_SERVER_UNAVAILABLE,
            "Update server is unavailable.",
        )
    if error.code in {
        ApiErrorCode.RESPONSE_INVALID,
        ApiErrorCode.RESPONSE_TOO_LARGE,
    }:
        return _check_result(STATUS_INVALID, "Update response is invalid.")
    return _check_result(STATUS_FAILED, "Update check failed.")


def _api_download_failure(error: ProductApiError) -> UpdateDownloadResult:
    if error.code is ApiErrorCode.NETWORK_UNAVAILABLE:
        return _download_result(STATUS_OFFLINE, "Update download is offline.")
    if error.code is ApiErrorCode.SERVER_UNAVAILABLE:
        return _download_result(
            STATUS_SERVER_UNAVAILABLE,
            "Update server is unavailable.",
        )
    if error.code in {
        ApiErrorCode.RESPONSE_INVALID,
        ApiErrorCode.RESPONSE_TOO_LARGE,
    }:
        return _download_result(STATUS_INVALID, "Update download is invalid.")
    return _download_result(STATUS_FAILED, "Update download failed.")


class ProductUpdateService:
    """Check authority, reverify signed metadata, and stage exact artifact bytes."""

    __slots__ = (
        "_api",
        "_cache",
        "_identity_manager",
        "_release_public_keys",
        "_staging_directory",
    )

    def __init__(
        self,
        api_client: ProductApiClient,
        entitlement_cache: SignedEntitlementCache,
        identity_manager: DeviceIdentityManager,
        staging_directory: str | os.PathLike[str],
        *,
        trusted_release_public_keys: Mapping[str, object],
    ) -> None:
        if not isinstance(api_client, ProductApiClient):
            raise TypeError("api_client must be a ProductApiClient")
        if not isinstance(entitlement_cache, SignedEntitlementCache):
            raise TypeError("entitlement_cache must be a SignedEntitlementCache")
        if not isinstance(identity_manager, DeviceIdentityManager):
            raise TypeError("identity_manager must be a DeviceIdentityManager")
        staging = Path(staging_directory).expanduser()
        if not staging.is_absolute():
            raise ValueError("update staging directory must be absolute")
        if staging.is_symlink():
            raise ValueError("update staging directory cannot be a symlink")
        staging = staging.resolve(strict=False)
        if not isinstance(trusted_release_public_keys, Mapping):
            raise TypeError("trusted_release_public_keys must be a mapping")
        self._api = api_client
        self._cache = entitlement_cache
        self._identity_manager = identity_manager
        self._staging_directory = staging
        self._release_public_keys = dict(trusted_release_public_keys)

    def __repr__(self) -> str:
        return (
            "ProductUpdateService(api=<configured>, staging=<private>, "
            "identity=<secure>, release_keys=<pinned>)"
        )

    def _verify_manifest(
        self,
        manifest_envelope: str,
        *,
        installed: ProductVersion,
        platform: str,
        architecture: str,
        expected: VerifiedReleaseManifest | None = None,
    ) -> VerifiedReleaseManifest | None:
        expectations: dict[str, object] = {
            "expected_platform": platform,
            "expected_architecture": architecture,
            "expected_artifact_kind": ArtifactKind.UPDATE_PACKAGE,
        }
        if expected is not None:
            expectations.update(
                {
                    "expected_version": expected.version,
                    "expected_build": expected.build,
                    "expected_sha256": expected.sha256,
                    "expected_byte_size": expected.byte_size,
                    "expected_storage_key": expected.storage_key,
                }
            )
        verification = verify_release_manifest(
            manifest_envelope,
            trusted_public_keys=self._release_public_keys,
            **expectations,
        )
        if verification.status != MANIFEST_SUCCESS or verification.claims is None:
            return None
        claims = verification.claims
        if not claims.product_version.is_newer_than(installed):
            return None
        if (
            claims.version != installed.version
            and installed.version not in claims.compatible_source_versions
        ):
            return None
        return claims

    def check(
        self,
        *,
        license_id: str,
        device_fingerprint: str,
        installed: ProductVersion,
        platform: str,
        architecture: str,
    ) -> UpdateCheckResult:
        try:
            if (
                type(license_id) is not str
                or _OPAQUE_ID_RE.fullmatch(license_id) is None
                or type(device_fingerprint) is not str
                or _FINGERPRINT_RE.fullmatch(device_fingerprint) is None
                or not isinstance(installed, ProductVersion)
            ):
                raise ValueError("update identity is invalid")
            platform_value = _normalized_target(platform, normalize_platform)
            architecture_value = _normalized_target(
                architecture,
                normalize_architecture,
            )
        except (TypeError, ValueError):
            return _check_result(STATUS_INVALID, "Update request is invalid.")

        request_payload = {
            "product_id": PRODUCT_ID,
            "license_id": license_id,
            "device_key_fingerprint": device_fingerprint,
            "installed_version": str(installed.version),
            "installed_build": installed.build,
            "platform": platform_value,
            "architecture": architecture_value,
        }
        try:
            response = self._api.request_json(
                "POST",
                UPDATE_CHECK_PATH,
                payload=request_payload,
            )
        except ProductApiError as exc:
            return _api_check_failure(exc)

        state = response.get("state")
        if state == STATUS_CURRENT and frozenset(response) == {"state"}:
            return _check_result(STATUS_CURRENT, "Installed version is current.")
        if state != STATUS_PURCHASE_REQUIRED or frozenset(response) != {
            "state",
            "manifest",
            "artifact_id",
            "release_id",
            "release_info",
        }:
            return _check_result(STATUS_INVALID, "Update response is invalid.")
        manifest_input = response["manifest"]
        artifact_id = response["artifact_id"]
        release_id = response["release_id"]
        if (
            type(manifest_input) is not str
            or type(artifact_id) is not str
            or _OPAQUE_ID_RE.fullmatch(artifact_id) is None
            or type(release_id) is not str
            or _OPAQUE_ID_RE.fullmatch(release_id) is None
        ):
            return _check_result(STATUS_INVALID, "Update response is invalid.")
        claims = self._verify_manifest(
            manifest_input,
            installed=installed,
            platform=platform_value,
            architecture=architecture_value,
        )
        if claims is None:
            status = (
                STATUS_NOT_AVAILABLE if not self._release_public_keys else STATUS_INVALID
            )
            return _check_result(status, "Signed update metadata is invalid.")
        try:
            release_info = _release_display_info(
                response["release_info"],
                expected_version=str(claims.version),
                expected_platform=platform_value,
            )
        except ValueError:
            return _check_result(STATUS_INVALID, "Release information is invalid.")

        identity_result = self._identity_manager.load()
        if identity_result.status == IDENTITY_NOT_AVAILABLE:
            return _check_result(
                STATUS_NOT_AVAILABLE,
                "Secure device identity is not available.",
            )
        if identity_result.status != IDENTITY_SUCCESS or identity_result.identity is None:
            return _check_result(
                STATUS_ENTITLEMENT_REQUIRED,
                "Activated device identity is required.",
            )
        identity = identity_result.identity
        if not hmac.compare_digest(identity.fingerprint, device_fingerprint):
            return _check_result(
                STATUS_ENTITLEMENT_REQUIRED,
                "Active device identity does not match.",
            )
        try:
            challenge = self._api.request_json(
                "POST",
                DEVICE_CHALLENGE_PATH,
                payload={
                    "license_id": license_id,
                    "device_key_fingerprint": identity.fingerprint,
                    "action": AUTHORIZE_INSTALL_ACTION,
                    "resource_id": artifact_id,
                },
            )
            if frozenset(challenge) != {
                "challenge_id",
                "challenge_nonce",
                "action",
                "resource_id",
                "issued_at",
                "expires_at",
            }:
                return _check_result(STATUS_INVALID, "Device challenge is invalid.")
            challenge_id = challenge["challenge_id"]
            challenge_nonce = challenge["challenge_nonce"]
            if (
                type(challenge_id) is not str
                or _OPAQUE_ID_RE.fullmatch(challenge_id) is None
                or type(challenge_nonce) is not str
                or challenge["action"] != AUTHORIZE_INSTALL_ACTION
                or challenge["resource_id"] != artifact_id
                or type(challenge["issued_at"]) is not str
                or type(challenge["expires_at"]) is not str
            ):
                return _check_result(STATUS_INVALID, "Device challenge is invalid.")
            signature = identity.sign_challenge(challenge_nonce)
            grant_response = self._api.request_json(
                "POST",
                f"{DEVICE_CHALLENGE_PATH}/{challenge_id}/verify",
                payload={
                    "challenge_nonce": challenge_nonce,
                    "public_key_base64": identity.public_key_base64,
                    "signature_base64": signature,
                },
            )
            if frozenset(grant_response) != {
                "device_grant",
                "action",
                "resource_id",
                "expires_at",
            }:
                return _check_result(STATUS_INVALID, "Device grant is invalid.")
            device_grant = grant_response["device_grant"]
            if (
                type(device_grant) is not str
                or not 16 <= len(device_grant) <= 4096
                or any(character in device_grant for character in "\x00\r\n")
                or grant_response["action"] != AUTHORIZE_INSTALL_ACTION
                or grant_response["resource_id"] != artifact_id
                or type(grant_response["expires_at"]) is not str
            ):
                return _check_result(STATUS_INVALID, "Device grant is invalid.")
            authorized = self._api.request_json(
                "POST",
                UPDATE_CHECK_PATH,
                payload=request_payload,
                headers={"X-Device-Grant": device_grant},
            )
        except ProductApiError as exc:
            if exc.code in {
                ApiErrorCode.UNAUTHORIZED,
                ApiErrorCode.NOT_FOUND,
                ApiErrorCode.CONFLICT,
            }:
                return _check_result(
                    STATUS_ENTITLEMENT_REQUIRED,
                    "Verified update authorization is required.",
                )
            return _api_check_failure(exc)
        except (TypeError, ValueError, RuntimeError):
            return _check_result(STATUS_INVALID, "Device proof is invalid.")

        if authorized.get("state") == STATUS_CURRENT and frozenset(authorized) == {
            "state"
        }:
            return _check_result(STATUS_CURRENT, "Installed version is current.")
        if authorized.get("state") == STATUS_PURCHASE_REQUIRED:
            if (
                frozenset(authorized)
                != {
                    "state",
                    "manifest",
                    "artifact_id",
                    "release_id",
                    "release_info",
                    "payment_instructions",
                }
                or authorized["manifest"] != manifest_input
                or authorized["artifact_id"] != artifact_id
                or authorized["release_id"] != release_id
                or authorized["release_info"] != response["release_info"]
            ):
                return _check_result(STATUS_INVALID, "Update response is invalid.")
            try:
                payment_instructions = _payment_instructions(
                    authorized["payment_instructions"]
                )
            except ValueError:
                return _check_result(
                    STATUS_INVALID,
                    "Payment instructions are invalid.",
                )
            return _check_result(
                STATUS_PURCHASE_REQUIRED,
                "Update purchase is required.",
                VerifiedUpdateCandidate(
                    source=installed,
                    manifest=claims,
                    manifest_envelope=manifest_input,
                    artifact_id=artifact_id,
                    release_id=release_id,
                    release_info=release_info,
                    payment_instructions=payment_instructions,
                ),
            )
        if authorized.get("state") != STATUS_ENTITLED or frozenset(authorized) != {
            "state",
            "manifest",
            "artifact_id",
            "release_id",
            "release_info",
            "download_path",
            "download_grant",
            "entitlement_certificate",
        }:
            return _check_result(STATUS_INVALID, "Update response is invalid.")
        if (
            authorized["manifest"] != manifest_input
            or authorized["artifact_id"] != artifact_id
            or authorized["release_id"] != release_id
            or authorized["release_info"] != response["release_info"]
        ):
            return _check_result(STATUS_INVALID, "Update response is invalid.")
        certificate_input = authorized["entitlement_certificate"]
        try:
            download_path = _download_api_path(authorized["download_path"])
            download_grant = _download_grant(authorized["download_grant"])
        except ValueError:
            return _check_result(STATUS_INVALID, "Update response is invalid.")
        if type(certificate_input) is not str:
            return _check_result(STATUS_INVALID, "Update response is invalid.")
        cached = self._cache.store_verified(
            certificate_input,
            license_id=license_id,
            device_fingerprint=device_fingerprint,
            version=claims.version,
        )
        if not cached.ok:
            return _check_result(
                STATUS_ENTITLEMENT_REQUIRED,
                "Exact-version entitlement could not be verified.",
            )
        candidate = VerifiedUpdateCandidate(
            source=installed,
            manifest=claims,
            manifest_envelope=manifest_input,
            artifact_id=artifact_id,
            release_id=release_id,
            release_info=release_info,
            download_path=download_path,
            download_grant=download_grant,
            entitlement_verified=True,
        )
        return _check_result(STATUS_ENTITLED, "Update is authorized.", candidate)

    def _ensure_staging_directory(self) -> None:
        self._staging_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._staging_directory.is_symlink():
            raise OSError("staging path is a symlink")
        opened = self._staging_directory.stat()
        if not stat.S_ISDIR(opened.st_mode):
            raise OSError("staging path is not a directory")
        if hasattr(os, "getuid") and opened.st_uid != os.getuid():
            raise OSError("staging directory owner mismatch")
        if os.name != "nt":
            self._staging_directory.chmod(0o700)

    def download(
        self,
        candidate: VerifiedUpdateCandidate,
        *,
        license_id: str,
        device_fingerprint: str,
        platform: str,
        architecture: str,
    ) -> UpdateDownloadResult:
        if (
            not isinstance(candidate, VerifiedUpdateCandidate)
            or not candidate.entitlement_verified
            or candidate.download_path is None
            or candidate.download_grant is None
        ):
            return _download_result(
                STATUS_ENTITLEMENT_REQUIRED,
                "Update entitlement is required.",
            )
        temporary: Path | None = None
        try:
            platform_value = _normalized_target(platform, normalize_platform)
            architecture_value = _normalized_target(
                architecture,
                normalize_architecture,
            )
            claims = self._verify_manifest(
                candidate.manifest_envelope,
                installed=candidate.source,
                platform=platform_value,
                architecture=architecture_value,
                expected=candidate.manifest,
            )
            if claims is None:
                return _download_result(STATUS_INVALID, "Update manifest is invalid.")
            identity_result = self._identity_manager.load()
            if (
                identity_result.status != IDENTITY_SUCCESS
                or identity_result.identity is None
                or not hmac.compare_digest(
                    identity_result.identity.fingerprint,
                    device_fingerprint,
                )
            ):
                return _download_result(
                    STATUS_ENTITLEMENT_REQUIRED,
                    "Active device identity is required.",
                )
            entitlement = self._cache.load_verified(
                license_id=license_id,
                device_fingerprint=device_fingerprint,
                version=claims.version,
            )
            if not entitlement.ok:
                return _download_result(
                    STATUS_ENTITLEMENT_REQUIRED,
                    "Exact-version entitlement is required.",
                )
            self._ensure_staging_directory()
            temporary = self._staging_directory / (
                ".download-" + secrets.token_hex(16) + ".part"
            )
            receipt = self._api.download_to_file(
                candidate.download_path,
                temporary,
                maximum_bytes=min(claims.byte_size, MAX_ARTIFACT_BYTES),
                headers={"X-Artifact-Grant": candidate.download_grant},
            )
            if (
                receipt.byte_size != claims.byte_size
                or receipt.sha256 != claims.sha256
            ):
                temporary.unlink(missing_ok=True)
                return _download_result(
                    STATUS_INVALID,
                    "Downloaded update does not match signed metadata.",
                )
            final_path = self._staging_directory / (
                "verified-" + claims.sha256 + ".package"
            )
            if final_path.is_symlink():
                temporary.unlink(missing_ok=True)
                return _download_result(STATUS_INVALID, "Staging target is invalid.")
            os.replace(temporary, final_path)
            temporary = None
            if os.name != "nt":
                final_path.chmod(0o600)
            opened = final_path.stat()
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != claims.byte_size:
                final_path.unlink(missing_ok=True)
                return _download_result(STATUS_INVALID, "Staged update is invalid.")
            return _download_result(
                STATUS_SUCCESS,
                "Update artifact was fully downloaded and verified.",
                VerifiedStagedUpdate(
                    final_path,
                    candidate.source,
                    claims.product_version,
                    claims.sha256,
                    claims.byte_size,
                ),
            )
        except ProductApiError as exc:
            return _api_download_failure(exc)
        except (OSError, TypeError, ValueError):
            return _download_result(STATUS_FAILED, "Update staging failed.")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


__all__ = [
    "STATUS_CURRENT",
    "STATUS_ENTITLED",
    "STATUS_ENTITLEMENT_REQUIRED",
    "STATUS_FAILED",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_OFFLINE",
    "STATUS_PURCHASE_REQUIRED",
    "STATUS_SERVER_UNAVAILABLE",
    "STATUS_SUCCESS",
    "UPDATE_CHECK_PATH",
    "PaymentInstructionsDisplay",
    "ProductUpdateService",
    "ReleaseDisplayInfo",
    "UpdateCheckResult",
    "UpdateDownloadResult",
    "VerifiedStagedUpdate",
    "VerifiedUpdateCandidate",
]
