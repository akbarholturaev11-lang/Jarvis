"""Fail-closed exact-version license gate for desktop startup.

The gate is deliberately independent from Qt.  It serializes activation and
refresh work, exposes a blocking event instead of polling, and permits a source
development bypass only when it was explicitly requested.  A packaged runtime
always ignores the bypass request.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

from core.product_runtime import (
    STATUS_ENTITLED,
    STATUS_FAILED as PRODUCT_FAILED,
    STATUS_INVALID as PRODUCT_INVALID,
    STATUS_NOT_ACTIVATED,
    STATUS_NOT_AVAILABLE as PRODUCT_NOT_AVAILABLE,
    STATUS_NOT_CONFIGURED,
    LocalProductState,
    ProductActivationOutcome,
)


ENV_DEVELOPMENT_LICENSE_BYPASS: Final = "JARVIS_DEV_LICENSE_BYPASS"

STATUS_ALLOWED: Final = "allowed"
STATUS_DEVELOPMENT_BYPASS: Final = "development_bypass"
STATUS_ACTIVATION_REQUIRED: Final = "activation_required"
STATUS_DEVICE_MISMATCH: Final = "device_mismatch"
STATUS_NOT_CONFIGURED: Final = "not_configured"
STATUS_NOT_AVAILABLE: Final = "not_available"
STATUS_INVALID: Final = "invalid"
STATUS_OFFLINE: Final = "offline"
STATUS_REJECTED: Final = "rejected"
STATUS_SERVER_UNAVAILABLE: Final = "server_unavailable"
STATUS_FAILED: Final = "failed"

_ALLOWED_STATUSES: Final = frozenset(
    {STATUS_ALLOWED, STATUS_DEVELOPMENT_BYPASS}
)
_ACTIVATABLE_STATUSES: Final = frozenset(
    {
        STATUS_ACTIVATION_REQUIRED,
        STATUS_DEVICE_MISMATCH,
        STATUS_INVALID,
        STATUS_OFFLINE,
        STATUS_REJECTED,
        STATUS_SERVER_UNAVAILABLE,
        STATUS_FAILED,
    }
)


class ProductRuntimePort(Protocol):
    @property
    def packaged_runtime_expected(self) -> bool: ...

    def local_state(self) -> LocalProductState: ...

    def activate(self, license_key: str) -> ProductActivationOutcome: ...

    def device_fingerprint(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class ProductGateSnapshot:
    status: str
    product_status: str
    packaged: bool
    version: str | None = None
    build: int | None = None
    device_fingerprint: str | None = field(default=None, repr=False)
    can_activate: bool = False
    purchase_available: bool = False

    @property
    def allowed(self) -> bool:
        return self.status in _ALLOWED_STATUSES

    def __repr__(self) -> str:
        device = "present" if self.device_fingerprint else "none"
        return (
            f"ProductGateSnapshot(status={self.status!r}, "
            f"product_status={self.product_status!r}, packaged={self.packaged!r}, "
            f"version={self.version!r}, build={self.build!r}, "
            f"device_fingerprint={device!r}, can_activate={self.can_activate!r}, "
            f"purchase_available={self.purchase_available!r})"
        )


def development_bypass_requested(
    environ: Mapping[str, str] | None = None,
) -> bool:
    source = os.environ if environ is None else environ
    return source.get(ENV_DEVELOPMENT_LICENSE_BYPASS) == "1"


class ProductLicenseGate:
    """Serialize gate decisions and activation without a polling loop."""

    __slots__ = (
        "_allowed_event",
        "_development_override",
        "_lock",
        "_runtime",
    )

    def __init__(
        self,
        runtime: ProductRuntimePort,
        *,
        development_override: bool | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if development_override is not None and type(development_override) is not bool:
            raise TypeError("development_override must be a boolean")
        self._runtime = runtime
        self._development_override = (
            development_bypass_requested(environ)
            if development_override is None
            else development_override
        )
        self._allowed_event = threading.Event()
        self._lock = threading.RLock()

    @property
    def runtime(self) -> ProductRuntimePort:
        return self._runtime

    def wait_until_allowed(self, timeout: float | None = None) -> bool:
        return self._allowed_event.wait(timeout)

    def evaluate(self) -> ProductGateSnapshot:
        with self._lock:
            try:
                state = self._runtime.local_state()
            except Exception:
                self._allowed_event.clear()
                snapshot = ProductGateSnapshot(
                    STATUS_FAILED,
                    PRODUCT_FAILED,
                    True,
                )
                return snapshot
            try:
                packaged = bool(self._runtime.packaged_runtime_expected)
            except Exception:
                packaged = True
            if state.runtime is not None:
                packaged = packaged or state.runtime.packaged
            if state.status == STATUS_ENTITLED:
                snapshot = self._snapshot(
                    STATUS_ALLOWED,
                    state,
                    packaged=packaged,
                    can_activate=False,
                )
            elif (
                self._development_override
                and not packaged
                and state.runtime is not None
            ):
                snapshot = self._snapshot(
                    STATUS_DEVELOPMENT_BYPASS,
                    state,
                    packaged=False,
                    can_activate=False,
                )
            else:
                gate_status = {
                    STATUS_NOT_ACTIVATED: STATUS_ACTIVATION_REQUIRED,
                    STATUS_NOT_CONFIGURED: STATUS_NOT_CONFIGURED,
                    PRODUCT_NOT_AVAILABLE: STATUS_NOT_AVAILABLE,
                    PRODUCT_INVALID: STATUS_INVALID,
                    PRODUCT_FAILED: STATUS_FAILED,
                }.get(state.status, STATUS_FAILED)
                snapshot = self._snapshot(
                    gate_status,
                    state,
                    packaged=packaged,
                    can_activate=(
                        gate_status in _ACTIVATABLE_STATUSES
                        and state.runtime is not None
                    ),
                )
            if snapshot.allowed:
                self._allowed_event.set()
            else:
                self._allowed_event.clear()
            return snapshot

    def activate(self, license_key: str) -> ProductGateSnapshot:
        """Activate, then re-read signed local state before opening the gate."""

        with self._lock:
            try:
                outcome = self._runtime.activate(license_key)
            except Exception:
                self._allowed_event.clear()
                return self._activation_snapshot(STATUS_FAILED)
            if outcome.ok:
                # ProductRuntimeService also performs this verification.  The
                # gate intentionally verifies again at the authority boundary.
                return self.evaluate()
            gate_status = {
                "device_mismatch": STATUS_DEVICE_MISMATCH,
                "invalid": STATUS_INVALID,
                "offline": STATUS_OFFLINE,
                "rejected": STATUS_REJECTED,
                "server_unavailable": STATUS_SERVER_UNAVAILABLE,
                "not_available": STATUS_NOT_AVAILABLE,
                "not_configured": STATUS_NOT_CONFIGURED,
                "failed": STATUS_FAILED,
            }.get(outcome.status, STATUS_FAILED)
            return self._activation_snapshot(gate_status)

    def _activation_snapshot(self, status: str) -> ProductGateSnapshot:
        self._allowed_event.clear()
        try:
            state = self._runtime.local_state()
        except Exception:
            return ProductGateSnapshot(status, PRODUCT_FAILED, True)
        try:
            packaged = bool(self._runtime.packaged_runtime_expected)
        except Exception:
            packaged = True
        if state.runtime is not None:
            packaged = packaged or state.runtime.packaged
        return self._snapshot(
            status,
            state,
            packaged=packaged,
            can_activate=status in _ACTIVATABLE_STATUSES,
        )

    def _snapshot(
        self,
        status: str,
        state: LocalProductState,
        *,
        packaged: bool,
        can_activate: bool,
    ) -> ProductGateSnapshot:
        version: str | None = None
        build: int | None = None
        if state.runtime is not None:
            version = str(state.runtime.product_version.version)
            build = state.runtime.product_version.build
        device: str | None = None
        if can_activate and state.status != STATUS_NOT_CONFIGURED:
            try:
                device = self._runtime.device_fingerprint()
            except Exception:
                device = None
        return ProductGateSnapshot(
            status,
            state.status,
            packaged,
            version,
            build,
            device,
            can_activate,
            False,
        )

    def __repr__(self) -> str:
        return (
            "ProductLicenseGate(runtime=<private>, "
            f"development_override={self._development_override!r})"
        )


__all__ = [
    "ENV_DEVELOPMENT_LICENSE_BYPASS",
    "STATUS_ACTIVATION_REQUIRED",
    "STATUS_ALLOWED",
    "STATUS_DEVELOPMENT_BYPASS",
    "STATUS_DEVICE_MISMATCH",
    "STATUS_FAILED",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_NOT_CONFIGURED",
    "STATUS_OFFLINE",
    "STATUS_REJECTED",
    "STATUS_SERVER_UNAVAILABLE",
    "ProductGateSnapshot",
    "ProductLicenseGate",
    "development_bypass_requested",
]
