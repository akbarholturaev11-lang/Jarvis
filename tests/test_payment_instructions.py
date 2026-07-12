from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from product_backend.payment_instructions import (
    MAX_INSTRUCTIONS_TEXT_LENGTH,
    MAX_PAYMENT_INSTRUCTIONS_BYTES,
    MAX_RECIPIENT_TEXT_LENGTH,
    PAYMENT_INSTRUCTIONS_SCHEMA,
    LocalizedPaymentText,
    PaymentInstructions,
    STATUS_CONFIGURED,
    STATUS_NOT_CONFIGURED,
    load_payment_instructions,
)


def _document() -> dict[str, object]:
    return {
        "schema": PAYMENT_INSTRUCTIONS_SCHEMA,
        "recipient": "Example private recipient",
        "method": {
            "en": "Private test payment method",
            "ru": "Закрытый тестовый способ оплаты",
        },
        "instructions": {
            "en": "Use the private test instructions supplied outside this repository.",
            "ru": "Используйте закрытые тестовые инструкции вне этого репозитория.",
        },
    }


def _write_private(path: Path, document: object) -> Path:
    path.write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


class PaymentInstructionsNotConfiguredTests(unittest.TestCase):
    def test_optional_missing_and_relative_paths_are_explicitly_not_configured(self):
        cases = (None, "relative-payment.json", Path("also-relative.json"))
        for supplied in cases:
            with self.subTest(supplied=supplied):
                result = load_payment_instructions(supplied)
                self.assertEqual(result.status, STATUS_NOT_CONFIGURED)
                self.assertFalse(result.configured)
                self.assertIsNone(result.instructions)
                self.assertNotIn(str(supplied), repr(result))

    def test_direct_construction_cannot_bypass_text_validation(self):
        with self.assertRaises(ValueError):
            PaymentInstructions(
                PAYMENT_INSTRUCTIONS_SCHEMA,
                "TEST-RECIPIENT-NOT-REAL",
                LocalizedPaymentText("Test method", "Тестовый способ"),
                LocalizedPaymentText(
                    "token=placeholder-sensitive-value",
                    "Тестовая инструкция",
                ),
            )


@unittest.skipUnless(
    os.name == "posix" and hasattr(os, "O_NOFOLLOW"),
    "owner-only no-follow payment config requires POSIX",
)
class PrivatePaymentInstructionsTests(unittest.TestCase):
    def test_valid_private_bilingual_configuration_is_loaded_and_redacted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = _write_private(
                Path(temporary).resolve() / "payment-instructions.json",
                _document(),
            )
            result = load_payment_instructions(path)

        self.assertEqual(result.status, STATUS_CONFIGURED)
        self.assertTrue(result.configured)
        self.assertIsNotNone(result.instructions)
        instructions = result.instructions
        assert instructions is not None
        self.assertEqual(instructions.schema, PAYMENT_INSTRUCTIONS_SCHEMA)
        self.assertEqual(instructions.recipient, "Example private recipient")
        self.assertEqual(instructions.method.en, "Private test payment method")
        self.assertTrue(instructions.method.ru)
        self.assertTrue(instructions.instructions.en)
        for sensitive in (
            instructions.recipient,
            instructions.method.en,
            instructions.method.ru,
            instructions.instructions.en,
            instructions.instructions.ru,
        ):
            self.assertNotIn(sensitive, repr(instructions))
            self.assertNotIn(sensitive, repr(result))
        self.assertIn("<redacted>", repr(instructions))

    def test_permissions_symlinks_and_multiple_hardlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()

            loose = _write_private(root / "loose.json", _document())
            loose.chmod(0o640)
            self.assertEqual(
                load_payment_instructions(loose).status,
                STATUS_NOT_CONFIGURED,
            )

            target = _write_private(root / "target.json", _document())
            symbolic = root / "symbolic.json"
            symbolic.symlink_to(target)
            self.assertEqual(
                load_payment_instructions(symbolic).status,
                STATUS_NOT_CONFIGURED,
            )

            real_parent = root / "real-parent"
            real_parent.mkdir(mode=0o700)
            parent_target = _write_private(
                real_parent / "payment.json",
                _document(),
            )
            self.assertTrue(parent_target.is_file())
            parent_link = root / "linked-parent"
            parent_link.symlink_to(real_parent, target_is_directory=True)
            self.assertEqual(
                load_payment_instructions(
                    parent_link / "payment.json"
                ).status,
                STATUS_NOT_CONFIGURED,
            )

            linked = _write_private(root / "linked.json", _document())
            os.link(linked, root / "second-link.json")
            self.assertEqual(
                load_payment_instructions(linked).status,
                STATUS_NOT_CONFIGURED,
            )

    def test_strict_schema_duplicates_and_size_bound_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cases: list[object] = []

            extra = _document()
            extra["unexpected"] = True
            cases.append(extra)

            wrong_schema = _document()
            wrong_schema["schema"] = "jarvis.payment-instructions.v2"
            cases.append(wrong_schema)

            missing_language = _document()
            missing_language["method"] = {"en": "Private test method"}
            cases.append(missing_language)

            cases.append(["not", "an", "object"])

            for index, document in enumerate(cases):
                with self.subTest(index=index):
                    path = _write_private(root / f"invalid-{index}.json", document)
                    self.assertEqual(
                        load_payment_instructions(path).status,
                        STATUS_NOT_CONFIGURED,
                    )

            duplicate = root / "duplicate.json"
            duplicate.write_text(
                "{"
                f'"schema":"{PAYMENT_INSTRUCTIONS_SCHEMA}",'
                f'"schema":"{PAYMENT_INSTRUCTIONS_SCHEMA}",'
                '"recipient":"Example private recipient",'
                '"method":{"en":"Private method","ru":"Тестовый способ"},'
                '"instructions":{"en":"Private instructions.",'
                '"ru":"Тестовые инструкции."}'
                "}",
                encoding="utf-8",
            )
            duplicate.chmod(0o600)
            self.assertEqual(
                load_payment_instructions(duplicate).status,
                STATUS_NOT_CONFIGURED,
            )

            oversized = root / "oversized.json"
            oversized.write_bytes(b"x" * (MAX_PAYMENT_INSTRUCTIONS_BYTES + 1))
            oversized.chmod(0o600)
            self.assertEqual(
                load_payment_instructions(oversized).status,
                STATUS_NOT_CONFIGURED,
            )

    def test_control_markup_secrets_and_text_bounds_fail_closed(self):
        unsafe_documents: list[dict[str, object]] = []

        control = _document()
        control["recipient"] = "Example\nrecipient"
        unsafe_documents.append(control)

        markup = _document()
        markup["method"] = {
            "en": "<strong>Private method</strong>",
            "ru": "Тестовый способ",
        }
        unsafe_documents.append(markup)

        markdown = _document()
        markdown["instructions"] = {
            "en": "Open [private details](https://example.invalid).",
            "ru": "Используйте тестовые инструкции.",
        }
        unsafe_documents.append(markdown)

        emphasis = _document()
        emphasis["method"] = {
            "en": "*Private method*",
            "ru": "Тестовый способ",
        }
        unsafe_documents.append(emphasis)

        secret = _document()
        secret["instructions"] = {
            "en": "token=placeholder-sensitive-value",
            "ru": "Используйте тестовые инструкции.",
        }
        unsafe_documents.append(secret)

        bearer = _document()
        bearer["instructions"] = {
            "en": "Bearer placeholder-sensitive-value",
            "ru": "Используйте тестовые инструкции.",
        }
        unsafe_documents.append(bearer)

        long_recipient = _document()
        long_recipient["recipient"] = "r" * (MAX_RECIPIENT_TEXT_LENGTH + 1)
        unsafe_documents.append(long_recipient)

        long_instructions = _document()
        long_instructions["instructions"] = {
            "en": "i" * (MAX_INSTRUCTIONS_TEXT_LENGTH + 1),
            "ru": "Используйте тестовые инструкции.",
        }
        unsafe_documents.append(long_instructions)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for index, document in enumerate(unsafe_documents):
                with self.subTest(index=index):
                    path = _write_private(root / f"unsafe-{index}.json", document)
                    result = load_payment_instructions(path)
                    self.assertEqual(result.status, STATUS_NOT_CONFIGURED)
                    self.assertIsNone(result.instructions)


if __name__ == "__main__":
    unittest.main()
