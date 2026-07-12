"""Fail-closed state machines for license, payment, update, and connectivity."""

from __future__ import annotations

from enum import StrEnum
from typing import TypeAlias


class LicenseState(StrEnum):
    MISSING = "missing"
    ACTIVE = "active"


class PaymentState(StrEnum):
    PENDING = "pending"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class UpdateState(StrEnum):
    CURRENT = "current"
    OLD_VERSION = "old_version"
    AVAILABLE = "available"
    PURCHASE_REQUIRED = "purchase_required"
    ENTITLED = "entitled"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    INSTALLING = "installing"
    INSTALLED = "installed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class ConnectivityState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    SERVER_UNAVAILABLE = "server_unavailable"


ProductState: TypeAlias = (
    LicenseState | PaymentState | UpdateState | ConnectivityState
)


class InvalidStateTransition(ValueError):
    """Raised when a caller attempts an unlisted product-state transition."""


_LICENSE_TRANSITIONS: dict[LicenseState, frozenset[LicenseState]] = {
    LicenseState.MISSING: frozenset({LicenseState.ACTIVE}),
    LicenseState.ACTIVE: frozenset(),
}

_PAYMENT_TRANSITIONS: dict[PaymentState, frozenset[PaymentState]] = {
    PaymentState.PENDING: frozenset({PaymentState.UNDER_REVIEW}),
    PaymentState.UNDER_REVIEW: frozenset(
        {PaymentState.APPROVED, PaymentState.REJECTED}
    ),
    PaymentState.APPROVED: frozenset(),
    PaymentState.REJECTED: frozenset(),
}

_UPDATE_TRANSITIONS: dict[UpdateState, frozenset[UpdateState]] = {
    UpdateState.CURRENT: frozenset({UpdateState.AVAILABLE}),
    UpdateState.OLD_VERSION: frozenset({UpdateState.AVAILABLE}),
    UpdateState.AVAILABLE: frozenset(
        {
            UpdateState.OLD_VERSION,
            UpdateState.PURCHASE_REQUIRED,
            UpdateState.ENTITLED,
        }
    ),
    UpdateState.PURCHASE_REQUIRED: frozenset(
        {UpdateState.OLD_VERSION, UpdateState.ENTITLED}
    ),
    UpdateState.ENTITLED: frozenset({UpdateState.DOWNLOADING}),
    UpdateState.DOWNLOADING: frozenset(
        {UpdateState.VERIFYING, UpdateState.FAILED}
    ),
    UpdateState.VERIFYING: frozenset(
        {UpdateState.INSTALLING, UpdateState.FAILED}
    ),
    UpdateState.INSTALLING: frozenset(
        {UpdateState.INSTALLED, UpdateState.FAILED}
    ),
    UpdateState.INSTALLED: frozenset({UpdateState.CURRENT}),
    UpdateState.FAILED: frozenset(
        {UpdateState.ENTITLED, UpdateState.ROLLED_BACK}
    ),
    UpdateState.ROLLED_BACK: frozenset({UpdateState.OLD_VERSION}),
}

_CONNECTIVITY_TRANSITIONS: dict[
    ConnectivityState, frozenset[ConnectivityState]
] = {
    ConnectivityState.ONLINE: frozenset(
        {ConnectivityState.OFFLINE, ConnectivityState.SERVER_UNAVAILABLE}
    ),
    ConnectivityState.OFFLINE: frozenset({ConnectivityState.ONLINE}),
    ConnectivityState.SERVER_UNAVAILABLE: frozenset(
        {ConnectivityState.ONLINE, ConnectivityState.OFFLINE}
    ),
}

_TRANSITIONS = {
    LicenseState: _LICENSE_TRANSITIONS,
    PaymentState: _PAYMENT_TRANSITIONS,
    UpdateState: _UPDATE_TRANSITIONS,
    ConnectivityState: _CONNECTIVITY_TRANSITIONS,
}


def allowed_transitions(state: object) -> frozenset[ProductState]:
    """Return only explicitly allowed next states; unknown input yields none."""

    state_type = type(state)
    transitions = _TRANSITIONS.get(state_type)
    if transitions is None:
        return frozenset()
    return transitions.get(state, frozenset())


def can_transition(current: object, target: object) -> bool:
    """Check a transition without coercing strings or crossing state families."""

    state_type = type(current)
    if state_type is not type(target) or state_type not in _TRANSITIONS:
        return False
    if current == target:
        return True
    return target in allowed_transitions(current)


def transition_or_raise(current: object, target: object) -> ProductState:
    """Return *target* for a valid transition, otherwise fail closed."""

    if not can_transition(current, target):
        raise InvalidStateTransition(f"invalid transition: {current!r} -> {target!r}")
    return target
