from __future__ import annotations

import hashlib
import json
import os
import plistlib
import stat
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from core.macos_update import (
    FileNonceHealthProbe,
    HelperCommandResult,
    MacOSArchiveLimits,
    MacOSDevelopmentUpdaterAdapter,
    assess_production_macos_helper,
    read_macos_app_identity,
    write_health_response,
)
from core.product_updates import VerifiedStagedUpdate
from core.product_version import BUNDLE_ID, PRODUCT_ID, ProductVersion
from core.update_transaction import (
    AdapterStatus,
    MacOSUpdaterAdapter,
    TransactionStatus,
    UpdateTransactionCoordinator,
    create_updater_adapter,
)


SOURCE = ProductVersion.parse("1.0.0", 10)
TARGET = ProductVersion.parse("1.1.0", 11)


def _make_app(path: Path, identity: ProductVersion, *, framework_links: bool = True) -> None:
    contents = path / "Contents"
    executable = contents / "MacOS" / "JARVIS"
    resources = contents / "Resources"
    executable.parent.mkdir(parents=True)
    resources.mkdir()
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    (resources / "product_build.json").write_text(
        json.dumps(
            {
                "product_id": "jarvis",
                "bundle_id": BUNDLE_ID,
                "version": str(identity.version),
                "build": identity.build,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (contents / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": BUNDLE_ID,
                "CFBundleShortVersionString": str(identity.version),
                "CFBundleVersion": str(identity.build),
                "CFBundleExecutable": "JARVIS",
            }
        )
    )
    if framework_links:
        framework = contents / "Frameworks" / "QtCore.framework"
        version = framework / "Versions" / "A"
        (version / "Resources").mkdir(parents=True)
        (version / "QtCore").write_bytes(b"signed framework bytes")
        os.symlink("A", framework / "Versions" / "Current")
        os.symlink("Versions/Current/QtCore", framework / "QtCore")
        os.symlink("Versions/Current/Resources", framework / "Resources")


def _zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _write_app_zip(
    app: Path,
    destination: Path,
    *,
    extra_entries: tuple[tuple[zipfile.ZipInfo, bytes], ...] = (),
) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_zip_info(f"{app.name}/", stat.S_IFDIR | 0o755), b"")
        for current, directories, files in os.walk(app, topdown=True, followlinks=False):
            current_path = Path(current)
            relative_root = current_path.relative_to(app)
            for name in tuple(directories):
                source = current_path / name
                relative = Path(app.name) / relative_root / name
                opened = source.lstat()
                if stat.S_ISLNK(opened.st_mode):
                    archive.writestr(
                        _zip_info(relative.as_posix(), stat.S_IFLNK | 0o777),
                        os.readlink(source).encode("utf-8"),
                    )
                    directories.remove(name)
                else:
                    archive.writestr(
                        _zip_info(relative.as_posix() + "/", stat.S_IFDIR | 0o755),
                        b"",
                    )
            for name in files:
                source = current_path / name
                relative = Path(app.name) / relative_root / name
                opened = source.lstat()
                if stat.S_ISLNK(opened.st_mode):
                    archive.writestr(
                        _zip_info(relative.as_posix(), stat.S_IFLNK | 0o777),
                        os.readlink(source).encode("utf-8"),
                    )
                else:
                    mode = stat.S_IFREG | (0o755 if opened.st_mode & 0o111 else 0o644)
                    archive.writestr(_zip_info(relative.as_posix(), mode), source.read_bytes())
        for info, content in extra_entries:
            archive.writestr(info, content)


class MacOSDevelopmentUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.install_parent = self.root / "Applications"
        self.install_parent.mkdir(mode=0o700)
        self.installed_app = self.install_parent / "JARVIS.app"
        _make_app(self.installed_app, SOURCE)
        self.candidate_app = self.root / "candidate" / "JARVIS.app"
        _make_app(self.candidate_app, TARGET)
        self.archive = self.root / "JARVIS.update.zip"
        _write_app_zip(self.candidate_app, self.archive)
        self.backups = self.install_parent / ".jarvis-backups"
        self.health = self.root / "health"
        self.journal = self.root / "update-journal.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _staged(
        self,
        *,
        path: Path | None = None,
        source: ProductVersion = SOURCE,
        target: ProductVersion = TARGET,
    ) -> VerifiedStagedUpdate:
        selected = path or self.archive
        content = selected.read_bytes()
        return VerifiedStagedUpdate(
            selected,
            source,
            target,
            hashlib.sha256(content).hexdigest(),
            len(content),
        )

    @staticmethod
    def _healthy_launcher(app: Path, request: Path, response: Path) -> None:
        if not app.is_dir():
            raise RuntimeError("candidate was not installed")
        write_health_response(
            request,
            response,
            actual_product_id=PRODUCT_ID,
            actual_bundle_id=BUNDLE_ID,
            actual_identity=read_macos_app_identity(app),
            healthy=True,
        )

    @staticmethod
    def _target_failing_launcher(app: Path, request: Path, response: Path) -> None:
        if read_macos_app_identity(app) == SOURCE:
            MacOSDevelopmentUpdateTests._healthy_launcher(app, request, response)

    def _adapter(
        self,
        *,
        healthy: bool = True,
        limits: MacOSArchiveLimits | None = None,
        phase_hook=None,
    ) -> MacOSDevelopmentUpdaterAdapter:
        probe = FileNonceHealthProbe(
            self.health,
            self._healthy_launcher if healthy else self._target_failing_launcher,
            poll_interval_seconds=0.005,
        )
        return MacOSDevelopmentUpdaterAdapter(
            installed_app=self.installed_app,
            backup_root=self.backups,
            health_probe=probe,
            development_mode=True,
            frozen=False,
            archive_limits=limits,
            phase_hook=phase_hook,
        )

    def _coordinator(
        self,
        adapter: MacOSDevelopmentUpdaterAdapter,
        *,
        journal: Path | None = None,
    ) -> UpdateTransactionCoordinator:
        return UpdateTransactionCoordinator(
            adapter,
            journal_path=journal or self.journal,
            health_timeout_seconds=0.08,
        )

    def _malicious_archive(
        self,
        entries: tuple[tuple[zipfile.ZipInfo, bytes], ...],
        *,
        name: str,
    ) -> Path:
        path = self.root / name
        _write_app_zip(self.candidate_app, path, extra_entries=entries)
        return path

    def test_real_a_to_b_update_preserves_framework_links_and_passes_nonce_health(self) -> None:
        result = self._coordinator(self._adapter()).apply(self._staged())

        self.assertEqual(result.status, TransactionStatus.INSTALLED)
        self.assertTrue(result.installed)
        self.assertEqual(read_macos_app_identity(self.installed_app), TARGET)
        framework = self.installed_app / "Contents" / "Frameworks" / "QtCore.framework"
        self.assertTrue((framework / "Versions" / "Current").is_symlink())
        self.assertEqual(os.readlink(framework / "Versions" / "Current"), "A")
        self.assertEqual((framework / "QtCore").resolve().read_bytes(), b"signed framework bytes")
        self.assertFalse(self.journal.exists())
        self.assertFalse((self.health / "request.json").exists())
        backups = tuple(path for path in self.backups.iterdir() if path.name.startswith("backup-"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o700)

    def test_failed_health_check_rolls_back_and_reverifies_a(self) -> None:
        result = self._coordinator(self._adapter(healthy=False)).apply(self._staged())

        self.assertEqual(result.status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)
        self.assertFalse(self.journal.exists())

    def test_unhealthy_source_fails_preflight_before_backup_or_mutation(self) -> None:
        probe = FileNonceHealthProbe(
            self.health,
            lambda _app, _request, _response: None,
            poll_interval_seconds=0.005,
        )
        adapter = MacOSDevelopmentUpdaterAdapter(
            installed_app=self.installed_app,
            backup_root=self.backups,
            health_probe=probe,
            development_mode=True,
            frozen=False,
        )

        result = self._coordinator(adapter).apply(self._staged())

        self.assertEqual(result.status, TransactionStatus.FAILED)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)
        self.assertEqual(tuple(self.backups.glob("backup-*")), ())

    def test_unhealthy_restored_source_does_not_claim_verified_rollback(self) -> None:
        source_launches = 0

        def launcher(app: Path, request: Path, response: Path) -> None:
            nonlocal source_launches
            identity = read_macos_app_identity(app)
            if identity == SOURCE:
                source_launches += 1
                if source_launches == 1:
                    self._healthy_launcher(app, request, response)

        probe = FileNonceHealthProbe(self.health, launcher, poll_interval_seconds=0.005)
        adapter = MacOSDevelopmentUpdaterAdapter(
            installed_app=self.installed_app,
            backup_root=self.backups,
            health_probe=probe,
            development_mode=True,
            frozen=False,
        )

        result = self._coordinator(adapter).apply(self._staged())

        self.assertEqual(result.status, TransactionStatus.ROLLBACK_REQUIRED)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)
        self.assertTrue(self.journal.exists())

    def test_blocking_health_launcher_cannot_exceed_coordinator_deadline(self) -> None:
        def blocking(_app: Path, _request: Path, _response: Path) -> None:
            time.sleep(0.25)

        probe = FileNonceHealthProbe(self.health, blocking, poll_interval_seconds=0.005)
        adapter = MacOSDevelopmentUpdaterAdapter(
            installed_app=self.installed_app,
            backup_root=self.backups,
            health_probe=probe,
            development_mode=True,
            frozen=False,
        )

        started = time.monotonic()
        result = self._coordinator(adapter).apply(self._staged())
        elapsed = time.monotonic() - started

        self.assertEqual(result.status, TransactionStatus.FAILED)
        self.assertLess(elapsed, 0.18)

    def test_stale_or_wrong_health_nonce_cannot_authorize_candidate(self) -> None:
        def wrong_nonce(_app: Path, request: Path, response: Path) -> None:
            if read_macos_app_identity(_app) == SOURCE:
                self._healthy_launcher(_app, request, response)
                return
            self._healthy_launcher(_app, request, response)
            document = json.loads(response.read_text(encoding="utf-8"))
            document["nonce"] = "0" * 64
            response.write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

        probe = FileNonceHealthProbe(self.health, wrong_nonce, poll_interval_seconds=0.005)
        adapter = MacOSDevelopmentUpdaterAdapter(
            installed_app=self.installed_app,
            backup_root=self.backups,
            health_probe=probe,
            development_mode=True,
            frozen=False,
        )

        result = self._coordinator(adapter).apply(self._staged())

        self.assertEqual(result.status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)

    def test_requested_target_with_source_runtime_identity_cannot_authorize(self) -> None:
        def stale_runtime(_app: Path, request: Path, response: Path) -> None:
            write_health_response(
                request,
                response,
                actual_product_id=PRODUCT_ID,
                actual_bundle_id=BUNDLE_ID,
                actual_identity=SOURCE,
                healthy=True,
            )

        probe = FileNonceHealthProbe(self.health, stale_runtime, poll_interval_seconds=0.005)
        adapter = MacOSDevelopmentUpdaterAdapter(
            installed_app=self.installed_app,
            backup_root=self.backups,
            health_probe=probe,
            development_mode=True,
            frozen=False,
        )

        result = self._coordinator(adapter).apply(self._staged())

        self.assertEqual(result.status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)

    def test_interruption_after_old_app_displacement_recovers_from_persisted_backup(self) -> None:
        def interrupt(phase: str) -> None:
            if phase == "after_displace":
                raise SystemExit("simulated power loss")

        with self.assertRaises(SystemExit):
            self._coordinator(self._adapter(phase_hook=interrupt)).apply(self._staged())
        self.assertTrue(self.journal.is_file())
        self.assertFalse(self.installed_app.exists())

        restarted = self._coordinator(self._adapter())
        result = restarted.recover()

        self.assertEqual(result.status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)
        self.assertFalse(self.journal.exists())
        self.assertEqual(tuple(self.install_parent.glob(".jarvis-old-*.app")), ())

    def test_interruption_after_candidate_replace_recovers_and_removes_displaced_app(self) -> None:
        def interrupt(phase: str) -> None:
            if phase == "after_candidate_replace":
                raise SystemExit("simulated power loss")

        with self.assertRaises(SystemExit):
            self._coordinator(self._adapter(phase_hook=interrupt)).apply(self._staged())
        self.assertEqual(read_macos_app_identity(self.installed_app), TARGET)

        result = self._coordinator(self._adapter()).recover()

        self.assertEqual(result.status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)
        self.assertEqual(tuple(self.install_parent.glob(".jarvis-old-*.app")), ())

    def test_corrupt_persisted_backup_cannot_report_verified_rollback(self) -> None:
        def interrupt(phase: str) -> None:
            if phase == "after_candidate_replace":
                raise SystemExit("simulated power loss")

        with self.assertRaises(SystemExit):
            self._coordinator(self._adapter(phase_hook=interrupt)).apply(self._staged())
        backup_executable = (
            next(self.backups.glob("backup-*/JARVIS.app"))
            / "Contents"
            / "MacOS"
            / "JARVIS"
        )
        backup_executable.write_bytes(b"corrupt backup executable")

        result = self._coordinator(self._adapter()).recover()

        self.assertEqual(result.status, TransactionStatus.ROLLBACK_REQUIRED)
        self.assertTrue(self.journal.exists())
        self.assertEqual(read_macos_app_identity(self.installed_app), TARGET)

    def test_rollback_failure_keeps_checkpoint_and_blocks_retry(self) -> None:
        def fail_rollback(phase: str) -> None:
            if phase == "before_rollback_replace":
                raise OSError("injected rollback failure")

        failed = self._coordinator(
            self._adapter(healthy=False, phase_hook=fail_rollback)
        ).apply(self._staged())
        blocked = self._coordinator(self._adapter()).apply(self._staged())
        recovered = self._coordinator(self._adapter()).recover()

        self.assertEqual(failed.status, TransactionStatus.ROLLBACK_REQUIRED)
        self.assertEqual(blocked.status, TransactionStatus.ROLLBACK_REQUIRED)
        self.assertEqual(recovered.status, TransactionStatus.ROLLED_BACK)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)

    def test_wrong_bundle_version_is_rejected_before_mutation(self) -> None:
        wrong_app = self.root / "wrong" / "JARVIS.app"
        _make_app(wrong_app, ProductVersion.parse("1.1.1", 11))
        wrong_archive = self.root / "wrong.zip"
        _write_app_zip(wrong_app, wrong_archive)

        result = self._coordinator(self._adapter()).apply(
            self._staged(path=wrong_archive)
        )

        self.assertEqual(result.status, TransactionStatus.PRESERVED)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)

    def test_corrupt_zip_is_rejected_before_mutation(self) -> None:
        corrupt = self.root / "corrupt.zip"
        corrupt.write_bytes(b"this is not a zip archive")

        result = self._coordinator(self._adapter()).apply(self._staged(path=corrupt))

        self.assertEqual(result.status, TransactionStatus.PRESERVED)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)

    def test_archive_path_alias_and_link_escape_attacks_are_preserved(self) -> None:
        attacks = {
            "parent": (_zip_info("../escape", stat.S_IFREG | 0o644), b"x"),
            "absolute": (_zip_info("/tmp/escape", stat.S_IFREG | 0o644), b"x"),
            "backslash": (_zip_info("JARVIS.app\\..\\escape", stat.S_IFREG | 0o644), b"x"),
            "duplicate": (
                _zip_info("JARVIS.app/Contents/MacOS/jarvis", stat.S_IFREG | 0o644),
                b"alias",
            ),
            "second_app": (_zip_info("Other.app/file", stat.S_IFREG | 0o644), b"x"),
            "link_escape": (
                _zip_info("JARVIS.app/Contents/Escape", stat.S_IFLNK | 0o777),
                b"../../outside",
            ),
            "link_cycle": (
                _zip_info("JARVIS.app/Contents/Cycle", stat.S_IFLNK | 0o777),
                b".",
            ),
            "empty_component": (
                _zip_info("JARVIS.app//Contents/alias", stat.S_IFREG | 0o644),
                b"alias",
            ),
        }
        for name, entry in attacks.items():
            with self.subTest(name=name):
                path = self._malicious_archive((entry,), name=f"attack-{name}.zip")
                journal = self.root / f"journal-{name}.json"
                result = self._coordinator(self._adapter(), journal=journal).apply(
                    self._staged(path=path)
                )
                self.assertEqual(result.status, TransactionStatus.PRESERVED)
                self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)

    def test_archive_rejects_entry_beneath_symlink_parent(self) -> None:
        link = _zip_info("JARVIS.app/Contents/Trap", stat.S_IFLNK | 0o777)
        child = _zip_info("JARVIS.app/Contents/Trap/payload", stat.S_IFREG | 0o644)
        path = self._malicious_archive(
            ((link, b"Resources"), (child, b"payload")),
            name="link-parent.zip",
        )

        result = self._coordinator(self._adapter()).apply(self._staged(path=path))

        self.assertEqual(result.status, TransactionStatus.PRESERVED)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)

    def test_raw_nul_archive_name_is_rejected_before_zipfile_truncation_alias(self) -> None:
        marker = _zip_info("JARVIS.app/Contents/Resources/NULX", stat.S_IFREG | 0o644)
        path = self._malicious_archive(((marker, b"payload"),), name="nul-name.zip")
        raw = path.read_bytes()
        self.assertEqual(raw.count(b"NULX"), 2)
        path.write_bytes(raw.replace(b"NULX", b"NU\x00X"))

        result = self._coordinator(self._adapter()).apply(self._staged(path=path))

        self.assertEqual(result.status, TransactionStatus.PRESERVED)
        self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)

    def test_archive_count_member_and_expanded_limits_fail_closed(self) -> None:
        huge = _zip_info("JARVIS.app/Contents/Resources/huge", stat.S_IFREG | 0o644)
        path = self._malicious_archive(((huge, b"A" * 4096),), name="bomb.zip")
        part_one = _zip_info("JARVIS.app/Contents/Resources/part-one", stat.S_IFREG | 0o644)
        part_two = _zip_info("JARVIS.app/Contents/Resources/part-two", stat.S_IFREG | 0o644)
        total_path = self._malicious_archive(
            ((part_one, b"A" * 1200), (part_two, b"B" * 1200)),
            name="expanded-bomb.zip",
        )
        cases = (
            (
                path,
                MacOSArchiveLimits(
                    max_entries=8192,
                    max_member_bytes=2048,
                    max_expanded_bytes=1024 * 1024,
                    max_compression_ratio=200,
                ),
            ),
            (
                path,
                MacOSArchiveLimits(
                    max_entries=3,
                    max_member_bytes=1024 * 1024,
                    max_expanded_bytes=1024 * 1024,
                    max_compression_ratio=200,
                ),
            ),
            (
                total_path,
                MacOSArchiveLimits(
                    max_entries=8192,
                    max_member_bytes=2048,
                    max_expanded_bytes=2048,
                    max_compression_ratio=200,
                ),
            ),
            (
                path,
                MacOSArchiveLimits(
                    max_entries=8192,
                    max_member_bytes=1024 * 1024,
                    max_expanded_bytes=1024 * 1024,
                    max_compression_ratio=2,
                ),
            ),
        )
        for index, (selected_path, limits) in enumerate(cases):
            with self.subTest(limits=limits):
                journal = self.root / f"limit-{index}.json"
                result = self._coordinator(
                    self._adapter(limits=limits), journal=journal
                ).apply(self._staged(path=selected_path))
                self.assertEqual(result.status, TransactionStatus.PRESERVED)
                self.assertEqual(read_macos_app_identity(self.installed_app), SOURCE)

    def test_downgrade_and_repeated_install_do_not_mutate(self) -> None:
        downgrade = self._staged(source=SOURCE, target=ProductVersion.parse("0.9.0", 9))
        invalid = self._coordinator(self._adapter()).apply(downgrade)
        installed = self._coordinator(
            self._adapter(), journal=self.root / "first.json"
        ).apply(self._staged())
        repeated = self._coordinator(
            self._adapter(), journal=self.root / "repeat.json"
        ).apply(self._staged())

        self.assertEqual(invalid.status, TransactionStatus.INVALID)
        self.assertEqual(installed.status, TransactionStatus.INSTALLED)
        self.assertEqual(repeated.status, TransactionStatus.FAILED)
        self.assertEqual(read_macos_app_identity(self.installed_app), TARGET)

    def test_info_plist_symlink_is_rejected_without_following_it(self) -> None:
        external = self.root / "external.plist"
        external.write_bytes((self.installed_app / "Contents" / "Info.plist").read_bytes())
        plist = self.installed_app / "Contents" / "Info.plist"
        plist.unlink()
        os.symlink(external, plist)

        with self.assertRaises(ValueError):
            read_macos_app_identity(self.installed_app)

    def test_development_adapter_cannot_be_enabled_in_frozen_runtime(self) -> None:
        probe = FileNonceHealthProbe(self.health, self._healthy_launcher)
        with self.assertRaises(RuntimeError):
            MacOSDevelopmentUpdaterAdapter(
                installed_app=self.installed_app,
                backup_root=self.backups,
                health_probe=probe,
                development_mode=True,
                frozen=True,
            )
        marker = object()
        previous = getattr(sys, "frozen", marker)
        try:
            sys.frozen = True
            with self.assertRaises(RuntimeError):
                MacOSDevelopmentUpdaterAdapter(
                    installed_app=self.installed_app,
                    backup_root=self.backups,
                    health_probe=probe,
                    development_mode=True,
                    frozen=False,
                )
        finally:
            if previous is marker:
                del sys.frozen
            else:
                sys.frozen = previous

    def test_development_storage_cannot_overlap_bundle_or_repermission_existing_dir(self) -> None:
        inside_probe = FileNonceHealthProbe(
            self.installed_app / "health",
            self._healthy_launcher,
        )
        with self.assertRaises(ValueError):
            MacOSDevelopmentUpdaterAdapter(
                installed_app=self.installed_app,
                backup_root=self.backups,
                health_probe=inside_probe,
                development_mode=True,
                frozen=False,
            )

        public_backup = self.install_parent / "public-backups"
        public_backup.mkdir(mode=0o755)
        public_backup.chmod(0o755)
        adapter = MacOSDevelopmentUpdaterAdapter(
            installed_app=self.installed_app,
            backup_root=public_backup,
            health_probe=FileNonceHealthProbe(self.health, self._healthy_launcher),
            development_mode=True,
            frozen=False,
        )

        self.assertEqual(adapter.capability().status, AdapterStatus.NOT_AVAILABLE)
        self.assertEqual(public_backup.stat().st_mode & 0o777, 0o755)

    def test_journal_inside_mutated_app_or_install_directory_is_rejected(self) -> None:
        adapter = self._adapter()
        invalid_paths = (
            self.installed_app / "Contents" / "update-journal.json",
            self.install_parent / "update-journal.json",
            self.backups / "update-journal.json",
            self.health / "update-journal.json",
        )
        for path in invalid_paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                UpdateTransactionCoordinator(
                    adapter,
                    journal_path=path,
                    health_timeout_seconds=0.08,
                )

    def test_unsafe_existing_journal_parent_is_not_repermissioned(self) -> None:
        public_parent = self.root / "public-journal"
        public_parent.mkdir(mode=0o755)
        public_parent.chmod(0o755)
        coordinator = UpdateTransactionCoordinator(
            self._adapter(),
            journal_path=public_parent / "journal.json",
            health_timeout_seconds=0.08,
        )

        result = coordinator.apply(self._staged())

        self.assertEqual(result.status, TransactionStatus.ROLLBACK_REQUIRED)
        self.assertEqual(public_parent.stat().st_mode & 0o777, 0o755)


class ProductionMacOSHelperTests(unittest.TestCase):
    TEAM_ID = "ABCDEFGHIJ"
    REQUIREMENT = 'identifier "com.jarvis.assistant.updater" and anchor apple generic'

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.helper = self.root / "updater-helper"
        self.helper.write_bytes(b"not a real signed helper")
        self.helper.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _valid_runner(self, calls: list[tuple[str, ...]]):
        def runner(argv: tuple[str, ...], _timeout: float) -> HelperCommandResult:
            calls.append(argv)
            if "-dv" in argv:
                return HelperCommandResult(0, stderr=f"TeamIdentifier={self.TEAM_ID}\n")
            if "-dr" in argv:
                return HelperCommandResult(0, stderr=f"designated => {self.REQUIREMENT}\n")
            return HelperCommandResult(0)

        return runner

    def test_missing_unsafe_and_unsigned_helpers_are_not_available(self) -> None:
        missing = assess_production_macos_helper(
            self.root / "missing",
            expected_team_id=self.TEAM_ID,
            designated_requirement=self.REQUIREMENT,
            frozen=True,
            required_uid=os.getuid(),
        )
        self.helper.chmod(0o722)
        unsafe = assess_production_macos_helper(
            self.helper,
            expected_team_id=self.TEAM_ID,
            designated_requirement=self.REQUIREMENT,
            frozen=True,
            required_uid=os.getuid(),
        )
        self.helper.chmod(0o700)
        unsigned = assess_production_macos_helper(
            self.helper,
            expected_team_id=self.TEAM_ID,
            designated_requirement=self.REQUIREMENT,
            frozen=True,
            runner=lambda _argv, _timeout: HelperCommandResult(1),
            required_uid=os.getuid(),
        )

        self.assertFalse(missing.trusted)
        self.assertEqual(missing.blocker, "signed_helper_not_installed")
        self.assertEqual(unsafe.blocker, "helper_file_metadata_is_unsafe")
        self.assertEqual(unsigned.blocker, "helper_signature_or_notarization_invalid")

    def test_hardlinked_or_replaced_helper_fails_inode_pin(self) -> None:
        linked = self.root / "linked-helper"
        os.link(self.helper, linked)
        hardlinked = assess_production_macos_helper(
            self.helper,
            expected_team_id=self.TEAM_ID,
            designated_requirement=self.REQUIREMENT,
            frozen=True,
            required_uid=os.getuid(),
        )
        linked.unlink()

        calls = 0

        def replacing_runner(argv: tuple[str, ...], _timeout: float) -> HelperCommandResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                replacement = self.root / "replacement"
                replacement.write_bytes(b"replacement helper")
                replacement.chmod(0o700)
                os.replace(replacement, self.helper)
            if "-dv" in argv:
                return HelperCommandResult(0, stderr=f"TeamIdentifier={self.TEAM_ID}\n")
            if "-dr" in argv:
                return HelperCommandResult(0, stderr=f"designated => {self.REQUIREMENT}\n")
            return HelperCommandResult(0)

        replaced = assess_production_macos_helper(
            self.helper,
            expected_team_id=self.TEAM_ID,
            designated_requirement=self.REQUIREMENT,
            frozen=True,
            runner=replacing_runner,
            required_uid=os.getuid(),
        )

        self.assertEqual(hardlinked.blocker, "helper_file_metadata_is_unsafe")
        self.assertEqual(replaced.blocker, "helper_changed_during_validation")

    def test_validation_invokes_fixed_codesign_spctl_and_stapler_checks(self) -> None:
        calls: list[tuple[str, ...]] = []

        assessment = assess_production_macos_helper(
            self.helper,
            expected_team_id=self.TEAM_ID,
            designated_requirement=self.REQUIREMENT,
            frozen=True,
            runner=self._valid_runner(calls),
            required_uid=os.getuid(),
        )

        self.assertTrue(assessment.trusted)
        self.assertIsNone(assessment.blocker)
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0][0], "/usr/bin/codesign")
        self.assertEqual(calls[3][0], "/usr/sbin/spctl")
        self.assertEqual(calls[4][:3], ("/usr/bin/xcrun", "stapler", "validate"))
        self.assertTrue(all(call[-1] == str(self.helper) for call in calls))

    def test_wrong_team_requirement_and_nonfrozen_runtime_fail_closed(self) -> None:
        wrong_team = assess_production_macos_helper(
            self.helper,
            expected_team_id="ZZZZZZZZZZ",
            designated_requirement=self.REQUIREMENT,
            frozen=True,
            runner=self._valid_runner([]),
            required_uid=os.getuid(),
        )
        wrong_requirement = assess_production_macos_helper(
            self.helper,
            expected_team_id=self.TEAM_ID,
            designated_requirement="identifier wrong and anchor apple generic",
            frozen=True,
            runner=self._valid_runner([]),
            required_uid=os.getuid(),
        )
        source_runtime = assess_production_macos_helper(
            self.helper,
            expected_team_id=self.TEAM_ID,
            designated_requirement=self.REQUIREMENT,
            frozen=False,
            runner=self._valid_runner([]),
            required_uid=os.getuid(),
        )

        self.assertEqual(wrong_team.blocker, "helper_team_id_mismatch")
        self.assertEqual(wrong_requirement.blocker, "helper_designated_requirement_mismatch")
        self.assertEqual(source_runtime.blocker, "frozen_runtime_required")

    def test_default_macos_factory_never_selects_development_mutation(self) -> None:
        adapter = create_updater_adapter("Darwin")
        capability = adapter.capability()

        self.assertIsInstance(adapter, MacOSUpdaterAdapter)
        self.assertEqual(capability.status, AdapterStatus.NOT_AVAILABLE)
        self.assertFalse(capability.mutation_enabled)
        self.assertIsNotNone(capability.blocker)


if __name__ == "__main__":
    unittest.main()
