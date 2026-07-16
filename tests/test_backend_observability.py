from __future__ import annotations

import io
import json
import logging
import unittest

from product_backend.observability import (
    InMemoryMetrics,
    JsonLogFormatter,
    NullMetrics,
    configure_json_logging,
    new_request_id,
    record_request_metric,
    redact_mapping,
    redact_text,
    resolve_request_id,
    sanitize_request_id,
)


class RedactionTests(unittest.TestCase):
    def test_secret_keys_are_masked_in_mappings(self) -> None:
        payload = {
            "subject": "admin:ops",
            "password": "hunter2-super-secret",
            "session_secret": "abcdefghijklmnopqrstuvwxyz012345",
            "nested": {"authorization": "Bearer abc", "note": "ok"},
            "list": [{"api_key": "zzz"}, "plain"],
        }
        cleaned = redact_mapping(payload)
        self.assertEqual(cleaned["subject"], "admin:ops")
        self.assertEqual(cleaned["password"], "***")
        self.assertEqual(cleaned["session_secret"], "***")
        self.assertEqual(cleaned["nested"]["authorization"], "***")
        self.assertEqual(cleaned["nested"]["note"], "ok")
        self.assertEqual(cleaned["list"][0]["api_key"], "***")
        self.assertEqual(cleaned["list"][1], "plain")

    def test_inline_secrets_and_opaque_tokens_are_masked_in_text(self) -> None:
        text = "login authorization=Bearer_ABCDEF token: qwertY123"
        cleaned = redact_text(text)
        self.assertNotIn("Bearer_ABCDEF", cleaned)
        self.assertIn("authorization=***", cleaned)
        # A long opaque base64url run is scrubbed even without a key hint.
        long_token = "A" * 40
        self.assertEqual(redact_text(f"value {long_token} end"), "value *** end")

    def test_depth_is_bounded(self) -> None:
        node: dict = {}
        current = node
        for _ in range(20):
            child: dict = {}
            current["child"] = child
            current = child
        # Should not raise or recurse without bound.
        redact_mapping(node)


class CorrelationIdTests(unittest.TestCase):
    def test_sanitize_rejects_unsafe_and_accepts_safe(self) -> None:
        self.assertIsNone(sanitize_request_id("short"))
        self.assertIsNone(sanitize_request_id("bad id with spaces"))
        self.assertIsNone(sanitize_request_id("x" * 200))
        self.assertIsNone(sanitize_request_id(1234))
        self.assertEqual(sanitize_request_id("req_abc12345"), "req_abc12345")

    def test_resolve_falls_back_to_generated_id(self) -> None:
        generated = resolve_request_id("!!bad!!")
        self.assertTrue(generated.startswith("req_"))
        self.assertEqual(resolve_request_id("keep_this_one_1234"), "keep_this_one_1234")
        self.assertNotEqual(new_request_id(), new_request_id())


class JsonLoggingTests(unittest.TestCase):
    def test_formatter_emits_redacted_json_with_extras(self) -> None:
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="jarvis.backend.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="request token=SUPERSECRETTOKENVALUE0001",
            args=(),
            exc_info=None,
        )
        record.request_id = "req_1234abcd"
        record.status = 200
        record.method = "GET"
        record.path = "/api/releases"
        line = formatter.format(record)
        payload = json.loads(line)
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["request_id"], "req_1234abcd")
        self.assertEqual(payload["status"], 200)
        self.assertNotIn("SUPERSECRETTOKENVALUE0001", line)

    def test_configure_json_logging_is_idempotent(self) -> None:
        stream = io.StringIO()
        logger = configure_json_logging(name="jarvis.test.logger", stream=stream)
        configure_json_logging(name="jarvis.test.logger", stream=stream)
        self.assertEqual(len(logger.handlers), 1)
        logger.info("hello", extra={"request_id": "req_aaaaaaaa"})
        emitted = json.loads(stream.getvalue().strip())
        self.assertEqual(emitted["message"], "hello")
        self.assertEqual(emitted["request_id"], "req_aaaaaaaa")


class MetricsTests(unittest.TestCase):
    def test_counter_and_prometheus_rendering(self) -> None:
        metrics = InMemoryMetrics()
        record_request_metric(metrics, method="get", status=200)
        record_request_metric(metrics, method="POST", status=503)
        record_request_metric(metrics, method="POST", status=502)
        rendered = metrics.render_prometheus()
        self.assertIn("# TYPE jarvis_backend_requests_total counter", rendered)
        self.assertIn('method="GET",status="2xx"', rendered)
        self.assertIn('method="POST",status="5xx"', rendered)
        # The two 5xx POST responses share one series with value 2.
        self.assertIn('method="POST",status="5xx"} 2', rendered)

    def test_series_are_bounded(self) -> None:
        metrics = InMemoryMetrics(max_series=16)
        for index in range(100):
            metrics.increment("jarvis_test_total", {"n": str(index)})
        rendered = metrics.render_prometheus()
        self.assertLessEqual(rendered.count("jarvis_test_total{"), 16)

    def test_null_metrics_records_nothing(self) -> None:
        metrics = NullMetrics()
        record_request_metric(metrics, method="GET", status=200)
        self.assertEqual(metrics.render_prometheus(), "")


if __name__ == "__main__":
    unittest.main()
