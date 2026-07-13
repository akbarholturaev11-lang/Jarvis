"""Platform-neutral secure secret storage.

The public contract is deliberately small: ``get``, ``set`` and ``delete``
always return a :class:`SecureStoreResult` with one of four honest statuses.
macOS and Windows use their native credential APIs.  The Linux Secret Service
CLI receives secret bytes only over stdin with ``shell=False``.  Backend output
and exception details are never copied into result messages.

This module does not migrate or read any existing configuration file.  It is an
isolated foundation that callers can adopt explicitly in a later change.
Its fixed messages are internal diagnostics, not visible UI copy; UI callers
must map statuses to the shared English/Russian localization dictionary.
"""

from __future__ import annotations

import ctypes
import platform
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final


STATUS_SUCCESS: Final = "success"
STATUS_NOT_AVAILABLE: Final = "not_available"
STATUS_FAILED: Final = "failed"
STATUS_NOT_FOUND: Final = "not_found"

_VALID_STATUSES: Final = frozenset(
    {
        STATUS_SUCCESS,
        STATUS_NOT_AVAILABLE,
        STATUS_FAILED,
        STATUS_NOT_FOUND,
    }
)
_IDENTIFIER_MAX_LENGTH: Final = 255
_SECRET_MAX_BYTES: Final = 64 * 1024
_COMMAND_TIMEOUT_SECONDS: Final = 15
_IDENTIFIER_PUNCTUATION: Final = frozenset("._:@+/- ")
_SECRET_TOOL_NOT_FOUND_EXIT_CODES: Final = frozenset({1})
_MACOS_ERR_SUCCESS: Final = 0
_MACOS_ERR_ITEM_NOT_FOUND: Final = -25300
_MACOS_ERR_DUPLICATE_ITEM: Final = -25299
_WINDOWS_ERROR_NOT_FOUND: Final = 1168
_WINDOWS_CRED_TYPE_GENERIC: Final = 1
_WINDOWS_CRED_PERSIST_LOCAL_MACHINE: Final = 2
_CF_STRING_ENCODING_UTF8: Final = 0x08000100


class _WindowsFileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _WindowsCredential(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _WindowsFileTime),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class _SecretInput(str):
    """A string that remains usable by subprocess but redacts container reprs."""

    def __repr__(self) -> str:
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class SecureStoreResult:
    """Result returned by every secure-store operation.

    ``value`` is intentionally excluded from ``repr``.  Messages produced by
    this module are fixed, sanitized text and never include command output,
    exception details, identifiers or secret values.
    """

    status: str
    value: str | None = field(default=None, repr=False)
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError("Invalid secure-store status")
        if self.value and self.value in self.message:
            object.__setattr__(self, "message", self.message.replace(self.value, "<redacted>"))

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    def __repr__(self) -> str:
        value_repr = "<redacted>" if self.value is not None else "None"
        return (
            f"{type(self).__name__}(status={self.status!r}, "
            f"value={value_repr}, message={self.message!r})"
        )


def _validation_error(service: object, account: object) -> SecureStoreResult | None:
    if not _valid_identifier(service):
        return SecureStoreResult(STATUS_FAILED, message="Invalid service identifier.")
    if not _valid_identifier(account):
        return SecureStoreResult(STATUS_FAILED, message="Invalid account identifier.")
    return None


def _valid_identifier(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if not value or len(value) > _IDENTIFIER_MAX_LENGTH:
        return False
    if value != value.strip() or not value[0].isalnum():
        return False
    return all(character.isalnum() or character in _IDENTIFIER_PUNCTUATION for character in value)


def _valid_secret(secret: object) -> bool:
    if not isinstance(secret, str) or not secret or any(char in secret for char in "\x00\r\n"):
        return False
    try:
        return len(secret.encode("utf-8")) <= _SECRET_MAX_BYTES
    except UnicodeEncodeError:
        return False


def _run_command(
    argv: list[str],
    *,
    input_text: str | None = None,
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "shell": False,
        "check": False,
        "timeout": _COMMAND_TIMEOUT_SECONDS,
    }
    if input_text is not None:
        kwargs["input"] = _SecretInput(input_text)
    try:
        completed = subprocess.run(argv, **kwargs)
    except FileNotFoundError:
        return None, STATUS_NOT_AVAILABLE
    except (OSError, subprocess.SubprocessError):
        return None, STATUS_FAILED
    return completed, None


def _run_failure(status: str | None) -> SecureStoreResult:
    if status == STATUS_NOT_AVAILABLE:
        return SecureStoreResult(
            STATUS_NOT_AVAILABLE,
            message="Secure storage backend is not available.",
        )
    return SecureStoreResult(STATUS_FAILED, message="Secure storage operation failed.")


def _stdout_text(completed: subprocess.CompletedProcess[str]) -> str:
    raw = completed.stdout
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        return raw
    return ""


def _has_stderr(completed: subprocess.CompletedProcess[str]) -> bool:
    """Tell an operational error from an empty lookup without exposing details."""

    raw = completed.stderr
    if isinstance(raw, bytes):
        return bool(raw.strip())
    if isinstance(raw, str):
        return bool(raw.strip())
    return False


class SecureStore(ABC):
    """Platform-neutral secure-storage contract."""

    def get(self, service: str, account: str) -> SecureStoreResult:
        validation_error = _validation_error(service, account)
        if validation_error is not None:
            return validation_error
        return self._get(service, account)

    def set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        validation_error = _validation_error(service, account)
        if validation_error is not None:
            return validation_error
        if not _valid_secret(secret):
            return SecureStoreResult(STATUS_FAILED, message="Invalid secret value.")
        return self._set(service, account, secret)

    def delete(self, service: str, account: str) -> SecureStoreResult:
        validation_error = _validation_error(service, account)
        if validation_error is not None:
            return validation_error
        return self._delete(service, account)

    @abstractmethod
    def _get(self, service: str, account: str) -> SecureStoreResult:
        raise NotImplementedError

    @abstractmethod
    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        raise NotImplementedError

    @abstractmethod
    def _delete(self, service: str, account: str) -> SecureStoreResult:
        raise NotImplementedError


class _MacOSNativeKeychain:
    """Small ctypes bridge to Security.framework with no CLI/TTY boundary."""

    _CORE_FOUNDATION = (
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    _SECURITY = "/System/Library/Frameworks/Security.framework/Security"

    def __init__(self) -> None:
        self._cf = ctypes.CDLL(self._CORE_FOUNDATION)
        self._security = ctypes.CDLL(self._SECURITY)
        self._configure_functions()
        self._key_callbacks = ctypes.c_void_p(
            ctypes.addressof(
                ctypes.c_byte.in_dll(self._cf, "kCFTypeDictionaryKeyCallBacks")
            )
        )
        self._value_callbacks = ctypes.c_void_p(
            ctypes.addressof(
                ctypes.c_byte.in_dll(self._cf, "kCFTypeDictionaryValueCallBacks")
            )
        )
        self._true = self._symbol(self._cf, "kCFBooleanTrue")
        self._constants = {
            name: self._symbol(self._security, name)
            for name in (
                "kSecClass",
                "kSecClassGenericPassword",
                "kSecAttrService",
                "kSecAttrAccount",
                "kSecValueData",
                "kSecReturnData",
                "kSecMatchLimit",
                "kSecMatchLimitOne",
                "kSecUseAuthenticationUI",
                "kSecUseAuthenticationUIFail",
            )
        }

    @staticmethod
    def _symbol(library: ctypes.CDLL, name: str) -> int:
        value = ctypes.c_void_p.in_dll(library, name).value
        if not value:
            raise OSError("Required secure-store symbol is unavailable.")
        return value

    def _configure_functions(self) -> None:
        self._cf.CFStringCreateWithBytes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
            ctypes.c_bool,
        ]
        self._cf.CFStringCreateWithBytes.restype = ctypes.c_void_p
        self._cf.CFDataCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_long,
        ]
        self._cf.CFDataCreate.restype = ctypes.c_void_p
        self._cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        self._cf.CFDataGetLength.restype = ctypes.c_long
        self._cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        self._cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
        self._cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._cf.CFDictionaryCreate.restype = ctypes.c_void_p
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None
        self._security.SecItemCopyMatching.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecItemCopyMatching.restype = ctypes.c_int32
        self._security.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._security.SecItemUpdate.restype = ctypes.c_int32
        self._security.SecItemAdd.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecItemAdd.restype = ctypes.c_int32
        self._security.SecItemDelete.argtypes = [ctypes.c_void_p]
        self._security.SecItemDelete.restype = ctypes.c_int32

    def _string(self, value: str) -> int:
        encoded = value.encode("utf-8")
        result = self._cf.CFStringCreateWithBytes(
            None,
            encoded,
            len(encoded),
            _CF_STRING_ENCODING_UTF8,
            False,
        )
        if not result:
            raise OSError("Secure-store string allocation failed.")
        return result

    def _data(self, value: bytes) -> int:
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        try:
            result = self._cf.CFDataCreate(None, buffer, len(value))
        finally:
            ctypes.memset(buffer, 0, len(value))
        if not result:
            raise OSError("Secure-store data allocation failed.")
        return result

    def _dictionary(self, pairs: list[tuple[int, int]]) -> int:
        keys = (ctypes.c_void_p * len(pairs))(*(pair[0] for pair in pairs))
        values = (ctypes.c_void_p * len(pairs))(*(pair[1] for pair in pairs))
        result = self._cf.CFDictionaryCreate(
            None,
            keys,
            values,
            len(pairs),
            self._key_callbacks,
            self._value_callbacks,
        )
        if not result:
            raise OSError("Secure-store query allocation failed.")
        return result

    def _query(self, service_ref: int, account_ref: int) -> int:
        c = self._constants
        return self._dictionary(
            [
                (c["kSecClass"], c["kSecClassGenericPassword"]),
                (c["kSecAttrService"], service_ref),
                (c["kSecAttrAccount"], account_ref),
                (
                    c["kSecUseAuthenticationUI"],
                    c["kSecUseAuthenticationUIFail"],
                ),
            ]
        )

    def get(self, service: str, account: str) -> tuple[int, bytes | None]:
        service_ref = self._string(service)
        account_ref = self._string(account)
        query = 0
        try:
            c = self._constants
            query = self._dictionary(
                [
                    (c["kSecClass"], c["kSecClassGenericPassword"]),
                    (c["kSecAttrService"], service_ref),
                    (c["kSecAttrAccount"], account_ref),
                    (c["kSecReturnData"], self._true),
                    (c["kSecMatchLimit"], c["kSecMatchLimitOne"]),
                    (
                        c["kSecUseAuthenticationUI"],
                        c["kSecUseAuthenticationUIFail"],
                    ),
                ]
            )
        finally:
            self._cf.CFRelease(service_ref)
            self._cf.CFRelease(account_ref)
        result = ctypes.c_void_p()
        try:
            status = int(
                self._security.SecItemCopyMatching(query, ctypes.byref(result))
            )
        finally:
            self._cf.CFRelease(query)
        if status != _MACOS_ERR_SUCCESS or not result.value:
            if result.value:
                self._cf.CFRelease(result)
            return status, None
        try:
            length = int(self._cf.CFDataGetLength(result))
            if length <= 0 or length > _SECRET_MAX_BYTES:
                return -1, None
            pointer = self._cf.CFDataGetBytePtr(result)
            if not pointer:
                return -1, None
            return status, ctypes.string_at(pointer, length)
        finally:
            self._cf.CFRelease(result)

    def set(self, service: str, account: str, secret: bytes) -> int:
        service_ref = self._string(service)
        account_ref = self._string(account)
        secret_ref = self._data(secret)
        query = attributes = addition = 0
        try:
            c = self._constants
            query = self._query(service_ref, account_ref)
            attributes = self._dictionary([(c["kSecValueData"], secret_ref)])
            addition = self._dictionary(
                [
                    (c["kSecClass"], c["kSecClassGenericPassword"]),
                    (c["kSecAttrService"], service_ref),
                    (c["kSecAttrAccount"], account_ref),
                    (c["kSecValueData"], secret_ref),
                    (
                        c["kSecUseAuthenticationUI"],
                        c["kSecUseAuthenticationUIFail"],
                    ),
                ]
            )
        finally:
            self._cf.CFRelease(service_ref)
            self._cf.CFRelease(account_ref)
            self._cf.CFRelease(secret_ref)
        try:
            status = int(self._security.SecItemUpdate(query, attributes))
            if status == _MACOS_ERR_ITEM_NOT_FOUND:
                status = int(self._security.SecItemAdd(addition, None))
                if status == _MACOS_ERR_DUPLICATE_ITEM:
                    status = int(self._security.SecItemUpdate(query, attributes))
            return status
        finally:
            self._cf.CFRelease(query)
            self._cf.CFRelease(attributes)
            self._cf.CFRelease(addition)

    def delete(self, service: str, account: str) -> int:
        service_ref = self._string(service)
        account_ref = self._string(account)
        try:
            query = self._query(service_ref, account_ref)
        finally:
            self._cf.CFRelease(service_ref)
            self._cf.CFRelease(account_ref)
        try:
            return int(self._security.SecItemDelete(query))
        finally:
            self._cf.CFRelease(query)


class MacOSKeychainStore(SecureStore):
    """Secure store backed directly by macOS Security.framework."""

    def __init__(self, native: object | None = None) -> None:
        self._native = native
        self._native_load_attempted = native is not None
        self._native_load_lock = threading.Lock()

    def _backend(self) -> object | None:
        if not self._native_load_attempted:
            with self._native_load_lock:
                if not self._native_load_attempted:
                    try:
                        self._native = _MacOSNativeKeychain()
                    except Exception:
                        self._native = None
                    self._native_load_attempted = True
        return self._native

    def _get(self, service: str, account: str) -> SecureStoreResult:
        backend = self._backend()
        if backend is None:
            return _run_failure(STATUS_NOT_AVAILABLE)
        try:
            status, raw = backend.get(service, account)
        except Exception:
            return _run_failure(STATUS_FAILED)
        if status == _MACOS_ERR_ITEM_NOT_FOUND:
            return SecureStoreResult(STATUS_NOT_FOUND, message="Secret was not found.")
        if status != _MACOS_ERR_SUCCESS or raw is None:
            return _run_failure(STATUS_FAILED)
        try:
            secret = raw.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return _run_failure(STATUS_FAILED)
        if not _valid_secret(secret):
            return _run_failure(STATUS_FAILED)
        return SecureStoreResult(STATUS_SUCCESS, value=secret, message="Secret retrieved.")

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        backend = self._backend()
        if backend is None:
            return _run_failure(STATUS_NOT_AVAILABLE)
        try:
            status = backend.set(service, account, secret.encode("utf-8"))
        except Exception:
            return _run_failure(STATUS_FAILED)
        if status != _MACOS_ERR_SUCCESS:
            return _run_failure(STATUS_FAILED)
        return SecureStoreResult(STATUS_SUCCESS, message="Secret stored.")

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        backend = self._backend()
        if backend is None:
            return _run_failure(STATUS_NOT_AVAILABLE)
        try:
            status = backend.delete(service, account)
        except Exception:
            return _run_failure(STATUS_FAILED)
        if status == _MACOS_ERR_ITEM_NOT_FOUND:
            return SecureStoreResult(STATUS_NOT_FOUND, message="Secret was not found.")
        if status != _MACOS_ERR_SUCCESS:
            return _run_failure(STATUS_FAILED)
        return SecureStoreResult(STATUS_SUCCESS, message="Secret deleted.")


class LinuxSecretToolStore(SecureStore):
    """Secure store backed by ``secret-tool`` when that binary is available."""

    def __init__(self, binary_path: str | None = None) -> None:
        candidate = binary_path if binary_path is not None else shutil.which("secret-tool")
        self._binary_path = (
            candidate
            if isinstance(candidate, str) and Path(candidate).is_absolute()
            else None
        )

    def _unavailable(self) -> SecureStoreResult:
        return SecureStoreResult(
            STATUS_NOT_AVAILABLE,
            message="Linux secret-tool is not available.",
        )

    def _get(self, service: str, account: str) -> SecureStoreResult:
        if not self._binary_path:
            return self._unavailable()
        completed, run_status = _run_command(
            [
                self._binary_path,
                "lookup",
                "service",
                service,
                "account",
                account,
            ]
        )
        if completed is None:
            return _run_failure(run_status)
        if (
            completed.returncode in _SECRET_TOOL_NOT_FOUND_EXIT_CODES
            and not _has_stderr(completed)
        ):
            return SecureStoreResult(STATUS_NOT_FOUND, message="Secret was not found.")
        if completed.returncode != 0:
            return _run_failure(STATUS_FAILED)
        # secret-tool writes the exact stored bytes when stdout is a pipe; it
        # adds a newline only for an interactive TTY.  subprocess always gives
        # it a pipe here, so stripping would corrupt a valid trailing byte.
        secret = _stdout_text(completed)
        if not secret:
            return SecureStoreResult(STATUS_FAILED, message="Secure storage returned no secret.")
        return SecureStoreResult(STATUS_SUCCESS, value=secret, message="Secret retrieved.")

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        if not self._binary_path:
            return self._unavailable()
        completed, run_status = _run_command(
            [
                self._binary_path,
                "store",
                "--label=Jarvis secure storage",
                "service",
                service,
                "account",
                account,
            ],
            # secret-tool reads stdin through EOF and stores every byte.  Do not
            # append the newline required by the separate macOS prompt path.
            input_text=secret,
        )
        if completed is None:
            return _run_failure(run_status)
        if completed.returncode != 0:
            return _run_failure(STATUS_FAILED)
        return SecureStoreResult(STATUS_SUCCESS, message="Secret stored.")

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        if not self._binary_path:
            return self._unavailable()
        completed, run_status = _run_command(
            [
                self._binary_path,
                "clear",
                "service",
                service,
                "account",
                account,
            ]
        )
        if completed is None:
            return _run_failure(run_status)
        if (
            completed.returncode in _SECRET_TOOL_NOT_FOUND_EXIT_CODES
            and not _has_stderr(completed)
        ):
            return SecureStoreResult(STATUS_NOT_FOUND, message="Secret was not found.")
        if completed.returncode != 0:
            return _run_failure(STATUS_FAILED)
        return SecureStoreResult(STATUS_SUCCESS, message="Secret deleted.")


class _WindowsCredentialManager:
    """Small ctypes bridge to the native Windows Credential Manager."""

    def __init__(self) -> None:
        self._advapi = ctypes.WinDLL("Advapi32", use_last_error=True)
        credential_pointer = ctypes.POINTER(_WindowsCredential)
        self._advapi.CredWriteW.argtypes = [credential_pointer, ctypes.c_uint32]
        self._advapi.CredWriteW.restype = ctypes.c_int
        self._advapi.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(credential_pointer),
        ]
        self._advapi.CredReadW.restype = ctypes.c_int
        self._advapi.CredDeleteW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._advapi.CredDeleteW.restype = ctypes.c_int
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None

    @staticmethod
    def _target(service: str, account: str) -> str:
        return f"{service}:{account}"

    def get(self, service: str, account: str) -> tuple[int, bytes | None]:
        pointer = ctypes.POINTER(_WindowsCredential)()
        ok = self._advapi.CredReadW(
            self._target(service, account),
            _WINDOWS_CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        )
        if not ok:
            return int(ctypes.get_last_error()), None
        try:
            credential = pointer.contents
            size = int(credential.CredentialBlobSize)
            if size <= 0 or size > _SECRET_MAX_BYTES:
                return -1, None
            if not credential.CredentialBlob:
                return -1, None
            return 0, ctypes.string_at(credential.CredentialBlob, size)
        finally:
            self._advapi.CredFree(pointer)

    def set(self, service: str, account: str, secret: bytes) -> int:
        buffer = (ctypes.c_ubyte * len(secret)).from_buffer_copy(secret)
        credential = _WindowsCredential()
        credential.Type = _WINDOWS_CRED_TYPE_GENERIC
        credential.TargetName = self._target(service, account)
        credential.CredentialBlobSize = len(secret)
        credential.CredentialBlob = ctypes.cast(
            buffer, ctypes.POINTER(ctypes.c_ubyte)
        )
        credential.Persist = _WINDOWS_CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = account
        try:
            ok = self._advapi.CredWriteW(ctypes.byref(credential), 0)
            return 0 if ok else int(ctypes.get_last_error())
        finally:
            ctypes.memset(buffer, 0, len(secret))

    def delete(self, service: str, account: str) -> int:
        ok = self._advapi.CredDeleteW(
            self._target(service, account),
            _WINDOWS_CRED_TYPE_GENERIC,
            0,
        )
        return 0 if ok else int(ctypes.get_last_error())


class WindowsSecureStore(SecureStore):
    """Secure store backed by the native Windows Credential Manager."""

    def __init__(self, native: object | None = None) -> None:
        self._native = native
        self._native_load_attempted = native is not None
        self._native_load_lock = threading.Lock()

    def _backend(self) -> object | None:
        if not self._native_load_attempted:
            with self._native_load_lock:
                if not self._native_load_attempted:
                    try:
                        self._native = _WindowsCredentialManager()
                    except Exception:
                        self._native = None
                    self._native_load_attempted = True
        return self._native

    def _get(self, service: str, account: str) -> SecureStoreResult:
        backend = self._backend()
        if backend is None:
            return SecureStoreResult(
                STATUS_NOT_AVAILABLE,
                message="Windows Credential Manager is not available.",
            )
        try:
            status, raw = backend.get(service, account)
        except Exception:
            return _run_failure(STATUS_FAILED)
        if status == _WINDOWS_ERROR_NOT_FOUND:
            return SecureStoreResult(STATUS_NOT_FOUND, message="Secret was not found.")
        if status != 0 or raw is None:
            return _run_failure(STATUS_FAILED)
        try:
            secret = raw.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return _run_failure(STATUS_FAILED)
        if not _valid_secret(secret):
            return _run_failure(STATUS_FAILED)
        return SecureStoreResult(STATUS_SUCCESS, value=secret, message="Secret retrieved.")

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        backend = self._backend()
        if backend is None:
            return SecureStoreResult(
                STATUS_NOT_AVAILABLE,
                message="Windows Credential Manager is not available.",
            )
        try:
            status = backend.set(service, account, secret.encode("utf-8"))
        except Exception:
            return _run_failure(STATUS_FAILED)
        if status != 0:
            return _run_failure(STATUS_FAILED)
        return SecureStoreResult(STATUS_SUCCESS, message="Secret stored.")

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        backend = self._backend()
        if backend is None:
            return SecureStoreResult(
                STATUS_NOT_AVAILABLE,
                message="Windows Credential Manager is not available.",
            )
        try:
            status = backend.delete(service, account)
        except Exception:
            return _run_failure(STATUS_FAILED)
        if status == _WINDOWS_ERROR_NOT_FOUND:
            return SecureStoreResult(STATUS_NOT_FOUND, message="Secret was not found.")
        if status != 0:
            return _run_failure(STATUS_FAILED)
        return SecureStoreResult(STATUS_SUCCESS, message="Secret deleted.")


class UnsupportedSecureStore(SecureStore):
    """Honest adapter for unknown or unsupported operating systems."""

    @staticmethod
    def _not_available() -> SecureStoreResult:
        return SecureStoreResult(
            STATUS_NOT_AVAILABLE,
            message="Secure storage is not available on this platform.",
        )

    def _get(self, service: str, account: str) -> SecureStoreResult:
        return self._not_available()

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        return self._not_available()

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        return self._not_available()


def create_secure_store(system: str | None = None) -> SecureStore:
    """Create the secure-store adapter for ``system`` or the current OS."""

    detected = platform.system() if system is None else system
    normalized = detected.strip().casefold()
    if normalized in {"darwin", "macos"}:
        return MacOSKeychainStore()
    if normalized == "linux":
        return LinuxSecretToolStore()
    if normalized == "windows":
        return WindowsSecureStore()
    return UnsupportedSecureStore()


def get_secure_store(system: str | None = None) -> SecureStore:
    """Compatibility-friendly name for :func:`create_secure_store`."""

    return create_secure_store(system)


__all__ = [
    "STATUS_FAILED",
    "STATUS_NOT_AVAILABLE",
    "STATUS_NOT_FOUND",
    "STATUS_SUCCESS",
    "LinuxSecretToolStore",
    "MacOSKeychainStore",
    "SecureStore",
    "SecureStoreResult",
    "UnsupportedSecureStore",
    "WindowsSecureStore",
    "create_secure_store",
    "get_secure_store",
]
