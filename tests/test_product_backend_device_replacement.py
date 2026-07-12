from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from product_backend.api_app import create_product_backend_app
from product_backend.api_auth import AdminAuthSettings, AdminPasswordCredential
from product_backend.api_ports import (
    ClientActivationPort,
    DeviceChallengePort,
    PrivatePaymentEvidenceStore,
    ReleaseArtifactStore,
)
from product_backend.api_queries import SQLiteProductReadStore
from product_backend.sqlite_repository import SQLiteCommerceRepository


NOW = datetime(2026, 7, 13, 3, 0, tzinfo=timezone.utc)
OLD_FINGERPRINT = "sha256:" + ("a" * 64)
NEW_FINGERPRINT = "sha256:" + ("b" * 64)


class AdminDeviceReplacementApiTests(unittest.TestCase):
    def test_admin_replacement_is_atomic_historical_and_not_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            database = root / "commerce.sqlite3"
            commerce = SQLiteCommerceRepository(database, clock=lambda: NOW)
            account = commerce.create_account("buyer:device-replacement")
            license_record = commerce.issue_license(account.id)
            old_binding = commerce.activate_device(
                license_record.id,
                OLD_FINGERPRINT,
                platform="macos",
                architecture="arm64",
                device_label="Old Mac",
            )
            credential = AdminPasswordCredential.derive_for_configuration(
                subject="admin:device-replacement",
                password="strong-test-password",
                salt=b"s" * 32,
            )
            app = create_product_backend_app(
                commerce=commerce,
                reads=SQLiteProductReadStore(database),
                evidence_store=Mock(spec=PrivatePaymentEvidenceStore),
                challenges=Mock(spec=DeviceChallengePort),
                activation=Mock(spec=ClientActivationPort),
                release_artifact_store=Mock(spec=ReleaseArtifactStore),
                auth_settings=AdminAuthSettings(
                    (credential,),
                    b"device-replacement-session-secret-32b",
                    ("testserver",),
                    secure_cookie=False,
                ),
                clock=lambda: NOW,
            )
            route = (
                f"/api/admin/licenses/{license_record.id}/devices/replace"
            )
            body = {
                "current_device_key_fingerprint": OLD_FINGERPRINT,
                "new_device_key_fingerprint": NEW_FINGERPRINT,
                "new_platform": "windows",
                "new_architecture": "x86_64",
                "new_device_label": "Replacement PC",
                "replacement_reason": "Owner verified a computer replacement",
            }
            try:
                with TestClient(app) as client:
                    unauthenticated = client.post(route, json=body)
                    self.assertEqual(unauthenticated.status_code, 401)

                    login = client.post(
                        "/api/admin/session",
                        json={
                            "subject": "admin:device-replacement",
                            "password": "strong-test-password",
                        },
                    )
                    self.assertEqual(login.status_code, 200)
                    headers = {"X-CSRF-Token": login.json()["csrf_token"]}

                    without_csrf = client.post(route, json=body)
                    self.assertEqual(without_csrf.status_code, 403)

                    invalid = client.post(
                        route,
                        headers=headers,
                        json={
                            **body,
                            "new_device_key_fingerprint": OLD_FINGERPRINT,
                        },
                    )
                    too_long = client.post(
                        route,
                        headers=headers,
                        json={**body, "replacement_reason": "x" * 241},
                    )
                    self.assertEqual(invalid.status_code, 400)
                    self.assertEqual(too_long.status_code, 422)
                    self.assertEqual(
                        commerce.get_active_device(license_record.id),
                        old_binding,
                    )
                    self.assertEqual(
                        len(commerce.list_device_history(license_record.id)),
                        1,
                    )

                    replaced = client.post(route, headers=headers, json=body)
                    self.assertEqual(replaced.status_code, 201)
                    payload = replaced.json()
                    self.assertEqual(
                        frozenset(payload),
                        {
                            "status",
                            "device_binding_id",
                            "license_id",
                            "device_key_fingerprint",
                            "platform",
                            "architecture",
                            "device_label",
                            "activated_at",
                        },
                    )
                    self.assertEqual(payload["status"], "replaced")
                    self.assertEqual(
                        payload["device_key_fingerprint"],
                        NEW_FINGERPRINT,
                    )
                    self.assertNotIn("replacement_reason", payload)
                    self.assertNotIn(
                        body["replacement_reason"],
                        replaced.text,
                    )

                    history = commerce.list_device_history(license_record.id)
                    active = commerce.get_active_device(license_record.id)
                    self.assertEqual(len(history), 2)
                    self.assertEqual(history[0].id, old_binding.id)
                    self.assertFalse(history[0].is_active)
                    self.assertEqual(
                        history[0].replaced_by_binding_id,
                        payload["device_binding_id"],
                    )
                    self.assertEqual(
                        history[0].replacement_reason,
                        body["replacement_reason"],
                    )
                    self.assertTrue(history[1].is_active)
                    self.assertEqual(active, history[1])
                    self.assertEqual(
                        active.device_key_fingerprint,
                        NEW_FINGERPRINT,
                    )

                    replay = client.post(route, headers=headers, json=body)
                    self.assertEqual(replay.status_code, 409)
                    replay_history = commerce.list_device_history(
                        license_record.id
                    )
                    self.assertEqual(replay_history, history)
                    self.assertEqual(
                        commerce.get_active_device(license_record.id),
                        active,
                    )
            finally:
                commerce.close()


if __name__ == "__main__":
    unittest.main()
