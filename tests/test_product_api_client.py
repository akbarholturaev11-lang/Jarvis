from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request

import core.product_api_client as api_module
from core.product_api_client import (
    ApiErrorCode,
    ProductApiClient,
    ProductApiError,
)


class FakeResponse:
    def __init__(
        self,
        raw: bytes,
        *,
        url: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._raw = raw
        self._offset = 0
        self._url = url
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._raw) - self._offset
        chunk = self._raw[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True

    def geturl(self) -> str:
        return self._url


class FakeTransport:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def open(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class ProductApiClientTests(unittest.TestCase):
    def test_redirect_policy_blocks_origin_changes_before_credentials_can_forward(self):
        handler = api_module._SafeRedirectHandler(allow_insecure_localhost=False)
        cross_origin_targets = (
            "https://evil.example.test/v1/check",
            "https://api.example.test:444/v1/check",
            "http://api.example.test/v1/check",
        )
        for code in (301, 302, 307, 308):
            for target in cross_origin_targets:
                with self.subTest(code=code, target=target), self.assertRaises(
                    ValueError
                ):
                    handler.redirect_request(
                        Request(
                            "https://api.example.test/v1/check",
                            headers={"X-Device-Grant": "private-grant"},
                        ),
                        None,
                        code,
                        "redirect",
                        {},
                        target,
                    )

        with self.assertRaises(ValueError):
            handler.redirect_request(
                Request(
                    "https://api.example.test/v1/check",
                    headers={"X-Device-Grant": "private-grant"},
                ),
                None,
                302,
                "redirect",
                {},
                "https://api.example.test/v1/other",
            )
        redirected = handler.redirect_request(
            Request("https://api.example.test/v1/check"),
            None,
            302,
            "redirect",
            {},
            "https://api.example.test/v1/other",
        )
        self.assertEqual(redirected.full_url, "https://api.example.test/v1/other")

    def test_loopback_redirect_cannot_change_port(self):
        handler = api_module._SafeRedirectHandler(allow_insecure_localhost=True)
        with self.assertRaises(ValueError):
            handler.redirect_request(
                Request("http://127.0.0.1:8000/start"),
                None,
                302,
                "redirect",
                {},
                "http://127.0.0.1:8001/target",
            )

    def test_tls_is_required_except_explicit_loopback_testing(self):
        for invalid in (
            "http://api.example.test",
            "http://192.168.1.10:8000",
            "ftp://api.example.test",
            "https://user:secret@api.example.test",
            "https://api.example.test?token=secret",
            "https://api.example.test/../admin",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ProductApiClient(invalid, transport=FakeTransport([]))

        local = ProductApiClient(
            "http://127.0.0.1:8080/api",
            allow_insecure_localhost=True,
            transport=FakeTransport([]),
        )
        self.assertNotIn("127.0.0.1", repr(local))
        with self.assertRaises(ValueError):
            ProductApiClient(
                "http://127.0.0.1:8080",
                transport=FakeTransport([]),
            )

    def test_json_request_is_bounded_canonical_and_closes_response(self):
        response = FakeResponse(
            b'{"ok":true}',
            url="https://api.example.test/v1/check",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": "11",
            },
        )
        transport = FakeTransport([response])
        client = ProductApiClient(
            "https://api.example.test",
            timeout_seconds=7,
            transport=transport,
        )

        result = client.request_json(
            "POST",
            "/v1/check",
            payload={"z": 2, "license_key": "private-license"},
        )

        self.assertEqual(result, {"ok": True})
        request = transport.requests[0]
        self.assertEqual(request["timeout_seconds"], 7.0)
        self.assertEqual(
            request["body"],
            b'{"license_key":"private-license","z":2}',
        )
        self.assertTrue(response.closed)
        self.assertNotIn("private-license", repr(client))

    def test_cross_origin_malformed_and_oversized_responses_fail_sanitized(self):
        cases = (
            FakeResponse(
                b"{}",
                url="https://evil.example.test/v1/check",
                headers={"Content-Type": "application/json"},
            ),
            FakeResponse(
                b"not-json",
                url="https://api.example.test/v1/check",
                headers={"Content-Type": "application/json"},
            ),
            FakeResponse(
                b"{}",
                url="https://api.example.test/v1/check",
                headers={"Content-Type": "text/html"},
            ),
            FakeResponse(
                b"{}",
                url="https://api.example.test/v1/check",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": "100",
                },
            ),
        )
        expected = (
            ApiErrorCode.RESPONSE_INVALID,
            ApiErrorCode.RESPONSE_INVALID,
            ApiErrorCode.RESPONSE_INVALID,
            ApiErrorCode.RESPONSE_TOO_LARGE,
        )
        for response, code in zip(cases, expected, strict=True):
            with self.subTest(code=code):
                client = ProductApiClient(
                    "https://api.example.test",
                    transport=FakeTransport([response]),
                )
                with self.assertRaises(ProductApiError) as raised:
                    client.request_json(
                        "GET",
                        "/v1/check",
                        maximum_response_bytes=16,
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("evil.example", str(raised.exception))
                self.assertTrue(response.closed)

    def test_request_path_cannot_escape_or_inject_query(self):
        client = ProductApiClient(
            "https://api.example.test",
            transport=FakeTransport([]),
        )
        for path in (
            "https://evil.example/test",
            "//evil.example/test",
            "/v1/../admin",
            "/v1/%2e%2e/admin",
            "/v1/check?token=secret",
        ):
            with self.subTest(path=path), self.assertRaises(ProductApiError):
                client.request_json("GET", path)

    def test_stream_download_uses_private_exclusive_file_and_digest(self):
        raw = b"signed artifact bytes" * 100
        response = FakeResponse(
            raw,
            url="https://api.example.test/v1/download",
            headers={"Content-Length": str(len(raw))},
        )
        client = ProductApiClient(
            "https://api.example.test",
            transport=FakeTransport([response]),
        )
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "artifact.part"
            receipt = client.download_to_file(
                "/v1/download",
                destination,
                maximum_bytes=len(raw),
            )

            self.assertEqual(destination.read_bytes(), raw)
            self.assertEqual(receipt.byte_size, len(raw))
            self.assertEqual(receipt.sha256, hashlib.sha256(raw).hexdigest())
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(str(destination), repr(receipt))

    def test_oversized_download_is_deleted(self):
        raw = b"x" * 17
        response = FakeResponse(
            raw,
            url="https://api.example.test/v1/download",
            headers={},
        )
        client = ProductApiClient(
            "https://api.example.test",
            transport=FakeTransport([response]),
        )
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "artifact.part"
            with self.assertRaises(ProductApiError) as raised:
                client.download_to_file(
                    "/v1/download",
                    destination,
                    maximum_bytes=16,
                )
            self.assertEqual(raised.exception.code, ApiErrorCode.RESPONSE_TOO_LARGE)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
