from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from core.secure_store import (
    STATUS_FAILED,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_SUCCESS,
    LinuxSecretToolStore,
    MacOSKeychainStore,
    SecureStoreResult,
    UnsupportedSecureStore,
    WindowsSecureStore,
    create_secure_store,
)


SERVICE = "com.jarvis.credentials"
ACCOUNT = "gemini-api-key"
SECRET = "super-secret-value"


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["mock-command"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class SecureStoreResultTests(unittest.TestCase):
    def test_secret_is_redacted_from_repr_str_and_message(self):
        result = SecureStoreResult(
            STATUS_SUCCESS,
            value=SECRET,
            message=f"retrieved {SECRET}",
        )

        self.assertEqual(result.value, SECRET)
        self.assertNotIn(SECRET, result.message)
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(SECRET, str(result))
        self.assertIn("<redacted>", repr(result))

    def test_ok_only_for_success(self):
        self.assertTrue(SecureStoreResult(STATUS_SUCCESS).ok)
        self.assertFalse(SecureStoreResult(STATUS_FAILED).ok)


class ValidationTests(unittest.TestCase):
    def test_invalid_service_and_account_never_execute_subprocess(self):
        store = MacOSKeychainStore()
        invalid_pairs = [
            ("", ACCOUNT),
            ("-option", ACCOUNT),
            ("bad;service", ACCOUNT),
            (SERVICE, "bad\naccount"),
            (SERVICE, " account"),
        ]

        with mock.patch("core.secure_store.subprocess.run") as run:
            for service, account in invalid_pairs:
                with self.subTest(service=service, account=account):
                    result = store.get(service, account)
                    self.assertEqual(result.status, STATUS_FAILED)
            run.assert_not_called()

    def test_invalid_secret_never_executes_subprocess_or_leaks(self):
        store = MacOSKeychainStore()
        with mock.patch("core.secure_store.subprocess.run") as run:
            for secret in ("", "bad\x00secret", "bad\nsecret"):
                with self.subTest(secret_length=len(secret)):
                    result = store.set(SERVICE, ACCOUNT, secret)
                    self.assertEqual(result.status, STATUS_FAILED)
                    self.assertNotIn(secret or "not-a-secret", result.message)
            run.assert_not_called()


class MacOSKeychainStoreTests(unittest.TestCase):
    def test_get_success_returns_value_but_redacts_repr(self):
        store = MacOSKeychainStore()
        with mock.patch(
            "core.secure_store.subprocess.run",
            return_value=_completed(0, stdout=f"{SECRET}\n"),
        ) as run:
            result = store.get(SERVICE, ACCOUNT)

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(result.value, SECRET)
        self.assertNotIn(SECRET, repr(result))
        argv = run.call_args.args[0]
        self.assertEqual(argv[0:2], ["/usr/bin/security", "find-generic-password"])
        self.assertIs(run.call_args.kwargs["shell"], False)
        self.assertIsInstance(argv, list)

    def test_get_exit_44_maps_to_not_found(self):
        with mock.patch(
            "core.secure_store.subprocess.run",
            return_value=_completed(44, stderr="item not found"),
        ):
            result = MacOSKeychainStore().get(SERVICE, ACCOUNT)

        self.assertEqual(result.status, STATUS_NOT_FOUND)
        self.assertIsNone(result.value)

    def test_get_other_nonzero_exit_maps_to_failed_without_stderr(self):
        with mock.patch(
            "core.secure_store.subprocess.run",
            return_value=_completed(2, stderr=f"failure: {SECRET}"),
        ):
            result = MacOSKeychainStore().get(SERVICE, ACCOUNT)

        self.assertEqual(result.status, STATUS_FAILED)
        self.assertNotIn(SECRET, result.message)
        self.assertNotIn(SECRET, repr(result))

    def test_set_uses_prompt_stdin_update_mode_argv_and_no_shell(self):
        with mock.patch(
            "core.secure_store.subprocess.run",
            return_value=_completed(0),
        ) as run:
            result = MacOSKeychainStore().set(SERVICE, ACCOUNT, SECRET)

        self.assertEqual(result.status, STATUS_SUCCESS)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0:2], ["/usr/bin/security", "add-generic-password"])
        self.assertIn("-U", argv)
        self.assertEqual(argv[-1], "-w")
        self.assertNotIn(SECRET, argv)
        self.assertEqual(run.call_args.kwargs["input"], f"{SECRET}\n")
        self.assertNotIn(SECRET, repr(run.call_args))
        self.assertIs(run.call_args.kwargs["shell"], False)
        self.assertIs(run.call_args.kwargs["check"], False)

    def test_delete_maps_success_and_not_found(self):
        with mock.patch(
            "core.secure_store.subprocess.run",
            side_effect=[_completed(0), _completed(44)],
        ) as run:
            deleted = MacOSKeychainStore().delete(SERVICE, ACCOUNT)
            missing = MacOSKeychainStore().delete(SERVICE, ACCOUNT)

        self.assertEqual(deleted.status, STATUS_SUCCESS)
        self.assertEqual(missing.status, STATUS_NOT_FOUND)
        for call in run.call_args_list:
            self.assertIs(call.kwargs["shell"], False)
            self.assertIsInstance(call.args[0], list)

    def test_missing_security_binary_maps_to_not_available(self):
        with mock.patch(
            "core.secure_store.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            result = MacOSKeychainStore().get(SERVICE, ACCOUNT)

        self.assertEqual(result.status, STATUS_NOT_AVAILABLE)

    def test_timeout_maps_to_failed_without_secret_leak(self):
        error = subprocess.TimeoutExpired(["/usr/bin/security", SECRET], timeout=15)
        with mock.patch("core.secure_store.subprocess.run", side_effect=error):
            result = MacOSKeychainStore().set(SERVICE, ACCOUNT, SECRET)

        self.assertEqual(result.status, STATUS_FAILED)
        self.assertNotIn(SECRET, result.message)
        self.assertNotIn(SECRET, repr(result))


class LinuxSecretToolStoreTests(unittest.TestCase):
    def test_absent_binary_returns_not_available_without_subprocess(self):
        with mock.patch("core.secure_store.shutil.which", return_value=None) as which:
            store = LinuxSecretToolStore()
        with mock.patch("core.secure_store.subprocess.run") as run:
            result = store.get(SERVICE, ACCOUNT)

        self.assertEqual(result.status, STATUS_NOT_AVAILABLE)
        which.assert_called_once_with("secret-tool")
        run.assert_not_called()

    def test_relative_binary_path_is_rejected(self):
        store = LinuxSecretToolStore("relative/secret-tool")
        with mock.patch("core.secure_store.subprocess.run") as run:
            result = store.get(SERVICE, ACCOUNT)

        self.assertEqual(result.status, STATUS_NOT_AVAILABLE)
        run.assert_not_called()

    def test_set_passes_secret_via_stdin_not_argv_and_disables_shell(self):
        with mock.patch("core.secure_store.shutil.which", return_value="/usr/bin/secret-tool"):
            store = LinuxSecretToolStore()
        with mock.patch(
            "core.secure_store.subprocess.run",
            return_value=_completed(0),
        ) as run:
            result = store.set(SERVICE, ACCOUNT, SECRET)

        self.assertEqual(result.status, STATUS_SUCCESS)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0:2], ["/usr/bin/secret-tool", "store"])
        self.assertNotIn(SECRET, argv)
        self.assertEqual(run.call_args.kwargs["input"], SECRET)
        self.assertNotIn(SECRET, repr(run.call_args))
        self.assertIs(run.call_args.kwargs["shell"], False)

    def test_get_success_and_exit_one_not_found(self):
        store = LinuxSecretToolStore("/usr/bin/secret-tool")
        with mock.patch(
            "core.secure_store.subprocess.run",
            side_effect=[_completed(0, stdout=SECRET), _completed(1)],
        ):
            found = store.get(SERVICE, ACCOUNT)
            missing = store.get(SERVICE, ACCOUNT)

        self.assertEqual(found.status, STATUS_SUCCESS)
        self.assertEqual(found.value, SECRET)
        self.assertEqual(missing.status, STATUS_NOT_FOUND)

    def test_exit_one_with_stderr_is_failed_not_missing(self):
        store = LinuxSecretToolStore("/usr/bin/secret-tool")
        with mock.patch(
            "core.secure_store.subprocess.run",
            side_effect=[
                _completed(1, stderr="secret service unavailable"),
                _completed(1, stderr="secret service unavailable"),
            ],
        ):
            lookup = store.get(SERVICE, ACCOUNT)
            delete = store.delete(SERVICE, ACCOUNT)

        self.assertEqual(lookup.status, STATUS_FAILED)
        self.assertEqual(delete.status, STATUS_FAILED)
        self.assertNotIn("secret service unavailable", lookup.message)
        self.assertNotIn("secret service unavailable", delete.message)

    def test_delete_exit_mapping(self):
        store = LinuxSecretToolStore("/usr/bin/secret-tool")
        with mock.patch(
            "core.secure_store.subprocess.run",
            side_effect=[_completed(0), _completed(1), _completed(2)],
        ):
            self.assertEqual(store.delete(SERVICE, ACCOUNT).status, STATUS_SUCCESS)
            self.assertEqual(store.delete(SERVICE, ACCOUNT).status, STATUS_NOT_FOUND)
            self.assertEqual(store.delete(SERVICE, ACCOUNT).status, STATUS_FAILED)

    def test_binary_disappearing_maps_to_not_available(self):
        store = LinuxSecretToolStore("/usr/bin/secret-tool")
        with mock.patch(
            "core.secure_store.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            result = store.delete(SERVICE, ACCOUNT)

        self.assertEqual(result.status, STATUS_NOT_AVAILABLE)


class UnavailableStoreTests(unittest.TestCase):
    def test_windows_never_fakes_success(self):
        store = WindowsSecureStore()
        with mock.patch("core.secure_store.subprocess.run") as run:
            results = [
                store.get(SERVICE, ACCOUNT),
                store.set(SERVICE, ACCOUNT, SECRET),
                store.delete(SERVICE, ACCOUNT),
            ]

        self.assertTrue(all(result.status == STATUS_NOT_AVAILABLE for result in results))
        run.assert_not_called()

    def test_unsupported_os_never_fakes_success(self):
        result = UnsupportedSecureStore().get(SERVICE, ACCOUNT)
        self.assertEqual(result.status, STATUS_NOT_AVAILABLE)


class FactoryTests(unittest.TestCase):
    def test_explicit_os_routing(self):
        with mock.patch("core.secure_store.shutil.which", return_value=None):
            self.assertIsInstance(create_secure_store("Darwin"), MacOSKeychainStore)
            self.assertIsInstance(create_secure_store("macOS"), MacOSKeychainStore)
            self.assertIsInstance(create_secure_store("Linux"), LinuxSecretToolStore)
            self.assertIsInstance(create_secure_store("Windows"), WindowsSecureStore)
            self.assertIsInstance(create_secure_store("FreeBSD"), UnsupportedSecureStore)

    def test_default_routing_uses_platform_system(self):
        with mock.patch("core.secure_store.platform.system", return_value="Windows") as system:
            store = create_secure_store()

        self.assertIsInstance(store, WindowsSecureStore)
        system.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
