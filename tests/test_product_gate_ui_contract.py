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

    def test_purchase_entry_is_explicitly_honest_until_stage_three(self) -> None:
        source = (ROOT / "ui.py").read_text(encoding="utf-8")
        purchase = ast.unparse(
            _function(source, "ProductGateOverlay", "_show_purchase_entry")
        )
        self.assertIn("product.gate.purchase_not_available", purchase)
        self.assertNotIn("success", purchase.casefold())


if __name__ == "__main__":
    unittest.main()
