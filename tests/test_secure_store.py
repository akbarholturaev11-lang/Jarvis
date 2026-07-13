from __future__ import annotations

import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
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
    class Native:
        def __init__(self) -> None:
            self.values: dict[tuple[str, str], bytes] = {}
            self.failure: int | None = None

        def get(self, service: str, account: str):
            if self.failure is not None:
                return self.failure, None
            value = self.values.get((service, account))
            return (0, value) if value is not None else (-25300, None)

        def set(self, service: str, account: str, secret: bytes):
            if self.failure is not None:
                return self.failure
            self.values[(service, account)] = secret
            return 0

        def delete(self, service: str, account: str):
            if self.failure is not None:
                return self.failure
            if self.values.pop((service, account), None) is None:
                return -25300
            return 0

    def test_get_success_returns_value_but_redacts_repr(self):
        native = self.Native()
        native.values[(SERVICE, ACCOUNT)] = SECRET.encode("utf-8")
        result = MacOSKeychainStore(native).get(SERVICE, ACCOUNT)

        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertEqual(result.value, SECRET)
        self.assertNotIn(SECRET, repr(result))

    def test_native_item_not_found_maps_to_not_found(self):
        result = MacOSKeychainStore(self.Native()).get(SERVICE, ACCOUNT)

        self.assertEqual(result.status, STATUS_NOT_FOUND)
        self.assertIsNone(result.value)

    def test_native_failure_maps_to_fixed_failed_result(self):
        native = self.Native()
        native.failure = -25308
        result = MacOSKeychainStore(native).get(SERVICE, ACCOUNT)

        self.assertEqual(result.status, STATUS_FAILED)
        self.assertNotIn(SECRET, result.message)
        self.assertNotIn(SECRET, repr(result))

    def test_crud_update_and_delete_use_native_api_without_subprocess(self):
        native = self.Native()
        store = MacOSKeychainStore(native)
        with mock.patch("core.secure_store.subprocess.run") as run:
            created = store.set(SERVICE, ACCOUNT, SECRET)
            first = store.get(SERVICE, ACCOUNT)
            updated = store.set(SERVICE, ACCOUNT, "updated-secret")
            second = store.get(SERVICE, ACCOUNT)
            deleted = store.delete(SERVICE, ACCOUNT)
            missing = store.delete(SERVICE, ACCOUNT)

        self.assertEqual(created.status, STATUS_SUCCESS)
        self.assertEqual(first.value, SECRET)
        self.assertEqual(updated.status, STATUS_SUCCESS)
        self.assertEqual(second.value, "updated-secret")
        self.assertEqual(deleted.status, STATUS_SUCCESS)
        self.assertEqual(missing.status, STATUS_NOT_FOUND)
        run.assert_not_called()

    def test_missing_native_framework_maps_to_not_available(self):
        with mock.patch(
            "core.secure_store._MacOSNativeKeychain", side_effect=OSError
        ):
            result = MacOSKeychainStore().get(SERVICE, ACCOUNT)

        self.assertEqual(result.status, STATUS_NOT_AVAILABLE)

    def test_native_exception_maps_to_failed_without_secret_leak(self):
        native = mock.Mock()
        native.set.side_effect = RuntimeError(f"backend leaked {SECRET}")
        result = MacOSKeychainStore(native).set(SERVICE, ACCOUNT, SECRET)

        self.assertEqual(result.status, STATUS_FAILED)
        self.assertNotIn(SECRET, result.message)
        self.assertNotIn(SECRET, repr(result))

    def test_lazy_native_initialization_is_single_and_thread_safe(self):
        native = self.Native()
        started = threading.Event()
        release = threading.Event()

        def create_native():
            started.set()
            self.assertTrue(release.wait(2))
            return native

        store = MacOSKeychainStore()
        with mock.patch(
            "core.secure_store._MacOSNativeKeychain", side_effect=create_native
        ) as factory:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(store.get, SERVICE, ACCOUNT)
                self.assertTrue(started.wait(2))
                second = pool.submit(store.get, SERVICE, ACCOUNT)
                release.set()
                results = [first.result(timeout=2), second.result(timeout=2)]

        factory.assert_called_once_with()
        self.assertTrue(all(result.status == STATUS_NOT_FOUND for result in results))


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


class WindowsSecureStoreTests(unittest.TestCase):
    class Native:
        def __init__(self) -> None:
            self.values: dict[tuple[str, str], bytes] = {}
            self.failure: int | None = None

        def get(self, service: str, account: str):
            if self.failure is not None:
                return self.failure, None
            value = self.values.get((service, account))
            return (0, value) if value is not None else (1168, None)

        def set(self, service: str, account: str, secret: bytes):
            if self.failure is not None:
                return self.failure
            self.values[(service, account)] = secret
            return 0

        def delete(self, service: str, account: str):
            if self.failure is not None:
                return self.failure
            if self.values.pop((service, account), None) is None:
                return 1168
            return 0

    def test_windows_crud_update_delete_and_restart_persistence(self):
        native = self.Native()
        first_process = WindowsSecureStore(native)
        with mock.patch("core.secure_store.subprocess.run") as run:
            self.assertEqual(
                first_process.set(SERVICE, ACCOUNT, SECRET).status, STATUS_SUCCESS
            )
            restarted = WindowsSecureStore(native)
            self.assertEqual(restarted.get(SERVICE, ACCOUNT).value, SECRET)
            self.assertEqual(
                restarted.set(SERVICE, ACCOUNT, "updated-secret").status,
                STATUS_SUCCESS,
            )
            self.assertEqual(restarted.get(SERVICE, ACCOUNT).value, "updated-secret")
            self.assertEqual(restarted.delete(SERVICE, ACCOUNT).status, STATUS_SUCCESS)
            self.assertEqual(restarted.get(SERVICE, ACCOUNT).status, STATUS_NOT_FOUND)
        run.assert_not_called()

    def test_windows_failure_and_unavailable_are_honest(self):
        native = self.Native()
        native.failure = 5
        failed = WindowsSecureStore(native).set(SERVICE, ACCOUNT, SECRET)
        self.assertEqual(failed.status, STATUS_FAILED)
        self.assertNotIn(SECRET, repr(failed))
        with mock.patch(
            "core.secure_store._WindowsCredentialManager", side_effect=OSError
        ):
            unavailable = WindowsSecureStore().get(SERVICE, ACCOUNT)
        self.assertEqual(unavailable.status, STATUS_NOT_AVAILABLE)

    def test_windows_lazy_initialization_is_single_and_thread_safe(self):
        native = self.Native()
        started = threading.Event()
        release = threading.Event()

        def create_native():
            started.set()
            self.assertTrue(release.wait(2))
            return native

        store = WindowsSecureStore()
        with mock.patch(
            "core.secure_store._WindowsCredentialManager", side_effect=create_native
        ) as factory:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(store.get, SERVICE, ACCOUNT)
                self.assertTrue(started.wait(2))
                second = pool.submit(store.get, SERVICE, ACCOUNT)
                release.set()
                results = [first.result(timeout=2), second.result(timeout=2)]

        factory.assert_called_once_with()
        self.assertTrue(all(result.status == STATUS_NOT_FOUND for result in results))


class UnavailableStoreTests(unittest.TestCase):

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
