from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from ops import backup as backup_mod
from ops import gen_secrets, migrate, restore, rotate, validate_config
from ops._common import POSIX
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
        repo = SQLiteCommerceRepository(data_dir / "commerce.sqlite3")
        repo.create_release("1.2.3", price_minor=4999, currency="USD")
        repo.close()

    def _release_count(self, data_dir: Path) -> int:
        import sqlite3

        path = data_dir / "commerce.sqlite3"
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
        finally:
            connection.close()

    def test_backup_and_restore_round_trip_preserves_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            # Seed a payment-evidence object too.
            evidence = _make_dir(data_dir / "payment-evidence")
            (evidence / "obj-001.bin").write_bytes(b"evidence-bytes")

            backup_dir = root / "backup"
            manifest = backup_mod.backup(data_dir, backup_dir)
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

    def test_restore_detects_tampered_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            backup_mod.backup(data_dir, backup_dir)
            # Corrupt a backed-up database.
            with (backup_dir / "commerce.sqlite3").open("r+b") as handle:
                handle.seek(0)
                handle.write(b"XXXX")
            with self.assertRaises(restore.RestoreError):
                restore.verify_backup(backup_dir)

    def test_restore_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = _make_dir(root / "data")
            self._seed_commerce(data_dir)
            backup_dir = root / "backup"
            backup_mod.backup(data_dir, backup_dir)
            with self.assertRaises(restore.RestoreError):
                restore.restore(backup_dir, data_dir, force=False)
            # With force it succeeds.
            self.assertGreaterEqual(
                restore.restore(backup_dir, data_dir, force=True), 1
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
    def test_session_secret_rotation_returns_new_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = rotate.rotate("session-secret", Path(temp))
            self.assertIn("JARVIS_ADMIN_SESSION_SECRET_B64URL", result.env_updates)
            self.assertTrue(result.steps)

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

    def test_mfa_and_release_rotations_include_side_effect_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            mfa = rotate.rotate("mfa-key", Path(temp))
            self.assertTrue(any("re-enrol" in note.lower() for note in mfa.notes))
            release = rotate.rotate(
                "release-key", Path(temp), key_id="release-key-002"
            )
            self.assertIn("release-key-002", release.public_values)

    def test_unknown_key_type_raises(self) -> None:
        with self.assertRaises(ValueError):
            rotate.rotate("nope", Path("."))


if __name__ == "__main__":
    unittest.main()
