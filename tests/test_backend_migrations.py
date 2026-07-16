from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from product_backend import migrations
from product_backend.migrations import MigrationError
from product_backend.sqlite_repository import SQLiteCommerceRepository


class MigrationTests(unittest.TestCase):
    def _commerce_path(self, root: Path) -> Path:
        return root / migrations.COMMERCE_DATABASE

    def test_fresh_commerce_database_reports_expected_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._commerce_path(root)
            repo = SQLiteCommerceRepository(path)
            repo.create_release("1.0.0", price_minor=1000, currency="USD")
            repo.close()
            status = migrations.inspect_database(path)
            self.assertTrue(status.exists)
            self.assertEqual(
                status.user_version, migrations.EXPECTED_COMMERCE_SCHEMA_VERSION
            )
            self.assertTrue(status.integrity_ok)
            migrations.verify(root)

    def test_forward_migration_from_older_version_preserves_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._commerce_path(root)
            repo = SQLiteCommerceRepository(path)
            repo.create_release("2.1.0", price_minor=2500, currency="EUR")
            repo.close()

            # Simulate a database stamped at an older schema version.
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 0")
            connection.commit()
            connection.close()
            self.assertEqual(migrations.inspect_database(path).user_version, 0)

            before, after = migrations.migrate_commerce_database(path)
            self.assertEqual(before, 0)
            self.assertEqual(after, migrations.EXPECTED_COMMERCE_SCHEMA_VERSION)

            # The pre-existing row survives the forward migration.
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            count = connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
            connection.close()
            self.assertEqual(count, 1)
            migrations.verify(root)

    def test_verify_fails_closed_when_commerce_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(MigrationError):
                migrations.verify(Path(temp))

    def test_verify_rejects_future_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._commerce_path(root)
            repo = SQLiteCommerceRepository(path)
            repo.close()
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 99")
            connection.commit()
            connection.close()
            with self.assertRaises(MigrationError):
                migrations.verify(root)

    def test_migrate_rejects_future_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._commerce_path(root)
            repo = SQLiteCommerceRepository(path)
            repo.close()
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 99")
            connection.commit()
            connection.close()
            with self.assertRaises(MigrationError):
                migrations.migrate_commerce_database(path)

    def test_report_lists_all_known_databases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            statuses = migrations.report(Path(temp))
            names = {status.filename for status in statuses}
            self.assertEqual(names, set(migrations.KNOWN_DATABASES))
            self.assertTrue(all(not status.exists for status in statuses))


if __name__ == "__main__":
    unittest.main()
