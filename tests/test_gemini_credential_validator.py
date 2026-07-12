from __future__ import annotations

import unittest

from core.gemini_credential_validator import (
    STATUS_INVALID,
    STATUS_NETWORK_UNAVAILABLE,
    STATUS_SERVER_UNAVAILABLE,
    STATUS_SUCCESS,
    validate_gemini_api_key,
)


class ApiFailure(RuntimeError):
    def __init__(self, code: int) -> None:
        self.code = code


class ResponseFailure(RuntimeError):
    def __init__(self, code: int) -> None:
        self.response = type("Response", (), {"status_code": code})()


class GeminiCredentialValidatorTests(unittest.TestCase):
    def test_success_uses_probe_and_result_does_not_expose_key(self) -> None:
        seen = []
        key = "A" * 32
        result = validate_gemini_api_key(key, probe=seen.append)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(seen, [key])
        self.assertNotIn(key, repr(result))

    def test_bad_shape_and_auth_rejection_are_invalid(self) -> None:
        self.assertEqual(
            validate_gemini_api_key("short", probe=lambda _key: None).status,
            STATUS_INVALID,
        )
        for code in (400, 401, 403):
            with self.subTest(code=code):
                result = validate_gemini_api_key(
                    "A" * 32,
                    probe=lambda _key, c=code: (_ for _ in ()).throw(ApiFailure(c)),
                )
                self.assertEqual(result.status, STATUS_INVALID)
        response_result = validate_gemini_api_key(
            "A" * 32,
            probe=lambda _key: (_ for _ in ()).throw(ResponseFailure(401)),
        )
        self.assertEqual(response_result.status, STATUS_INVALID)

    def test_network_and_server_failures_are_honest(self) -> None:
        network = validate_gemini_api_key(
            "A" * 32,
            probe=lambda _key: (_ for _ in ()).throw(TimeoutError()),
        )
        server = validate_gemini_api_key(
            "A" * 32,
            probe=lambda _key: (_ for _ in ()).throw(RuntimeError()),
        )
        self.assertEqual(network.status, STATUS_NETWORK_UNAVAILABLE)
        self.assertEqual(server.status, STATUS_SERVER_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
