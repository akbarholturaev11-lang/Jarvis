from __future__ import annotations

import unittest

from core.credential_service import CredentialResult
from core.gemini_credential_onboarding import validate_and_store_gemini_api_key
from core.gemini_credential_validator import GeminiCredentialValidationResult
from core.secure_store import STATUS_FAILED, STATUS_SUCCESS


class GeminiCredentialOnboardingTests(unittest.TestCase):
    def test_invalid_key_never_reaches_storage(self) -> None:
        stored: list[str] = []
        result = validate_and_store_gemini_api_key(
            "invalid-secret",
            validator=lambda _value: GeminiCredentialValidationResult("invalid"),
            storage=lambda value: (
                stored.append(value) or CredentialResult(STATUS_SUCCESS)
            ),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "validation")
        self.assertEqual(result.status, "invalid")
        self.assertEqual(stored, [])
        self.assertNotIn("invalid-secret", repr(result))

    def test_storage_failure_remains_distinct_from_validation(self) -> None:
        result = validate_and_store_gemini_api_key(
            "valid-shaped-secret",
            validator=lambda _value: GeminiCredentialValidationResult("success"),
            storage=lambda _value: CredentialResult(STATUS_FAILED),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.phase, "storage")
        self.assertEqual(result.status, STATUS_FAILED)

    def test_success_requires_both_phases(self) -> None:
        result = validate_and_store_gemini_api_key(
            "valid-shaped-secret",
            validator=lambda _value: GeminiCredentialValidationResult("success"),
            storage=lambda _value: CredentialResult(STATUS_SUCCESS),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.phase, "storage")
        self.assertEqual(result.status, STATUS_SUCCESS)


if __name__ == "__main__":
    unittest.main()
