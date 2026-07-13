from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.device_identity import DeviceIdentity
from core.product_version import PRODUCT_ID
from product_backend.api_activation import (
    ActivationDeviceMismatchError,
    SQLiteClientActivationService,
)
from product_backend.api_ports import EntitlementCertificateSigner
from product_backend.models import ConflictError
from product_backend.repository import CommerceRepository


class ProductBackendActivationTests(unittest.TestCase):
    def test_device_mismatch_does_not_consume_activation_credential(self) -> None:
        commerce = Mock(spec=CommerceRepository)
        commerce.get_entitlement.return_value = object()
        commerce.activate_device.side_effect = ConflictError(
            "private device detail must not escape"
        )
        signer = Mock(spec=EntitlementCertificateSigner)
        device = DeviceIdentity(Ed25519PrivateKey.generate())
        with tempfile.TemporaryDirectory() as temp:
            service = SQLiteClientActivationService(
                commerce,
                signer,
                b"activation-pepper-for-tests-32-bytes-long",
                Path(temp) / "activation.sqlite3",
                clock=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
            )
            try:
                issued = service.issue_activation_credential(
                    license_id="license_test_001",
                    version="1.0.0",
                )
                challenge = service.create_activation_challenge(
                    product_id=PRODUCT_ID,
                    license_key=issued.license_key,
                    device_key_fingerprint=device.fingerprint,
                    device_public_key=device.public_key_base64,
                    version="1.0.0",
                    platform="macos",
                    architecture="arm64",
                )
                with self.assertRaises(ActivationDeviceMismatchError):
                    service.complete_activation(
                        product_id=PRODUCT_ID,
                        challenge_id=challenge.challenge_id,
                        challenge_nonce=challenge.challenge_nonce,
                        device_key_fingerprint=device.fingerprint,
                        device_public_key=device.public_key_base64,
                        challenge_signature=device.sign_challenge(
                            challenge.challenge_nonce
                        ),
                        version="1.0.0",
                        platform="macos",
                        architecture="arm64",
                    )

                # A new challenge with the same one-time credential proves the
                # credential was not wasted before admin device replacement.
                retried = service.create_activation_challenge(
                    product_id=PRODUCT_ID,
                    license_key=issued.license_key,
                    device_key_fingerprint=device.fingerprint,
                    device_public_key=device.public_key_base64,
                    version="1.0.0",
                    platform="macos",
                    architecture="arm64",
                )
                self.assertNotEqual(retried.challenge_id, challenge.challenge_id)
                signer.sign_entitlement_certificate.assert_not_called()
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
