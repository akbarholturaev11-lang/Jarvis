from __future__ import annotations

import ast
import unittest
from pathlib import Path

from core import i18n


ROOT = Path(__file__).resolve().parents[1]


def _function(source: str, class_name: str, function_name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == function_name:
                    return item
    raise AssertionError(f"{class_name}.{function_name} was not found")


class ProductGateUiContractTests(unittest.TestCase):
    def test_all_product_gate_ui_strings_exist_in_english_and_russian(self) -> None:
        messages = i18n._MESSAGES
        english = {key for key in messages["en"] if key.startswith("product.gate.")}
        russian = {key for key in messages["ru"] if key.startswith("product.gate.")}
        self.assertEqual(english, russian)
        self.assertGreaterEqual(len(english), 20)
        for language in ("en", "ru"):
            for key in english:
                self.assertTrue(messages[language][key].strip(), (language, key))

    def test_all_update_ui_strings_exist_in_english_and_russian(self) -> None:
        messages = i18n._MESSAGES
        prefixes = ("settings.update_", "product.update_recovery_")
        english = {
            key
            for key in messages["en"]
            if key == "settings.install_update"
            or key.startswith(prefixes)
        }
        russian = {
            key
            for key in messages["ru"]
            if key == "settings.install_update"
            or key.startswith(prefixes)
        }
        self.assertEqual(english, russian)
        self.assertGreaterEqual(len(english), 12)
        for language in ("en", "ru"):
            for key in english:
                self.assertTrue(messages[language][key].strip(), (language, key))

    def test_main_defers_gemini_and_uses_bootstrap_coordinator(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        main_function = ast.get_source_segment(
            source,
            next(
                node
                for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == "main"
            ),
        )
        self.assertIsNotNone(main_function)
        assert main_function is not None
        self.assertIn("defer_gemini_onboarding=True", main_function)
        self.assertIn("ProductLicenseGate", main_function)
        self.assertIn("ProductBootstrapCoordinator", main_function)
        self.assertNotIn("wait_for_packaged_entitlement", source)

    def test_deferred_window_is_inert_before_first_gate_signal(self) -> None:
        source = (ROOT / "ui.py").read_text(encoding="utf-8")
        constructor = ast.unparse(_function(source, "MainWindow", "__init__"))
        deferred_branch = constructor[constructor.index("if defer_gemini_onboarding") :]
        self.assertIn("self.centralWidget().setEnabled(False)", deferred_branch)
        self.assertIn("self._gear_btn.setEnabled(False)", deferred_branch)

    def test_waits_use_events_and_activation_secret_is_cleared_before_emit(self) -> None:
        source = (ROOT / "ui.py").read_text(encoding="utf-8")
        wait_api = ast.unparse(_function(source, "JarvisUI", "wait_for_api_key"))
        wait_license = ast.unparse(
            _function(source, "JarvisUI", "wait_for_product_gate")
        )
        self.assertIn("event = self._win._api_ready_event", wait_api)
        self.assertIn("event = self._win._product_gate_event", wait_license)
        self.assertIn("event.wait()", wait_api)
        self.assertIn("event.wait()", wait_license)
        self.assertLess(
            wait_api.index("_bootstrap_cancelled"),
            wait_api.index("_api_ready_event"),
        )
        self.assertLess(
            wait_license.index("_bootstrap_cancelled"),
            wait_license.index("_product_gate_event"),
        )
        self.assertNotIn("sleep", wait_api + wait_license)

        submit = ast.unparse(
            _function(source, "ProductGateOverlay", "_submit_activation")
        )
        self.assertLess(submit.index("_key_input.clear()"), submit.index(".emit(key)"))
        self.assertIn("QLineEdit.EchoMode.Password", source)
        self.assertIn("_key_input.setMaxLength(256)", source)
        self.assertIn("with self._win._bootstrap_lock", source)

    def test_purchase_flow_is_server_backed_sanitized_and_bounded(self) -> None:
        source = (ROOT / "ui.py").read_text(encoding="utf-8")
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        select_payment = ast.unparse(
            _function(source, "MainWindow", "_select_initial_payment")
        )
        schedule = ast.unparse(
            _function(source, "MainWindow", "_schedule_payment_poll")
        )
        payment_result = ast.unparse(
            _function(source, "ProductGateOverlay", "apply_payment_result")
        )
        gate_constructor = ast.unparse(
            _function(source, "ProductGateOverlay", "__init__")
        )
        settings_build = ast.unparse(_function(source, "SettingsOverlay", "_build"))
        self.assertIn("prepare_payment_evidence(selected)", select_payment)
        self.assertIn("evidence.content", select_payment)
        self.assertNotIn("read_bytes", select_payment)
        self.assertIn("_payment_poll_delays", schedule)
        self.assertNotIn("while", schedule)
        self.assertIn("product.gate.payment_rejected", payment_result)
        self.assertIn(
            "_status.setTextFormat(Qt.TextFormat.PlainText)", gate_constructor
        )
        self.assertIn(
            "_product_lbl.setTextFormat(Qt.TextFormat.PlainText)", settings_build
        )
        self.assertIn("setPlainText", source)
        self.assertIn("product_runtime.prepare_initial_purchase", main_source)
        self.assertIn("product_runtime.submit_initial_purchase", main_source)
        self.assertIn("product_runtime.poll_initial_purchase", main_source)

    def test_purchase_screen_renders_server_release_and_payment_fields(self) -> None:
        source = (ROOT / "ui.py").read_text(encoding="utf-8")
        constructor = ast.unparse(
            _function(source, "ProductGateOverlay", "__init__")
        )
        offer_renderer = ast.unparse(
            _function(source, "ProductGateOverlay", "apply_purchase_offer")
        )
        for required in (
            "self._purchase_details = QTextEdit()",
            "self._purchase = QPushButton(t('product.gate.purchase'))",
            "self._upload_payment = QPushButton(t('product.gate.upload_payment'))",
        ):
            with self.subTest(required=required):
                self.assertIn(required, constructor)
        for required in (
            "features_",
            "fixes_",
            "price_minor",
            "currency",
            "method_",
            "instructions_",
            "recipient",
            "product.gate.offer",
            "product.gate.features",
            "product.gate.fixes",
            "product.gate.payment_destination",
            "product.gate.payment_steps",
            "self._purchase_details.setPlainText",
            "self._upload_payment.setEnabled(configured)",
        ):
            with self.subTest(required=required):
                self.assertIn(required, offer_renderer)

    def test_update_install_is_explicit_and_startup_recovery_precedes_gate(self) -> None:
        source = (ROOT / "ui.py").read_text(encoding="utf-8")
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        settings_build = ast.unparse(_function(source, "SettingsOverlay", "_build"))
        result_handler = ast.unparse(
            _function(source, "SettingsOverlay", "_on_product_result")
        )
        self.assertIn("settings.install_update", settings_build)
        self.assertIn("install_product_update", result_handler)
        self.assertIn("settings.update_rollback_required", result_handler)
        self.assertIn("recover_interrupted_update(product_runtime)", main_source)
        self.assertLess(
            main_source.index("recovery = recover_interrupted_update(product_runtime)"),
            main_source.index("prepared = bootstrap.prepare"),
        )
        self.assertLess(
            main_source.index("if not recovery.may_start"),
            main_source.index("prepared = bootstrap.prepare"),
        )


if __name__ == "__main__":
    unittest.main()
