from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ops import backup as backup_mod
from ops import gen_secrets, migrate, restore, rotate, validate_config
from ops import _common
from ops._common import OpsNotAvailableError, POSIX, UnsafePathError
from product_backend.admin_credentials import SQLiteAdminCredentialStore
from product_backend.admin_mfa import MfaSecretCipher, SQLiteAdminMfaManager
from product_backend.api_activation import SQLiteClientActivationService
from product_backend.api_auth import (
    MIN_PBKDF2_ITERATIONS,
    AdminPasswordCredential,
)
from product_backend.api_signing import InjectedEd25519EntitlementSigner
from product_backend.device_challenges import SQLiteDeviceChallengeService
from product_backend.migrations import KNOWN_DATABASES
from product_backend.sqlite_repository import SQLiteCommerceRepository


def _make_dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    if POSIX:
        os.chmod(path, mode)
    return path


def _full_env(root: Path) -> dict[str, str]:
    secrets_dir = _make_dir(root / "secrets")
    data_dir = _make_dir(root / "data")
    artifact_dir = _make_dir(root / "artifacts")
    bundle = gen_secrets.generate_secret_bundle(
        secrets_dir,
        admin_subject="admin:ops",
        allowed_hosts="product.example.com,localhost",
        admin_password="a-strong-admin-password",
    )
    env = dict(bundle.env)
    env["JARVIS_BACKEND_DATA_DIR"] = str(data_dir)
    env["JARVIS_RELEASE_ARTIFACT_ROOT"] = str(artifact_dir)
    return env


@unittest.skipUnless(POSIX, "secure path tests require POSIX no-follow support")
class SecureFileWriteTests(unittest.TestCase):
    def test_private_directory_refuses_root_and_system_root_without_chmod(self) -> None:
        with mock.patch.object(_common.os, "fchmod") as chmod:
            for path in (Path("/"), Path("/tmp")):
                with self.subTest(path=path), self.assertRaises(UnsafePathError):
                    _common.ensure_private_directory(path)
            chmod.assert_not_called()

    def test_private_directory_refuses_non_effective_user_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "existing"
            path.mkdir()
            wrong_uid = path.stat().st_uid + 1
            with mock.patch.object(
                _common.os,
                "geteuid",
                return_value=wrong_uid,
            ), mock.patch.object(_common.os, "fchmod") as chmod:
                with self.assertRaises(UnsafePathError):
                    _common.ensure_private_directory(path)
                chmod.assert_not_called()

    def test_permission_failure_raises_instead_of_returning_manual_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private"
            with mock.patch.object(
                _common.os,
                "fchmod",
                side_effect=OSError("denied"),
            ):
                with self.assertRaises(PermissionError):
                    _common.ensure_private_directory(path)

    def test_secret_write_is_create_only_owner_only_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nested" / "secret"
            result = _common.write_secret_bytes(path, b"first")
            self.assertEqual(result.status, "applied")
            self.assertEqual(path.read_bytes(), b"first")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                _common.write_secret_bytes(path, b"second")
            self.assertEqual(path.read_bytes(), b"first")
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_secret_write_rejects_symlink_destination_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside"
            outside.write_bytes(b"do-not-touch")
            destination = root / "secret"
            destination.symlink_to(outside)
            with self.assertRaises(UnsafePathError):
                _common.write_secret_bytes(destination, b"leaked", overwrite=True)
            self.assertEqual(outside.read_bytes(), b"do-not-touch")

    def test_secret_write_rejects_hardlink_destination_without_touching_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside"
            outside.write_bytes(b"do-not-touch")
            destination = root / "secret"
            os.link(outside, destination)
            with self.assertRaises(UnsafePathError):
                _common.write_secret_bytes(destination, b"leaked", overwrite=True)
            self.assertEqual(outside.read_bytes(), b"do-not-touch")

    def test_secret_write_rejects_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = _make_dir(root / "outside")
            parent = root / "linked-parent"
            parent.symlink_to(outside, target_is_directory=True)
            with self.assertRaises((UnsafePathError, OSError)):
                _common.write_secret_bytes(parent / "secret", b"leaked")
            self.assertFalse((outside / "secret").exists())

    def test_stable_copy_rejects_hardlinked_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.write_bytes(b"private")
            os.link(source, root / "second-link")
            with self.assertRaises(UnsafePathError):
                _common.copy_stable_file(
                    source,
                    root / "copy",
                    max_bytes=100,
                )


@unittest.skipUnless(POSIX, "ops hardening tests assume POSIX permissions")
class GenSecretsTests(unittest.TestCase):
    def test_generated_files_are_owner_only_and_config_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = _full_env(root)
            for name in (
                "JARVIS_ENTITLEMENT_PRIVATE_KEY_FILE",
                "JARVIS_ACTIVATION_PEPPER_FILE",
                "JARVIS_ADMIN_MFA_KEY_FILE",
            ):
                mode = stat.S_IMODE(Path(env[name]).stat().st_mode)
                self.assertEqual(mode, 0o600, name)
            report = validate_config.validate(env, build=True)
            self.assertTrue(report.ok, report.errors)

    def test_evidence_directory_is_owner_only_after_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = _full_env(root)
            report = validate_config.validate(env, build=True)
            self.assertTrue(report.ok, report.errors)
            evidence = Path(env["JARVIS_BACKEND_DATA_DIR"]) / "payment-evidence"
            self.assertTrue(evidence.is_dir())
            self.assertEqual(evidence.stat().st_mode & 0o077, 0)

    def test_admin_allowlist_config_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = _full_env(Path(temp))
            env["JARVIS_ADMIN_ALLOWED_NETWORKS"] = "10.0.0.0/8,192.168.1.0/24"
            report = validate_config.validate(env, build=True)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.warnings, [])

    def test_generated_bundle_repr_does_not_contain_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = gen_secrets.generate_secret_bundle(
                Path(temp) / "secrets",
                admin_subject="admin:ops",
                allowed_hosts="product.example.com",
                admin_password="known-test-password-that-must-not-appear",
            )
            diagnostic = repr(bundle)
            self.assertNotIn(
                bundle.env["JARVIS_ADMIN_SESSION_SECRET_B64URL"],
                diagnostic,
            )
            self.assertNotIn(
                bundle.env["JARVIS_ADMIN_PASSWORD_HASH_B64URL"],
                diagnostic,
            )
            self.assertNotIn("known-test-password", diagnostic)
            self.assertEqual(bundle.env["JARVIS_REQUIRE_HTTPS"], "true")

    def test_cli_writes_env_and_generated_password_without_printing_either(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out_dir = Path(temp) / "secrets"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = gen_secrets.main(
                    [
                        "--out-dir",
                        str(out_dir),
                        "--admin-subject",
                        "admin:ops",
                        "--allowed-hosts",
                        "product.example.com",
                    ]
                )
            self.assertEqual(result, 0)
            env_path = out_dir / gen_secrets.DEFAULT_ENV_FILENAME
            password_path = out_dir / gen_secrets.INITIAL_PASSWORD_FILENAME
            self.assertTrue(env_path.is_file())
            self.assertTrue(password_path.is_file())
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(password_path.stat().st_mode), 0o600)
            env_secret = next(
                line.split("=", 1)[1].strip('"')
                for line in env_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("JARVIS_ADMIN_SESSION_SECRET_B64URL=")
            )
            password = password_path.read_text(encoding="utf-8").rstrip("\n")
            output = stdout.getvalue() + stderr.getvalue()
            self.assertNotIn(env_secret, output)
            self.assertNotIn(password, output)

    def test_cli_reads_owner_only_password_file_without_echoing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            password = "owner-only-test-password-5Qw9"
            password_path = root / "password.input"
            password_path.write_text(password + "\n", encoding="utf-8")
            os.chmod(password_path, 0o600)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = gen_secrets.main(
                    [
                        "--out-dir",
                        str(root / "secrets"),
                        "--admin-subject",
                        "admin:ops",
                        "--allowed-hosts",
                        "product.example.com",
                        "--admin-password-file",
                        str(password_path),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertNotIn(password, stdout.getvalue() + stderr.getvalue())
            self.assertFalse(
                (root / "secrets" / gen_secrets.INITIAL_PASSWORD_FILENAME).exists()
            )

    def test_legacy_cli_password_forms_are_rejected_without_echo(self) -> None:
        secret = "legacy-argv-secret-must-not-echo"
        for arguments in (
            ["--admin-password", secret],
            [f"--admin-password={secret}"],
        ):
            with self.subTest(arguments=arguments[0]):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = gen_secrets.main(arguments)
                self.assertEqual(result, 2)
                self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())

    def test_cli_never_prints_secret_embedded_in_exception(self) -> None:
        secret = "exception-secret-must-not-echo"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            gen_secrets,
            "generate_secret_bundle",
            side_effect=RuntimeError(secret),
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = gen_secrets.main(
                [
                    "--out-dir",
                    "/tmp/jarvis-test-secret-generation",
                    "--admin-subject",
                    "admin:ops",
                    "--allowed-hosts",
                    "product.example.com",
                ]
            )
        self.assertEqual(result, 1)
        self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())

    def test_secret_generation_is_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            arguments = [
                "--out-dir",
                str(root / "secrets"),
                "--admin-subject",
                "admin:ops",
                "--allowed-hosts",
                "product.example.com",
            ]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                self.assertEqual(gen_secrets.main(arguments), 0)
            env_path = root / "secrets" / gen_secrets.DEFAULT_ENV_FILENAME
            before = hashlib.sha256(env_path.read_bytes()).hexdigest()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                self.assertEqual(gen_secrets.main(arguments), 1)
            self.assertEqual(hashlib.sha256(env_path.read_bytes()).hexdigest(), before)

    def test_https_disable_switch_is_not_available(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            gen_secrets.main(
                [
                    "--out-dir",
                    "/tmp/unused",
                    "--admin-subject",
                    "admin:ops",
                    "--allowed-hosts",
                    "product.example.com",
                    "--no-require-https",
                ]
            )


class OpsOutputBoundaryTests(unittest.TestCase):
    def test_all_sensitive_output_inside_repository_is_rejected_before_write(self) -> None:
        repository = Path(_common.__file__).resolve().parents[1]
        forbidden = repository / ".ops-output-boundary-test"
        self.assertFalse(forbidden.exists())
        with self.assertRaises(UnsafePathError):
            _common.write_secret_bytes(forbidden / "secret", b"secret")
        with self.assertRaises(UnsafePathError):
            gen_secrets.generate_secret_bundle(
                forbidden / "generated",
                admin_subject="admin:ops",
                allowed_hosts="product.example.com",
                admin_password="test-password-not-printed",
            )
        with self.assertRaises(UnsafePathError):
            rotate.rotate("session-secret", forbidden / "rotation")
        with self.assertRaises(UnsafePathError):
            backup_mod.backup(
                Path("/nonexistent-source-is-not-read"),
                forbidden / "backup",
                service_stopped=True,
            )
        with self.assertRaises(UnsafePathError):
            restore.restore(
                Path("/nonexistent-backup-is-not-read"),
                forbidden / "restore",
            )
        self.assertFalse(forbidden.exists())

    def test_env_file_inside_repository_is_rejected_before_bundle_generation(self) -> None:
        repository = Path(_common.__file__).resolve().parents[1]
        forbidden_env = repository / ".forbidden-backend.env"
        with tempfile.TemporaryDirectory() as temp:
            out_dir = Path(temp) / "secrets"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                status = gen_secrets.main(
                    [
                        "--out-dir",
                        str(out_dir),
                        "--admin-subject",
                        "admin:ops",
                        "--allowed-hosts",
                        "product.example.com",
                        "--env-file",
                        str(forbidden_env),
                    ]
                )
            self.assertEqual(status, 1)
            self.assertFalse(out_dir.exists())
            self.assertFalse(forbidden_env.exists())

    def test_case_insensitive_repository_alias_is_rejected_before_write(self) -> None:
        repository = Path(_common.__file__).resolve().parents[1]
        alias = None
        parts = list(repository.parts)
        for index, part in enumerate(parts):
            alternate = part.swapcase()
            if not alternate or alternate == part:
                continue
            candidate = Path(*parts[:index], alternate, *parts[index + 1 :])
            try:
                if candidate.exists() and os.path.samefile(candidate, repository):
                    alias = candidate
                    break
            except OSError:
                continue
        if alias is None:
            self.skipTest("filesystem is case-sensitive for the repository path")

        forbidden = alias / ".case-alias-output-boundary-test"
        self.assertFalse(forbidden.exists())
        with self.assertRaises(UnsafePathError):
            _common.write_secret_bytes(forbidden / "secret", b"secret")
        self.assertFalse(forbidden.exists())

    def test_non_posix_secure_ops_fail_not_available_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(_common, "POSIX", False):
                with self.assertRaises(OpsNotAvailableError):
                    _common.write_secret_bytes(root / "direct", b"secret")
                with self.assertRaises(OpsNotAvailableError):
                    gen_secrets.generate_secret_bundle(
                        root / "generated",
                        admin_subject="admin:ops",
                        allowed_hosts="product.example.com",
                        admin_password="test-password-not-printed",
                    )
                with self.assertRaises(OpsNotAvailableError):
                    rotate.rotate("session-secret", root / "rotation")
                with self.assertRaises(OpsNotAvailableError):
                    backup_mod.backup(
                        root / "data",
                        root / "backup",
                        service_stopped=True,
                    )
                with self.assertRaises(OpsNotAvailableError):
                    restore.restore(root / "backup", root / "restore")
            self.assertEqual(list(root.iterdir()), [])


class ValidateConfigTests(unittest.TestCase):
    def test_missing_config_fails_closed(self) -> None:
        report = validate_config.validate({}, build=False)
        self.assertFalse(report.ok)
        self.assertTrue(report.errors)

    def test_wildcard_host_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = _full_env(Path(temp))
            env["JARVIS_API_ALLOWED_HOSTS"] = "*"
            report = validate_config.validate(env, build=False)
            self.assertFalse(report.ok)
            self.assertTrue(
                any("wildcard" in error for error in report.errors)
            )

    def test_password_only_mfa_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = _full_env(Path(temp))
            env["JARVIS_ADMIN_MFA_ALLOW_PASSWORD_ONLY"] = "true"
            report = validate_config.validate(env, build=False)
            self.assertFalse(report.ok)

    def test_env_file_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = _full_env(root)
            env_file = root / "backend.env"
            lines = [f'{key}={_quote(value)}' for key, value in env.items()]
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            loaded = validate_config.load_env_file(env_file)
            self.assertEqual(loaded["JARVIS_ADMIN_SUBJECT"], "admin:ops")
            self.assertEqual(
                loaded["JARVIS_RELEASE_PUBLIC_KEYS_JSON"],
                env["JARVIS_RELEASE_PUBLIC_KEYS_JSON"],
            )


def _quote(value: str) -> str:
    import json

    return json.dumps(value)


class BackupRestoreTests(unittest.TestCase):
    def _seed_commerce(self, data_dir: Path) -> None:
        with contextlib.ExitStack() as stack:
            commerce = SQLiteCommerceRepository(data_dir / "commerce.sqlite3")
            stack.callback(commerce.close)
            commerce.create_release("1.2.3", price_minor=4999, currency="USD")

            credential = AdminPasswordCredential(
                "admin:ops-test",
                b"s" * 32,
                b"d" * 32,
                MIN_PBKDF2_ITERATIONS,
            )
            credentials = SQLiteAdminCredentialStore(
                data_dir / "admin-credentials.sqlite3",
                (credential,),
            )
            stack.callback(credentials.close)

            challenges = SQLiteDeviceChallengeService(
                commerce,
                data_dir / "device-challenges.sqlite3",
            )
            stack.callback(challenges.close)

            activation = SQLiteClientActivationService(
                commerce,
                InjectedEd25519EntitlementSigner(
                    Ed25519PrivateKey.generate(),
                    key_id="ops-test-entitlement-key",
                ),
                b"ops-test-activation-pepper-32-bytes",
                data_dir / "activation.sqlite3",
            )
            stack.callback(activation.close)

            mfa = SQLiteAdminMfaManager(
                MfaSecretCipher(b"m" * 32),
                data_dir / "admin-mfa.sqlite3",
            )
            stack.callback(mfa.close)

        _make_dir(data_dir / "payment-evidence")

    def _backup(self, data_dir: Path, backup_dir: Path):
        return backup_mod.backup(
            data_dir,
            backup_dir,
            service_stopped=True,
        )

    def _release_count(self, data_dir: Path) -> int:
        path = data_dir / "commerce.sqlite3"
        connection = sqlite3.connect(
            f"{path.absolute().as_uri()}?mode=ro",
            uri=True,
        )
        try:
            return connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
        finally:
            connection.close()

    def _manifest(self, backup_dir: Path) -> dict[str, object]:
        return json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))

    def _write_manifest(self, backup_dir: Path, document: dict[str, object]) -> None:
        (backup_dir / "manifest.json").write_text(
            json.dumps(document, sort_keys=True),
            encoding="utf-8",
        )

    def test_database_schema_validator_rejects_missing_index_and_trigger(self) -> None:
        mutations = (
            "DROP INDEX payment_client_submission_identity",
            "DROP TRIGGER entitlement_requires_approved_payment",
        )
        for statement in mutations:
            with (
                self.subTest(statement=statement),
                tempfile.TemporaryDirectory() as temp,
            ):
                data_dir = _make_dir(Path(temp) / "data")
                self._seed_commerce(data_dir)
                database = data_dir / "commerce.sqlite3"
                connection = sqlite3.connect(database)
                try:
                    connection.execute(statement)
                    connection.commit()
                finally:
                    connection.close()
                with self.assertRaisesRegex(RuntimeError, "schema"):
                    backup_mod.validate_database_snapshot(
                        database,
                        "commerce.sqlite3",
                    )

    def test_backup_and_restore_round_trip_preserves_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            # Seed a payment-evidence object too.
            evidence = _make_dir(data_dir / "payment-evidence")
            (evidence / "obj-001.bin").write_bytes(b"evidence-bytes")

            backup_dir = root / "backup"
            manifest = self._backup(data_dir, backup_dir)
            self.assertTrue((backup_dir / "manifest.json").is_file())
            self.assertTrue(len(manifest.entries) >= 2)

            target = root / "restored"
            restored = restore.restore(backup_dir, target)
            self.assertEqual(restored, len(manifest.entries))
            self.assertEqual(self._release_count(target), 1)
            self.assertEqual(
                (target / "payment-evidence" / "obj-001.bin").read_bytes(),
                b"evidence-bytes",
            )
            if POSIX:
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_atomic_publish_failure_leaves_no_target_or_staging_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)
            target = root / "restored"
            with mock.patch.object(
                restore,
                "publish_private_directory",
                side_effect=OSError("injected publish failure"),
            ):
                with self.assertRaisesRegex(restore.RestoreError, "publication"):
                    restore.restore(backup_dir, target)
            self.assertFalse(target.exists())
            self.assertEqual(list(root.glob(".jarvis-restore-*")), [])

    def test_empty_payment_evidence_root_is_preserved_by_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)

            target = root / "restored"
            restore.restore(backup_dir, target)
            evidence = target / "payment-evidence"
            self.assertTrue(evidence.is_dir())
            self.assertEqual(list(evidence.iterdir()), [])
            if POSIX:
                self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o700)

    def test_backup_requires_stopped_service_and_every_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            without_confirmation = root / "without-confirmation"
            with self.assertRaises(RuntimeError):
                backup_mod.backup(data_dir, without_confirmation)
            self.assertFalse(without_confirmation.exists())

            (data_dir / "payment-evidence").rmdir()
            missing_evidence = root / "missing-evidence"
            with self.assertRaises(FileNotFoundError):
                backup_mod.backup(
                    data_dir,
                    missing_evidence,
                    service_stopped=True,
                )
            self.assertFalse(missing_evidence.exists())
            _make_dir(data_dir / "payment-evidence")

            (data_dir / KNOWN_DATABASES[-1]).unlink()
            missing_database = root / "missing-database"
            with self.assertRaises(FileNotFoundError):
                backup_mod.backup(
                    data_dir,
                    missing_database,
                    service_stopped=True,
                )
            self.assertFalse(missing_database.exists())

    def test_backup_cli_requires_explicit_maintenance_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            target = root / "backup"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = backup_mod.main(
                    ["--data-dir", str(data_dir), "--backup-dir", str(target)]
                )
            self.assertEqual(status, 2)
            self.assertIn("confirm-service-stopped", output.getvalue())
            self.assertFalse(target.exists())

    def test_restore_detects_tampered_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)
            # Corrupt a backed-up database.
            with (backup_dir / "commerce.sqlite3").open("r+b") as handle:
                handle.seek(0)
                handle.write(b"XXXX")
            with self.assertRaises(restore.RestoreError):
                restore.verify_backup(backup_dir)

    def test_restore_refuses_existing_target_even_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)
            with self.assertRaises(restore.RestoreError):
                restore.restore(backup_dir, data_dir, force=False)
            with self.assertRaisesRegex(restore.RestoreError, "not_available"):
                restore.restore(backup_dir, data_dir, force=True)

    def test_online_backup_handles_uri_metacharacters_in_database_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data ?# with spaces")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup ?#"
            manifest = self._backup(data_dir, backup_dir)
            self.assertTrue(any(entry.relpath == "commerce.sqlite3" for entry in manifest.entries))
            target = root / "restored ?#"
            restore.restore(backup_dir, target)
            self.assertEqual(self._release_count(target), 1)

    @unittest.skipUnless(POSIX, "symlink/hardlink checks require POSIX")
    def test_backup_rejects_symlink_and_hardlink_evidence_sources(self) -> None:
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                data_dir = _make_dir(root / "data")
                self._seed_commerce(data_dir)
                evidence = _make_dir(data_dir / "payment-evidence")
                outside = root / "outside.bin"
                outside.write_bytes(b"private-outside-data")
                source = evidence / "object.bin"
                if kind == "symlink":
                    source.symlink_to(outside)
                else:
                    os.link(outside, source)
                with self.assertRaises((UnsafePathError, OSError)):
                    self._backup(data_dir, root / "backup")

    @unittest.skipUnless(POSIX, "hardlink checks require POSIX")
    def test_backup_rejects_hardlinked_database_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            os.link(data_dir / "commerce.sqlite3", root / "database-second-link")
            with self.assertRaises(UnsafePathError):
                self._backup(data_dir, root / "backup")

    def test_backup_enforces_evidence_size_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            evidence = _make_dir(data_dir / "payment-evidence")
            (evidence / "too-large.bin").write_bytes(b"1234")
            with mock.patch.object(backup_mod, "MAX_EVIDENCE_FILE_BYTES", 3):
                with self.assertRaises(ValueError):
                    self._backup(data_dir, root / "backup")

    def test_backup_rejects_casefold_colliding_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            _make_dir(data_dir / "payment-evidence")
            collision = [
                backup_mod.BackupEntry(
                    "payment-evidence/Foo.bin",
                    hashlib.sha256(b"").hexdigest(),
                    0,
                ),
                backup_mod.BackupEntry(
                    "payment-evidence/foo.bin",
                    hashlib.sha256(b"").hexdigest(),
                    0,
                ),
            ]
            with mock.patch.object(
                backup_mod,
                "_backup_evidence",
                return_value=collision,
            ):
                with self.assertRaisesRegex(ValueError, "collide"):
                    self._backup(data_dir, root / "backup")

    def test_restore_rejects_absolute_dot_dot_double_slash_and_backslash_paths(self) -> None:
        unsafe_paths = (
            "/absolute.sqlite3",
            "../commerce.sqlite3",
            "./commerce.sqlite3",
            "payment-evidence//object.bin",
            "payment-evidence\\object.bin",
            "payment-evidence/object:stream",
            "payment-evidence/CON.txt",
            "payment-evidence/trailing.",
            "unknown.sqlite3",
        )
        for unsafe in unsafe_paths:
            with self.subTest(path=unsafe), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                data_dir = _make_dir(root / "data")
                self._seed_commerce(data_dir)
                backup_dir = root / "backup"
                self._backup(data_dir, backup_dir)
                document = self._manifest(backup_dir)
                files = document["files"]
                self.assertIsInstance(files, dict)
                metadata = files.pop("commerce.sqlite3")
                files[unsafe] = metadata
                self._write_manifest(backup_dir, document)
                with self.assertRaises(restore.RestoreError):
                    restore.verify_backup(backup_dir)

    def test_restore_rejects_wrong_schema_unknown_fields_and_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)
            original = self._manifest(backup_dir)

            wrong_schema = dict(original)
            wrong_schema["schema"] = "jarvis.backend-backup.v0"
            self._write_manifest(backup_dir, wrong_schema)
            with self.assertRaises(restore.RestoreError):
                restore.verify_backup(backup_dir)

            unknown = dict(original)
            unknown["unexpected"] = True
            self._write_manifest(backup_dir, unknown)
            with self.assertRaises(restore.RestoreError):
                restore.verify_backup(backup_dir)

            valid_text = json.dumps(original)
            duplicate = valid_text.replace(
                '"schema": "jarvis.backend-backup.v1"',
                '"schema": "jarvis.backend-backup.v1", '
                '"schema": "jarvis.backend-backup.v1"',
                1,
            )
            (backup_dir / "manifest.json").write_text(duplicate, encoding="utf-8")
            with self.assertRaises(restore.RestoreError):
                restore.verify_backup(backup_dir)

    def test_restore_normalizes_deep_json_recursion_to_restore_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backup_dir = _make_dir(Path(temp) / "backup")
            deeply_nested = "[" * 10_000 + "0" + "]" * 10_000
            (backup_dir / "manifest.json").write_text(
                deeply_nested,
                encoding="utf-8",
            )
            with self.assertRaises(restore.RestoreError):
                restore.verify_backup(backup_dir)

    def test_restore_rejects_casefold_path_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)
            document = self._manifest(backup_dir)
            empty_metadata = {
                "sha256": hashlib.sha256(b"").hexdigest(),
                "bytes": 0,
            }
            document["files"]["payment-evidence/Foo.bin"] = empty_metadata
            document["files"]["payment-evidence/foo.bin"] = empty_metadata
            self._write_manifest(backup_dir, document)
            with self.assertRaisesRegex(restore.RestoreError, "collide"):
                restore.verify_backup(backup_dir)

    def test_restore_rejects_declared_size_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)
            document = self._manifest(backup_dir)
            metadata = document["files"]["commerce.sqlite3"]
            metadata["bytes"] -= 1
            self._write_manifest(backup_dir, document)
            with self.assertRaises(restore.RestoreError):
                restore.verify_backup(backup_dir)

    def test_restore_rejects_corrupt_sqlite_even_with_recomputed_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)
            corrupt = b"not a sqlite database, but hash metadata is internally valid"
            database = backup_dir / "commerce.sqlite3"
            database.write_bytes(corrupt)
            document = self._manifest(backup_dir)
            document["files"]["commerce.sqlite3"] = {
                "sha256": hashlib.sha256(corrupt).hexdigest(),
                "bytes": len(corrupt),
            }
            self._write_manifest(backup_dir, document)
            with self.assertRaisesRegex(restore.RestoreError, "SQLite"):
                restore.verify_backup(backup_dir)

    def test_restore_rejects_valid_sqlite_with_wrong_application_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)
            database = backup_dir / "admin-credentials.sqlite3"
            database.unlink()
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE unrelated (id INTEGER)")
                connection.commit()
            finally:
                connection.close()
            payload = database.read_bytes()
            document = self._manifest(backup_dir)
            document["files"][database.name] = {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
            self._write_manifest(backup_dir, document)
            with self.assertRaisesRegex(restore.RestoreError, "schema"):
                restore.verify_backup(backup_dir)

    def test_restore_rejects_expected_table_with_wrong_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)
            database = backup_dir / "admin-credentials.sqlite3"
            database.unlink()
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE admin_password_credentials (id INTEGER)"
                )
                connection.commit()
            finally:
                connection.close()
            payload = database.read_bytes()
            document = self._manifest(backup_dir)
            document["files"][database.name] = {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
            self._write_manifest(backup_dir, document)
            with self.assertRaisesRegex(restore.RestoreError, "schema"):
                restore.verify_backup(backup_dir)

    def test_restore_rejects_wrong_commerce_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)
            database = backup_dir / "commerce.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA user_version = 999")
            finally:
                connection.close()
            payload = database.read_bytes()
            document = self._manifest(backup_dir)
            document["files"][database.name] = {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
            self._write_manifest(backup_dir, document)
            with self.assertRaisesRegex(restore.RestoreError, "schema"):
                restore.verify_backup(backup_dir)

    @unittest.skipUnless(POSIX, "symlink/hardlink checks require POSIX")
    def test_restore_rejects_symlink_and_hardlink_backup_sources(self) -> None:
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                data_dir = _make_dir(root / "data")
                self._seed_commerce(data_dir)
                evidence = _make_dir(data_dir / "payment-evidence")
                (evidence / "object.bin").write_bytes(b"evidence")
                backup_dir = root / "backup"
                self._backup(data_dir, backup_dir)
                source = backup_dir / "payment-evidence" / "object.bin"
                outside = root / "outside"
                outside.write_bytes(source.read_bytes())
                source.unlink()
                if kind == "symlink":
                    source.symlink_to(outside)
                else:
                    os.link(outside, source)
                with self.assertRaises(restore.RestoreError):
                    restore.verify_backup(backup_dir)

    @unittest.skipUnless(POSIX, "symlink/hardlink checks require POSIX")
    def test_restore_rejects_unsafe_targets_before_replacing_any_file(self) -> None:
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source_data = _make_dir(root / "source-data")
                self._seed_commerce(source_data)
                source_evidence = _make_dir(source_data / "payment-evidence")
                (source_evidence / "object.bin").write_bytes(b"new-evidence")
                backup_dir = root / "backup"
                self._backup(source_data, backup_dir)

                target = _make_dir(root / "target")
                target_repo = SQLiteCommerceRepository(target / "commerce.sqlite3")
                target_repo.create_release("9.9.9", price_minor=1, currency="USD")
                target_repo.close()
                before = hashlib.sha256((target / "commerce.sqlite3").read_bytes()).hexdigest()
                target_evidence = _make_dir(target / "payment-evidence")
                outside = root / "outside"
                outside.write_bytes(b"outside-must-remain")
                unsafe_target = target_evidence / "object.bin"
                if kind == "symlink":
                    unsafe_target.symlink_to(outside)
                else:
                    os.link(outside, unsafe_target)

                with self.assertRaises(restore.RestoreError):
                    restore.restore(backup_dir, target, force=True)
                self.assertEqual(
                    hashlib.sha256((target / "commerce.sqlite3").read_bytes()).hexdigest(),
                    before,
                )
                self.assertEqual(outside.read_bytes(), b"outside-must-remain")

    def test_restore_rejects_overlapping_backup_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            self._backup(data_dir, backup_dir)
            with self.assertRaises(restore.RestoreError):
                restore.restore(backup_dir, backup_dir / "restored")

    def test_force_restore_rejects_unmanifested_stale_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_data = _make_dir(root / "source")
            self._seed_commerce(source_data)
            backup_dir = root / "backup"
            self._backup(source_data, backup_dir)

            target = _make_dir(root / "target")
            target_repo = SQLiteCommerceRepository(target / "commerce.sqlite3")
            target_repo.create_release("9.9.9", price_minor=1, currency="USD")
            target_repo.close()
            before = hashlib.sha256((target / "commerce.sqlite3").read_bytes()).hexdigest()
            (target / "stale.sqlite3").write_bytes(b"unmanifested")
            with self.assertRaisesRegex(restore.RestoreError, "not_available"):
                restore.restore(backup_dir, target, force=True)
            self.assertEqual(
                hashlib.sha256((target / "commerce.sqlite3").read_bytes()).hexdigest(),
                before,
            )


class MigrateCliTests(unittest.TestCase):
    def test_apply_and_verify_via_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = _make_dir(Path(temp) / "data")
            repo = SQLiteCommerceRepository(data_dir / "commerce.sqlite3")
            repo.close()
            self.assertEqual(migrate.main(["apply", "--data-dir", str(data_dir)]), 0)
            self.assertEqual(migrate.main(["verify", "--data-dir", str(data_dir)]), 0)

    def test_verify_fails_without_commerce(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = _make_dir(Path(temp) / "empty")
            self.assertEqual(
                migrate.main(["verify", "--data-dir", str(data_dir)]), 1
            )


class RotateTests(unittest.TestCase):
    def test_cli_requires_explicit_output_directory(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            rotate.main(["session-secret"])

    def test_session_secret_rotation_writes_private_file_without_value_in_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = rotate.rotate("session-secret", Path(temp))
            self.assertEqual(result.env_updates, {})
            path = result.files["session_secret_env"]
            self.assertTrue(path.is_file())
            if POSIX:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            secret = path.read_text(encoding="utf-8").split("=", 1)[1].strip('"\n')
            self.assertGreater(len(secret), 32)
            self.assertNotIn(secret, repr(result))
            self.assertTrue(result.steps)

    def test_session_secret_cli_never_prints_private_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = rotate.main(
                    ["session-secret", "--out-dir", str(Path(temp) / "rotate")]
                )
            self.assertEqual(status, 0)
            path = Path(temp) / "rotate" / rotate.SESSION_SECRET_FILENAME
            secret = path.read_text(encoding="utf-8").split("=", 1)[1].strip('"\n')
            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())

    def test_rotation_cli_does_not_echo_secret_from_exception(self) -> None:
        secret = "rotation-exception-secret-must-not-echo"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            rotate,
            "rotate",
            side_effect=RuntimeError(secret),
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = rotate.main(["session-secret", "--out-dir", "/tmp/unused"])
        self.assertEqual(status, 1)
        self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())

    def test_rotation_cli_never_prints_generic_env_update_values(self) -> None:
        secret = "future-env-secret-must-not-echo"
        synthetic = rotate.RotationResult(
            "session-secret",
            env_updates={"FUTURE_SECRET": secret},
            steps=["Apply through the private store."],
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            rotate,
            "rotate",
            return_value=synthetic,
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = rotate.main(
                ["session-secret", "--out-dir", "/tmp/unused-rotation-output"]
            )
        self.assertEqual(status, 0)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertIn("FUTURE_SECRET", output)
        self.assertNotIn(secret, output)

    def test_session_secret_rotation_is_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out_dir = Path(temp)
            first = rotate.rotate("session-secret", out_dir)
            path = first.files["session_secret_env"]
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                rotate.rotate("session-secret", out_dir)
            self.assertEqual(path.read_bytes(), before)

    def test_entitlement_key_rotation_generates_overlap_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = rotate.rotate(
                "entitlement-key", Path(temp), key_id="entitlement-key-002"
            )
            self.assertIn("entitlement-key-002", result.public_values)
            self.assertTrue(result.files["entitlement_key"].is_file())
            self.assertTrue(
                any("overlap" in step.lower() for step in result.steps)
            )

    def test_mfa_rotation_is_honestly_unavailable_without_writing_a_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out_dir = Path(temp) / "mfa-rotation"
            with self.assertRaisesRegex(OpsNotAvailableError, "not_available"):
                rotate.rotate("mfa-key", out_dir)
            self.assertFalse(out_dir.exists())

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = rotate.main(["mfa-key", "--out-dir", str(out_dir)])
            self.assertEqual(status, 1)
            self.assertIn("not_available", stdout.getvalue() + stderr.getvalue())
            self.assertFalse(out_dir.exists())

    def test_release_rotation_includes_overlap_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            release = rotate.rotate(
                "release-key", Path(temp), key_id="release-key-002"
            )
            self.assertIn("release-key-002", release.public_values)

    def test_unknown_key_type_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
            rotate.rotate("nope", Path(temp))

    def test_key_id_cannot_escape_private_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for unsafe in ("../outside", "nested/key", "nested\\key", "\x1bescape"):
                with self.subTest(key_id=unsafe), self.assertRaises(ValueError):
                    rotate.rotate("entitlement-key", Path(temp), key_id=unsafe)


if __name__ == "__main__":
    unittest.main()
