from __future__ import annotations

import base64
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.product_version import BUNDLE_ID, PRODUCT_ID
from core.release_manifest import (
    MANIFEST_SCHEMA,
    SCHEMA_VERSION,
    ArtifactKind,
)
from product_backend import (
    ArtifactVerificationCandidate,
    ArtifactVerificationError,
    InstallMode,
    InvalidTransitionError,
    PinnedReleaseArtifactVerifier,
    SQLiteCommerceRepository,
    VerifiedDevicePrincipal,
)


KEY_ID = "release-key-001"
FINGERPRINT = "sha256:" + ("a" * 64)


def _canonical(document: dict[str, object]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _payload(candidate: ArtifactVerificationCandidate) -> dict[str, object]:
    return {
        "schema": MANIFEST_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "product_id": candidate.product_id,
        "bundle_id": candidate.bundle_id,
        "version": candidate.release_version,
        "build": candidate.build,
        "platform": candidate.platform,
        "architecture": candidate.architecture,
        "artifact_kind": candidate.artifact_kind.value,
        "sha256": candidate.sha256,
        "byte_size": candidate.byte_size,
        "storage_key": candidate.storage_key,
        "signing_key_id": candidate.signing_key_id,
        "compatible_source_versions": list(candidate.compatible_source_versions),
    }


class PinnedReleaseArtifactVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = Ed25519PrivateKey.generate()
        self.verifier = PinnedReleaseArtifactVerifier(
            {KEY_ID: self.private_key.public_key()},
            clock=lambda: datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc),
        )

    def _candidate(
        self,
        *,
        version: str = "2.0.0",
        build: int = 20,
        kind: ArtifactKind = ArtifactKind.UPDATE_PACKAGE,
        sources: tuple[str, ...] = ("1.0.0",),
        storage_key: str = "releases/2.0.0/macos/arm64/Jarvis.update",
    ) -> ArtifactVerificationCandidate:
        unsigned = ArtifactVerificationCandidate(
            product_id=PRODUCT_ID,
            bundle_id=BUNDLE_ID,
            release_version=version,
            platform="macos",
            architecture="arm64",
            artifact_kind=kind,
            build=build,
            sha256="ab" * 32,
            byte_size=4096,
            storage_key=storage_key,
            signature="A" * 86,
            signing_key_id=KEY_ID,
            compatible_source_versions=sources,
        )
        signature = _b64url(self.private_key.sign(_canonical(_payload(unsigned))))
        return replace(unsigned, signature=signature)

    def test_real_pinned_key_signature_returns_a_receipt(self) -> None:
        receipt = self.verifier.verify(self._candidate())

        self.assertEqual(receipt.verification_key_id, KEY_ID)
        self.assertEqual(receipt.verified_at, "2026-07-13T03:00:00.000000Z")
        self.assertNotIn(KEY_ID, repr(self.verifier))

    def test_tampering_and_wrong_or_missing_key_fail_with_sanitized_error(self) -> None:
        candidate = self._candidate()
        wrong_key = Ed25519PrivateKey.generate()
        cases = (
            (self.verifier, replace(candidate, sha256="cd" * 32)),
            (
                self.verifier,
                replace(candidate, storage_key="releases/other/Jarvis.update"),
            ),
            (
                PinnedReleaseArtifactVerifier(
                    {KEY_ID: wrong_key.public_key()},
                    clock=lambda: datetime.now(timezone.utc),
                ),
                candidate,
            ),
            (
                PinnedReleaseArtifactVerifier(
                    {"release-key-002": self.private_key.public_key()},
                    clock=lambda: datetime.now(timezone.utc),
                ),
                candidate,
            ),
        )
        for verifier, attempted in cases:
            with self.subTest(attempted=attempted.storage_key):
                with self.assertRaisesRegex(
                    ArtifactVerificationError,
                    "^release artifact verification failed$",
                ):
                    verifier.verify(attempted)

    def test_signed_kind_and_source_rules_are_still_enforced(self) -> None:
        invalid_candidates = (
            self._candidate(
                kind=ArtifactKind.INITIAL_INSTALLER,
                sources=("1.0.0",),
            ),
            self._candidate(kind=ArtifactKind.UPDATE_PACKAGE, sources=()),
            self._candidate(
                kind=ArtifactKind.UPDATE_PACKAGE,
                sources=("2.0.0",),
            ),
        )
        for candidate in invalid_candidates:
            with self.subTest(
                kind=candidate.artifact_kind,
                sources=candidate.compatible_source_versions,
            ), self.assertRaises(ArtifactVerificationError):
                self.verifier.verify(candidate)

    def test_real_verifier_drives_publish_entitlement_and_update_authorization(self) -> None:
        with SQLiteCommerceRepository(artifact_verifier=self.verifier) as repo:
            account = repo.create_account("buyer:pinned-verifier-001")
            license_record = repo.issue_license(account.id)
            repo.activate_device(
                license_record.id,
                FINGERPRINT,
                platform="macos",
                architecture="arm64",
            )
            release = repo.create_release(
                "2.0.0", price_minor=125_000, currency="UZS"
            )
            candidate = self._candidate()
            artifact = repo.add_release_artifact(
                release.id,
                platform=candidate.platform,
                architecture=candidate.architecture,
                artifact_kind=candidate.artifact_kind,
                build=candidate.build,
                sha256=candidate.sha256,
                byte_size=candidate.byte_size,
                storage_key=candidate.storage_key,
                signature=candidate.signature,
                signing_key_id=candidate.signing_key_id,
                compatible_source_versions=candidate.compatible_source_versions,
            )
            repo.publish_release(release.id)
            payment = repo.submit_payment(
                license_record.id,
                release.id,
                screenshot_storage_key="payments/pinned/evidence.png",
                screenshot_sha256="d" * 64,
                screenshot_byte_size=2048,
                screenshot_mime_type="image/png",
                paid_at="2026-07-13T02:59:00Z",
                client_submission_id="submission:pinned",
            )
            repo.start_payment_review(payment.id, admin_subject="admin:operator-001")
            repo.approve_payment(payment.id, admin_subject="admin:operator-001")

            authorization = repo.authorize_install(
                license_record.id,
                device_principal=VerifiedDevicePrincipal(
                    FINGERPRINT, "macos", "arm64", True
                ),
                artifact_id=artifact.id,
                install_mode=InstallMode.UPDATE,
                source_version="1.0.0",
                source_build=10,
            )

        self.assertTrue(authorization.allowed)

    def test_repository_does_not_persist_metadata_tampered_after_signing(self) -> None:
        with SQLiteCommerceRepository(artifact_verifier=self.verifier) as repo:
            release = repo.create_release(
                "2.0.0", price_minor=125_000, currency="UZS"
            )
            candidate = self._candidate()
            with self.assertRaises(ArtifactVerificationError):
                repo.add_release_artifact(
                    release.id,
                    platform=candidate.platform,
                    architecture=candidate.architecture,
                    artifact_kind=candidate.artifact_kind,
                    build=candidate.build,
                    sha256=candidate.sha256,
                    byte_size=candidate.byte_size,
                    storage_key="releases/tampered/Jarvis.update",
                    signature=candidate.signature,
                    signing_key_id=candidate.signing_key_id,
                    compatible_source_versions=candidate.compatible_source_versions,
                )
            with self.assertRaises(InvalidTransitionError):
                repo.publish_release(release.id)


if __name__ == "__main__":
    unittest.main()
