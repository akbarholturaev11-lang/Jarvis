"""Fail-closed startup recovery for interrupted update transactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from core.update_transaction import TransactionStatus, UpdateTransactionResult


class UpdateRecoveryService(Protocol):
    def recover_update_if_required(self) -> UpdateTransactionResult | None: ...


@dataclass(frozen=True, slots=True)
class StartupUpdateRecovery:
    """Decision made before licensing, onboarding, or assistant startup."""

    required: bool
    may_start: bool
    result: UpdateTransactionResult | None = field(default=None, repr=False)


def recover_interrupted_update(
    service: UpdateRecoveryService,
) -> StartupUpdateRecovery:
    """Resolve a durable rollback checkpoint, blocking on any uncertainty."""

    try:
        result = service.recover_update_if_required()
    except Exception:
        return StartupUpdateRecovery(True, False)
    if result is None:
        return StartupUpdateRecovery(False, True)
    if not isinstance(result, UpdateTransactionResult):
        return StartupUpdateRecovery(True, False)
    return StartupUpdateRecovery(
        True,
        result.status
        in {
            TransactionStatus.PRESERVED,
            TransactionStatus.ROLLED_BACK,
        },
        result,
    )


__all__ = [
    "StartupUpdateRecovery",
    "UpdateRecoveryService",
    "recover_interrupted_update",
]
