from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from core.product_updates import VerifiedStagedUpdate
from core.product_version import ProductVersion
from core.update_transaction import (
    AdapterBackupResult,
    AdapterMutationResult,
    AdapterStatus,
    AdapterVerificationResult,
    LinuxUpdaterAdapter,
    MacOSUpdaterAdapter,
    TransactionStatus,
    UnsupportedUpdaterAdapter,
    UpdateTransactionCoordinator,
    UpdaterPlatformAdapter,
    VerifiedArtifactHandle,
    WindowsUpdaterAdapter,
    create_updater_adapter,
    open_verified_staged_artifact,
)


SOURCE = ProductVersion.parse("1.0.0", 10)
TARGET = ProductVersion.parse("1.1.0", 11)


class FakeUpdaterAdapter(UpdaterPlatformAdapter):
    platform_key = "test"

    def __init__(self) -> None:
        self.current: ProductVersion | None = SOURCE
        self.healthy = True
        self.install_result = AdapterMutationResult(AdapterStatus.SUCCESS, True)
        self.rollback_result = AdapterMutationResult(AdapterStatus.SUCCESS, True)
        self.fail_target_health = False
        self.install_exception: BaseException | None = None
        self.mutate_before_exception = False
        self.verify_timeouts: list[float] = []
        self.backup_calls = 0
        self.install_calls = 0
        self.rollback_calls = 0
        self.installed_artifact: VerifiedArtifactHandle | None = None
        self.installed_artifact_bytes: bytes | None = None
        self.replace_staged_path: tuple[Path, bytes] | None = None
        self.path_replacement_succeeded = False
        self.mutate_staged_path: tuple[Path, bytes] | None = None

    def prepare_persisted_backup(self, *, source, target):
        self.backup_calls += 1
        if self.replace_staged_path is not None:
            path, replacement_bytes = self.replace_staged_path
            replacement = path.with_name(path.name + ".replacement")
            replacement.write_bytes(replacement_bytes)
            try:
                os.replace(replacement, path)
                self.path_replacement_succeeded = True
            except PermissionError:
                replacement.unlink(missing_ok=True)
        if self.mutate_staged_path is not None:
            path, replacement_bytes = self.mutate_staged_path
            with path.open("r+b") as staged_file:
                staged_file.seek(0)
                staged_file.write(replacement_bytes)
                staged_file.truncate()
                staged_file.flush()
                os.fsync(staged_file.fileno())
        return AdapterBackupResult(AdapterStatus.SUCCESS, "backup-test-001")

    def install(self, staged_artifact, *, backup_reference, source, target):
        self.install_calls += 1
        if not isinstance(staged_artifact, VerifiedArtifactHandle):
            raise TypeError("installer did not receive a verified artifact handle")
        self.installed_artifact = staged_artifact
        with tempfile.TemporaryFile(mode="w+b") as private_copy:
            if not staged_artifact.copy_verified_to_private_descriptor(
                private_copy.fileno()
            ):
                return AdapterMutationResult(AdapterStatus.FAILED, False)
            self.installed_artifact_bytes = private_copy.read()
        if self.install_exception is not None:
            if self.mutate_before_exception:
                self.current = target
            raise self.install_exception
        result = self.install_result
        if result.status is AdapterStatus.SUCCESS or result.mutation_possible:
            self.current = target
        return result

    def verify_installed(self, expected, *, timeout_seconds):
        self.verify_timeouts.append(timeout_seconds)
        if (
            self.current == expected
            and self.healthy
            and not (expected == TARGET and self.fail_target_health)
        ):
            return AdapterVerificationResult(
                AdapterStatus.SUCCESS,
                installed=self.current,
                healthy=True,
            )
        return AdapterVerificationResult(AdapterStatus.FAILED)

    def rollback(self, backup_reference, expected_previous):
        self.rollback_calls += 1
        if self.rollback_result.status is AdapterStatus.SUCCESS:
            self.current = expected_previous
            self.healthy = True
        return self.rollback_result


class UpdateTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.raw = b"verified staged update" * 100
        self.path = Path(self.temp.name).resolve() / "verified.package"
        self.path.write_bytes(self.raw)
        self.staged = VerifiedStagedUpdate(
            self.path,
            SOURCE,
            TARGET,
            hashlib.sha256(self.raw).hexdigest(),
            len(self.raw),
        )
        self._journal_counter = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _coordinator(
        self,
        adapter: UpdaterPlatformAdapter,
        *,
        journal: Path | None = None,
        health_timeout_seconds: float = 30,
    ) -> UpdateTransactionCoordinator:
        if journal is None:
            self._journal_counter += 1
            journal = (
                Path(self.temp.name).resolve()
                / f"transaction-{self._journal_counter}.json"
            )
        return UpdateTransactionCoordinator(
            adapter,
            health_timeout_seconds=health_timeout_seconds,
            journal_path=journal,
        )

    def test_success_requires_exact_post_install_identity_and_health(self):
        adapter = FakeUpdaterAdapter()
        coordinator = self._coordinator(
            adapter,
            health_timeout_seconds=12,
        )

        result = coordinator.apply(self.staged)

        self.assertEqual(result.status, TransactionStatus.INSTALLED)
        self.assertTrue(result.installed)
        self.assertFalse(coordinator.rollback_required)
        self.assertEqual(adapter.install_calls, 1)
        self.assertEqual(adapter.rollback_calls, 0)
        self.assertEqual(adapter.verify_timeouts, [12.0, 12.0])
        self.assertEqual(adapter.installed_artifact_bytes, self.raw)
        self.assertIsNotNone(adapter.installed_artifact)
        self.assertTrue(adapter.installed_artifact.closed)

    def test_path_replacement_after_verification_cannot_change_installed_bytes(self):
        adapter = FakeUpdaterAdapter()
        replacement = b"unverified replacement package"
        adapter.replace_staged_path = (self.path, replacement)

        result = self._coordinator(adapter).apply(self.staged)

        self.assertEqual(result.status, TransactionStatus.INSTALLED)
        self.assertEqual(adapter.installed_artifact_bytes, self.raw)
        if adapter.path_replacement_succeeded:
            self.assertEqual(self.path.read_bytes(), replacement)

    def test_same_inode_mutation_after_first_verification_is_rejected(self):
        adapter = FakeUpdaterAdapter()
        mutation = b"X" * len(self.raw)
        adapter.mutate_staged_path = (self.path, mutation)

        result = self._coordinator(adapter).apply(self.staged)

        self.assertEqual(result.status, TransactionStatus.PRESERVED)
        self.assertEqual(adapter.current, SOURCE)
        self.assertEqual(adapter.install_calls, 1)
        self.assertIsNone(adapter.installed_artifact_bytes)
        self.assertEqual(self.path.read_bytes(), mutation)

    @unittest.skipIf(os.name == "nt", "POSIX private descriptor rule")
    def test_linked_same_user_copy_destination_is_rejected(self):
        linked_destination = Path(self.temp.name).resolve() / "linked-copy.tmp"
        descriptor = os.open(
            linked_destination,
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with open_verified_staged_artifact(self.staged) as artifact:
                self.assertIsNotNone(artifact)
                self.assertFalse(
                    artifact.copy_verified_to_private_descriptor(descriptor)
                )
            self.assertEqual(os.fstat(descriptor).st_size, 0)
        finally:
            os.close(descriptor)

    def test_pinned_handle_is_closed_even_when_installer_aborts(self):
        adapter = FakeUpdaterAdapter()
        adapter.install_exception = SystemExit("simulated installer abort")

        with self.assertRaises(SystemExit):
            self._coordinator(adapter).apply(self.staged)

        self.assertIsNotNone(adapter.installed_artifact)
        self.assertTrue(adapter.installed_artifact.closed)
        with tempfile.TemporaryFile(mode="w+b") as private_copy:
            with self.assertRaises(RuntimeError):
                adapter.installed_artifact.copy_verified_to_private_descriptor(
                    private_copy.fileno()
                )

    def test_staged_bytes_are_reverified_immediately_before_install(self):
        adapter = FakeUpdaterAdapter()
        self.path.write_bytes(self.raw + b"tamper")

        result = self._coordinator(adapter).apply(self.staged)

        self.assertEqual(result.status, TransactionStatus.INVALID)
        self.assertEqual(adapter.install_calls, 0)

    def test_preinstall_failure_reports_preserved_only_after_old_version_proof(self):
        adapter = FakeUpdaterAdapter()
        adapter.install_result = AdapterMutationResult(AdapterStatus.FAILED, False)

        result = self._coordinator(adapter).apply(self.staged)

        self.assertEqual(result.status, TransactionStatus.PRESERVED)
        self.assertTrue(result.safe)
        self.assertEqual(adapter.rollback_calls, 0)

    def test_failed_post_install_health_rolls_back_and_verifies_previous(self):
        adapter = FakeUpdaterAdapter()
        adapter.fail_target_health = True

        result = self._coordinator(adapter).apply(self.staged)

        self.assertEqual(result.status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(adapter.current, SOURCE)
        self.assertEqual(adapter.rollback_calls, 1)

    def test_unverified_rollback_blocks_retry_until_recovery_succeeds(self):
        adapter = FakeUpdaterAdapter()
        adapter.fail_target_health = True
        adapter.rollback_result = AdapterMutationResult(AdapterStatus.FAILED, True)
        journal = Path(self.temp.name).resolve() / "crash-recovery.json"
        coordinator = self._coordinator(adapter, journal=journal)

        failed = coordinator.apply(self.staged)
        retry = coordinator.apply(self.staged)
        adapter.rollback_result = AdapterMutationResult(AdapterStatus.SUCCESS, True)
        restarted = self._coordinator(adapter, journal=journal)
        recovered = restarted.recover()

        self.assertEqual(failed.status, TransactionStatus.ROLLBACK_REQUIRED)
        self.assertEqual(retry.status, TransactionStatus.ROLLBACK_REQUIRED)
        self.assertEqual(adapter.install_calls, 1)
        self.assertEqual(recovered.status, TransactionStatus.ROLLED_BACK)
        self.assertFalse(restarted.rollback_required)
        self.assertFalse(journal.exists())

    def test_wrong_preinstall_identity_never_calls_installer(self):
        adapter = FakeUpdaterAdapter()
        adapter.current = ProductVersion.parse("0.9.0", 9)

        result = self._coordinator(adapter).apply(self.staged)

        self.assertEqual(result.status, TransactionStatus.FAILED)
        self.assertEqual(adapter.install_calls, 0)

    def test_platform_factory_is_explicit_and_all_foundation_adapters_are_honest(self):
        cases = (
            ("Darwin", MacOSUpdaterAdapter, TransactionStatus.NOT_AVAILABLE),
            ("Windows", WindowsUpdaterAdapter, TransactionStatus.NOT_AVAILABLE),
            ("Linux", LinuxUpdaterAdapter, TransactionStatus.NOT_AVAILABLE),
            ("Haiku", UnsupportedUpdaterAdapter, TransactionStatus.UNSUPPORTED),
        )
        for system, expected_type, expected_status in cases:
            with self.subTest(system=system):
                adapter = create_updater_adapter(system)
                result = UpdateTransactionCoordinator(adapter).apply(self.staged)
                capability = adapter.capability()
                self.assertIsInstance(adapter, expected_type)
                self.assertEqual(result.status, expected_status)
                self.assertNotEqual(result.status, TransactionStatus.INSTALLED)
                self.assertFalse(capability.mutation_enabled)
                self.assertIsNotNone(capability.blocker)

    def test_process_loss_after_durable_checkpoint_recovers_on_new_coordinator(self):
        journal = Path(self.temp.name).resolve() / "power-loss.json"
        adapter = FakeUpdaterAdapter()
        adapter.install_exception = SystemExit("simulated power loss")
        first = self._coordinator(adapter, journal=journal)

        with self.assertRaises(SystemExit):
            first.apply(self.staged)

        self.assertTrue(journal.is_file())
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        checkpoint = json.loads(journal.read_text())
        self.assertEqual(checkpoint["source_version"], "1.0.0")
        self.assertEqual(checkpoint["source_build"], 10)
        self.assertEqual(checkpoint["target_version"], "1.1.0")
        self.assertEqual(checkpoint["target_build"], 11)
        self.assertEqual(checkpoint["backup_reference"], "backup-test-001")
        adapter.install_exception = None
        restarted = self._coordinator(adapter, journal=journal)
        recovered = restarted.recover()

        self.assertEqual(recovered.status, TransactionStatus.PRESERVED)
        self.assertFalse(journal.exists())
        self.assertFalse(restarted.rollback_required)

    def test_startup_probe_sees_checkpoint_written_after_construction(self):
        journal = Path(self.temp.name).resolve() / "late-checkpoint.json"
        observer_adapter = FakeUpdaterAdapter()
        observer = self._coordinator(observer_adapter, journal=journal)
        self.assertFalse(observer.rollback_required)

        writer_adapter = FakeUpdaterAdapter()
        writer_adapter.install_exception = SystemExit("simulated helper loss")
        writer = self._coordinator(writer_adapter, journal=journal)
        with self.assertRaises(SystemExit):
            writer.apply(self.staged)
        self.assertTrue(journal.exists())
        # The observer's cached construction state is stale; startup must use
        # the atomic fresh probe, which reloads under the journal lock.
        self.assertFalse(observer.rollback_required)

        recovered = observer.recover_if_required()

        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.status, TransactionStatus.PRESERVED)
        self.assertFalse(journal.exists())

    def test_startup_probe_blocks_late_corrupt_checkpoint(self):
        journal = Path(self.temp.name).resolve() / "late-corrupt.json"
        observer = self._coordinator(FakeUpdaterAdapter(), journal=journal)
        self.assertFalse(observer.rollback_required)
        journal.write_text('{"state":"rollback_required","secret":"unexpected"}')

        recovered = observer.recover_if_required()

        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.status, TransactionStatus.ROLLBACK_REQUIRED)

    def test_process_loss_after_mutation_restores_persisted_backup_on_restart(self):
        journal = Path(self.temp.name).resolve() / "post-mutation-loss.json"
        adapter = FakeUpdaterAdapter()
        adapter.install_exception = SystemExit("simulated power loss")
        adapter.mutate_before_exception = True
        first = self._coordinator(adapter, journal=journal)

        with self.assertRaises(SystemExit):
            first.apply(self.staged)

        self.assertEqual(adapter.current, TARGET)
        adapter.install_exception = None
        restarted = self._coordinator(adapter, journal=journal)
        recovered = restarted.recover()

        self.assertEqual(recovered.status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(adapter.current, SOURCE)
        self.assertEqual(adapter.rollback_calls, 1)
        self.assertFalse(journal.exists())

    def test_corrupt_durable_checkpoint_blocks_install_and_recovery_guessing(self):
        journal = Path(self.temp.name).resolve() / "corrupt.json"
        journal.write_text('{"state":"rollback_required","secret":"unexpected"}')
        adapter = FakeUpdaterAdapter()
        coordinator = self._coordinator(adapter, journal=journal)

        blocked = coordinator.apply(self.staged)
        recovered = coordinator.recover()

        self.assertEqual(blocked.status, TransactionStatus.ROLLBACK_REQUIRED)
        self.assertEqual(recovered.status, TransactionStatus.ROLLBACK_REQUIRED)
        self.assertEqual(adapter.install_calls, 0)
        self.assertEqual(adapter.rollback_calls, 0)


if __name__ == "__main__":
    unittest.main()
