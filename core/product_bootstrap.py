"""Framework-independent product-before-Gemini startup ordering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Final, Protocol

from core.product_gate import ProductGateSnapshot, ProductLicenseGate


STATUS_READY: Final = "ready"
STATUS_CANCELLED: Final = "cancelled"


class ProductBootstrapUiPort(Protocol):
    def present_product_gate(self, snapshot: ProductGateSnapshot) -> None: ...

    def wait_for_product_gate(self) -> bool: ...

    def begin_gemini_onboarding(self) -> None: ...

    def wait_for_api_key(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ProductBootstrapResult:
    status: str
    runtime: object | None = field(default=None, repr=False)

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY and self.runtime is not None


class ProductBootstrapCoordinator:
    """Open the assistant runtime only after both authority boundaries pass."""

    __slots__ = ("_gate", "_ui")

    def __init__(
        self,
        gate: ProductLicenseGate,
        ui: ProductBootstrapUiPort,
    ) -> None:
        self._gate = gate
        self._ui = ui

    def prepare(self, runtime_factory: Callable[[], object]) -> ProductBootstrapResult:
        snapshot = self._gate.evaluate()
        while True:
            while True:
                self._ui.present_product_gate(snapshot)
                if not self._ui.wait_for_product_gate():
                    return ProductBootstrapResult(STATUS_CANCELLED)
                # A UI event is never authority. Re-read signed local state (or
                # the non-packaged explicit dev policy) before Gemini. A
                # transient failure returns to the event wait without polling.
                snapshot = self._gate.evaluate()
                if snapshot.allowed:
                    break
            self._ui.begin_gemini_onboarding()
            if not self._ui.wait_for_api_key():
                return ProductBootstrapResult(STATUS_CANCELLED)
            # Gemini onboarding may take minutes. Re-verify immediately before
            # constructing any assistant/audio/dashboard runtime. If authority
            # changed, return to the event-driven license screen.
            snapshot = self._gate.evaluate()
            if snapshot.allowed:
                return ProductBootstrapResult(STATUS_READY, runtime_factory())


__all__ = [
    "STATUS_CANCELLED",
    "STATUS_READY",
    "ProductBootstrapCoordinator",
    "ProductBootstrapResult",
    "ProductBootstrapUiPort",
]
