from __future__ import annotations

import unittest

from core.product_version import ProductVersion
from core.update_startup import recover_interrupted_update
from core.update_transaction import TransactionStatus, UpdateTransactionResult


SOURCE = ProductVersion.parse("1.0.0", 1)
TARGET = ProductVersion.parse("1.1.0", 2)


class _RecoveryService:
    def __init__(
        self,
        *,
        result: object = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.recover_calls = 0

    def recover_update_if_required(self):
        self.recover_calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _result(status: TransactionStatus) -> UpdateTransactionResult:
    return UpdateTransactionResult(status, "test", SOURCE, TARGET)


class UpdateStartupRecoveryTests(unittest.TestCase):
    def test_atomic_fresh_probe_without_checkpoint_allows_startup(self) -> None:
        service = _RecoveryService(result=None)
        recovery = recover_interrupted_update(service)
        self.assertFalse(recovery.required)
        self.assertTrue(recovery.may_start)
        self.assertIsNone(recovery.result)
        self.assertEqual(service.recover_calls, 1)

    def test_verified_preserve_or_rollback_allows_startup(self) -> None:
        for status in (
            TransactionStatus.PRESERVED,
            TransactionStatus.ROLLED_BACK,
        ):
            with self.subTest(status=status):
                service = _RecoveryService(result=_result(status))
                recovery = recover_interrupted_update(service)
                self.assertTrue(recovery.required)
                self.assertTrue(recovery.may_start)
                self.assertEqual(recovery.result.status, status)
                self.assertEqual(service.recover_calls, 1)

    def test_any_unresolved_or_unexpected_result_blocks_startup(self) -> None:
        results = (
            _result(TransactionStatus.ROLLBACK_REQUIRED),
            _result(TransactionStatus.NOT_AVAILABLE),
            _result(TransactionStatus.FAILED),
            "preserved",
        )
        for result in results:
            with self.subTest(result=result):
                recovery = recover_interrupted_update(
                    _RecoveryService(result=result)
                )
                self.assertTrue(recovery.required)
                self.assertFalse(recovery.may_start)

    def test_invalid_contract_or_exception_fails_closed(self) -> None:
        recovery_error = recover_interrupted_update(
            _RecoveryService(error=OSError("rollback"))
        )
        self.assertFalse(recovery_error.may_start)


if __name__ == "__main__":
    unittest.main()
