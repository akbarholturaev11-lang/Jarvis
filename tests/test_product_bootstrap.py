from __future__ import annotations

import unittest

from core.product_bootstrap import (
    STATUS_CANCELLED,
    STATUS_READY,
    ProductBootstrapCoordinator,
)
from core.product_gate import ProductLicenseGate
from core.product_runtime import STATUS_ENTITLED, STATUS_NOT_ACTIVATED, LocalProductState
from core.product_version import ProductVersion
from core.runtime_product import RuntimeProductIdentity

from tests.test_product_gate import FakeProductRuntime


def _identity() -> RuntimeProductIdentity:
    return RuntimeProductIdentity(
        ProductVersion.parse("1.0.0", 1),
        "macos",
        "arm64",
        True,
    )


class FakeUi:
    def __init__(
        self,
        trace: list[str],
        *,
        license_wait: bool | list[bool] = True,
        api_wait=True,
    ) -> None:
        self.trace = trace
        self.license_wait = license_wait
        self.api_wait = api_wait

    def present_product_gate(self, snapshot) -> None:
        self.trace.append("license")

    def wait_for_product_gate(self) -> bool:
        self.trace.append("license_wait")
        if isinstance(self.license_wait, list):
            return self.license_wait.pop(0)
        return self.license_wait

    def begin_gemini_onboarding(self) -> None:
        self.trace.append("gemini")

    def wait_for_api_key(self) -> bool:
        self.trace.append("gemini_wait")
        return self.api_wait


class ProductBootstrapCoordinatorTests(unittest.TestCase):
    def test_strict_license_then_gemini_then_runtime_order(self) -> None:
        trace: list[str] = []
        runtime = FakeProductRuntime(
            LocalProductState(STATUS_ENTITLED, _identity(), "license_test_001"),
            packaged_expected=True,
        )
        coordinator = ProductBootstrapCoordinator(
            ProductLicenseGate(runtime),
            FakeUi(trace),
        )
        result = coordinator.prepare(
            lambda: trace.append("runtime") or object()
        )
        self.assertEqual(result.status, STATUS_READY)
        self.assertTrue(result.ready)
        self.assertEqual(
            trace,
            ["license", "license_wait", "gemini", "gemini_wait", "runtime"],
        )

    def test_closed_license_gate_never_starts_gemini_or_runtime(self) -> None:
        trace: list[str] = []
        runtime = FakeProductRuntime(
            LocalProductState(STATUS_NOT_ACTIVATED, _identity()),
            packaged_expected=True,
        )
        coordinator = ProductBootstrapCoordinator(
            ProductLicenseGate(runtime),
            FakeUi(trace, license_wait=False),
        )
        result = coordinator.prepare(
            lambda: trace.append("runtime") or object()
        )
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertEqual(trace, ["license", "license_wait"])

    def test_cancelled_gemini_onboarding_never_constructs_runtime(self) -> None:
        trace: list[str] = []
        runtime = FakeProductRuntime(
            LocalProductState(STATUS_ENTITLED, _identity(), "license_test_001"),
            packaged_expected=True,
        )
        result = ProductBootstrapCoordinator(
            ProductLicenseGate(runtime),
            FakeUi(trace, api_wait=False),
        ).prepare(lambda: trace.append("runtime") or object())
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertNotIn("runtime", trace)

    def test_ui_event_alone_never_bypasses_signed_local_authority(self) -> None:
        trace: list[str] = []
        runtime = FakeProductRuntime(
            LocalProductState(STATUS_NOT_ACTIVATED, _identity()),
            packaged_expected=True,
        )
        result = ProductBootstrapCoordinator(
            ProductLicenseGate(runtime),
            FakeUi(trace, license_wait=[True, False]),
        ).prepare(lambda: trace.append("runtime") or object())
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertEqual(
            trace,
            ["license", "license_wait", "license", "license_wait"],
        )
        self.assertNotIn("gemini", trace)

    def test_transient_reverification_failure_waits_for_a_new_event(self) -> None:
        trace: list[str] = []

        class SequencedRuntime(FakeProductRuntime):
            def __init__(self):
                super().__init__(
                    LocalProductState(
                        STATUS_ENTITLED,
                        _identity(),
                        "license_test_001",
                    ),
                    packaged_expected=True,
                )
                self.states = [
                    self.state,
                    LocalProductState(STATUS_NOT_ACTIVATED, _identity()),
                    self.state,
                    self.state,
                ]

            def local_state(self):
                self.local_state_calls += 1
                return self.states.pop(0)

        result = ProductBootstrapCoordinator(
            ProductLicenseGate(SequencedRuntime()),
            FakeUi(trace, license_wait=[True, True]),
        ).prepare(lambda: trace.append("runtime") or object())
        self.assertTrue(result.ready)
        self.assertEqual(trace.count("license_wait"), 2)
        self.assertEqual(trace[-1], "runtime")

    def test_license_is_reverified_after_gemini_before_runtime_creation(self) -> None:
        trace: list[str] = []
        runtime = FakeProductRuntime(
            LocalProductState(STATUS_ENTITLED, _identity(), "license_test_001"),
            packaged_expected=True,
        )

        class MutatingUi(FakeUi):
            def wait_for_api_key(self) -> bool:
                self.trace.append("gemini_wait")
                runtime.state = LocalProductState(
                    STATUS_NOT_ACTIVATED,
                    _identity(),
                )
                return True

        result = ProductBootstrapCoordinator(
            ProductLicenseGate(runtime),
            MutatingUi(trace, license_wait=[True, False]),
        ).prepare(lambda: trace.append("runtime") or object())
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertNotIn("runtime", trace)
        self.assertEqual(trace.count("license"), 2)


if __name__ == "__main__":
    unittest.main()
