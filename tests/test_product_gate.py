from __future__ import annotations

import threading
import time
import unittest

from core.product_gate import (
    ENV_DEVELOPMENT_LICENSE_BYPASS,
    STATUS_ACTIVATION_REQUIRED,
    STATUS_ALLOWED,
    STATUS_DEVELOPMENT_BYPASS,
    STATUS_DEVICE_MISMATCH,
    STATUS_INVALID,
    STATUS_NOT_CONFIGURED,
    ProductLicenseGate,
    development_bypass_requested,
)
from core.product_runtime import (
    STATUS_ENTITLED,
    STATUS_NOT_ACTIVATED,
    STATUS_NOT_CONFIGURED as PRODUCT_NOT_CONFIGURED,
    LocalProductState,
    ProductActivationOutcome,
)
from core.product_version import ProductVersion
from core.runtime_product import RuntimeProductIdentity


class FakeProductRuntime:
    def __init__(self, state: LocalProductState, *, packaged_expected=False) -> None:
        self.state = state
        self.packaged_runtime_expected = packaged_expected
        self.activation_status = STATUS_ENTITLED
        self.activation_changes_state = True
        self.purchase_available = True
        self.pending_purchase = None
        self.local_state_calls = 0
        self.keys: list[str] = []

    def local_state(self) -> LocalProductState:
        self.local_state_calls += 1
        return self.state

    def activate(self, license_key: str) -> ProductActivationOutcome:
        self.keys.append(license_key)
        if self.activation_status == STATUS_ENTITLED and self.activation_changes_state:
            self.state = LocalProductState(
                STATUS_ENTITLED,
                self.state.runtime,
                "license_test_001",
            )
        return ProductActivationOutcome(self.activation_status)

    def device_fingerprint(self) -> str | None:
        return "sha256:" + ("a" * 64)

    def pending_initial_purchase(self):
        return self.pending_purchase


def _runtime(*, packaged: bool) -> RuntimeProductIdentity:
    return RuntimeProductIdentity(
        ProductVersion.parse("1.2.3", 7),
        "macos",
        "arm64",
        packaged,
    )


class ProductLicenseGateTests(unittest.TestCase):
    def test_packaged_no_license_is_gated_and_valid_license_is_allowed(self) -> None:
        runtime = FakeProductRuntime(
            LocalProductState(STATUS_NOT_ACTIVATED, _runtime(packaged=True)),
            packaged_expected=True,
        )
        gate = ProductLicenseGate(runtime, development_override=False)
        missing = gate.evaluate()
        self.assertEqual(missing.status, STATUS_ACTIVATION_REQUIRED)
        self.assertFalse(missing.allowed)
        self.assertEqual(missing.version, "1.2.3")
        self.assertEqual(missing.build, 7)
        self.assertTrue(missing.purchase_available)
        self.assertFalse(missing.purchase_pending)

        runtime.state = LocalProductState(
            STATUS_ENTITLED,
            _runtime(packaged=True),
            "license_test_001",
        )
        entitled = gate.evaluate()
        self.assertEqual(entitled.status, STATUS_ALLOWED)
        self.assertTrue(entitled.allowed)
        self.assertFalse(entitled.purchase_available)

    def test_packaged_runtime_ignores_explicit_development_override(self) -> None:
        runtime = FakeProductRuntime(
            LocalProductState(STATUS_NOT_ACTIVATED, _runtime(packaged=True)),
            packaged_expected=True,
        )
        decision = ProductLicenseGate(
            runtime,
            development_override=True,
        ).evaluate()
        self.assertEqual(decision.status, STATUS_ACTIVATION_REQUIRED)
        self.assertFalse(decision.allowed)

    def test_source_requires_exact_explicit_override(self) -> None:
        runtime = FakeProductRuntime(
            LocalProductState(STATUS_NOT_ACTIVATED, _runtime(packaged=False))
        )
        self.assertEqual(
            ProductLicenseGate(runtime, environ={}).evaluate().status,
            STATUS_ACTIVATION_REQUIRED,
        )
        overridden = ProductLicenseGate(
            runtime,
            environ={ENV_DEVELOPMENT_LICENSE_BYPASS: "1"},
        ).evaluate()
        self.assertEqual(overridden.status, STATUS_DEVELOPMENT_BYPASS)
        self.assertTrue(overridden.allowed)
        for value in ("true", "yes", "01", " 1", "1 "):
            with self.subTest(value=value):
                self.assertFalse(
                    development_bypass_requested(
                        {ENV_DEVELOPMENT_LICENSE_BYPASS: value}
                    )
                )

    def test_missing_config_and_invalid_state_fail_closed(self) -> None:
        missing = FakeProductRuntime(
            LocalProductState(PRODUCT_NOT_CONFIGURED, _runtime(packaged=True)),
            packaged_expected=True,
        )
        self.assertEqual(
            ProductLicenseGate(missing).evaluate().status,
            STATUS_NOT_CONFIGURED,
        )
        invalid = FakeProductRuntime(
            LocalProductState("invalid"),
            packaged_expected=True,
        )
        invalid_decision = ProductLicenseGate(invalid).evaluate()
        self.assertEqual(invalid_decision.status, STATUS_INVALID)
        self.assertFalse(invalid_decision.can_activate)

    def test_activation_wakes_event_without_polling_after_verified_readback(self) -> None:
        runtime = FakeProductRuntime(
            LocalProductState(STATUS_NOT_ACTIVATED, _runtime(packaged=True)),
            packaged_expected=True,
        )
        gate = ProductLicenseGate(runtime)
        self.assertFalse(gate.evaluate().allowed)
        observed: list[bool] = []
        waiter = threading.Thread(
            target=lambda: observed.append(gate.wait_until_allowed(1.0))
        )
        waiter.start()
        time.sleep(0.01)
        activated = gate.activate("test-activation-key")
        waiter.join(timeout=1)

        self.assertTrue(activated.allowed)
        self.assertEqual(observed, [True])
        self.assertEqual(runtime.local_state_calls, 2)
        self.assertEqual(runtime.keys, ["test-activation-key"])

    def test_claimed_success_without_local_entitlement_never_opens_gate(self) -> None:
        runtime = FakeProductRuntime(
            LocalProductState(STATUS_NOT_ACTIVATED, _runtime(packaged=True)),
            packaged_expected=True,
        )
        runtime.activation_changes_state = False
        gate = ProductLicenseGate(runtime)
        result = gate.activate("test-activation-key")
        self.assertFalse(result.allowed)
        self.assertEqual(result.status, STATUS_ACTIVATION_REQUIRED)
        self.assertFalse(gate.wait_until_allowed(0.01))

    def test_lost_local_entitlement_clears_allowed_event(self) -> None:
        runtime = FakeProductRuntime(
            LocalProductState(
                STATUS_ENTITLED,
                _runtime(packaged=True),
                "license_test_001",
            ),
            packaged_expected=True,
        )
        gate = ProductLicenseGate(runtime)
        self.assertTrue(gate.evaluate().allowed)
        self.assertTrue(gate.wait_until_allowed(0))

        runtime.state = LocalProductState(
            STATUS_NOT_ACTIVATED,
            _runtime(packaged=True),
        )
        self.assertFalse(gate.evaluate().allowed)
        self.assertFalse(gate.wait_until_allowed(0))

    def test_device_mismatch_is_distinct_and_public_repr_redacts_fingerprint(self) -> None:
        runtime = FakeProductRuntime(
            LocalProductState(STATUS_NOT_ACTIVATED, _runtime(packaged=True)),
            packaged_expected=True,
        )
        runtime.activation_status = "device_mismatch"
        result = ProductLicenseGate(runtime).activate("test-activation-key")
        self.assertEqual(result.status, STATUS_DEVICE_MISMATCH)
        self.assertTrue(result.can_activate)
        self.assertNotIn("sha256:", repr(result))


if __name__ == "__main__":
    unittest.main()
