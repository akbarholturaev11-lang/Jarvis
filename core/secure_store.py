"""Platform-neutral secure secret storage.

The public contract is deliberately small: ``get``, ``set`` and ``delete``
always return a :class:`SecureStoreResult` with one of four honest statuses.
Platform commands are executed as argv lists with ``shell=False`` and command
output is never copied into result messages.

This module does not migrate or read any existing configuration file.  It is an
isolated foundation that callers can adopt explicitly in a later change.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
_MACOS_SECURITY: Final = "/usr/bin/security"
_MACOS_NOT_FOUND_EXIT_CODES: Final = frozenset({44})
_SECRET_TOOL_NOT_FOUND_EXIT_CODES: Final = frozenset({1})


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


def _secret_from_stdout(completed: subprocess.CompletedProcess[str]) -> str:
    raw = completed.stdout
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        text = raw
    else:
        return ""
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


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


class MacOSKeychainStore(SecureStore):
    """Secure store backed by the macOS ``security`` command."""

    def _get(self, service: str, account: str) -> SecureStoreResult:
        completed, run_status = _run_command(
            [
                _MACOS_SECURITY,
                "find-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
            ]
        )
        if completed is None:
            return _run_failure(run_status)
        if completed.returncode in _MACOS_NOT_FOUND_EXIT_CODES:
            return SecureStoreResult(STATUS_NOT_FOUND, message="Secret was not found.")
        if completed.returncode != 0:
            return _run_failure(STATUS_FAILED)
        secret = _secret_from_stdout(completed)
        if not secret:
            return SecureStoreResult(STATUS_FAILED, message="Secure storage returned no secret.")
        return SecureStoreResult(STATUS_SUCCESS, value=secret, message="Secret retrieved.")

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        completed, run_status = _run_command(
            [
                _MACOS_SECURITY,
                "add-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-U",
                "-w",
            ],
            input_text=f"{secret}\n",
        )
        if completed is None:
            return _run_failure(run_status)
        if completed.returncode != 0:
            return _run_failure(STATUS_FAILED)
        return SecureStoreResult(STATUS_SUCCESS, message="Secret stored.")

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        completed, run_status = _run_command(
            [
                _MACOS_SECURITY,
                "delete-generic-password",
                "-a",
                account,
                "-s",
                service,
            ]
        )
        if completed is None:
            return _run_failure(run_status)
        if completed.returncode in _MACOS_NOT_FOUND_EXIT_CODES:
            return SecureStoreResult(STATUS_NOT_FOUND, message="Secret was not found.")
        if completed.returncode != 0:
            return _run_failure(STATUS_FAILED)
        return SecureStoreResult(STATUS_SUCCESS, message="Secret deleted.")


class LinuxSecretToolStore(SecureStore):
    """Secure store backed by ``secret-tool`` when that binary is available."""

    def __init__(self, binary_path: str | None = None) -> None:
        self._binary_path = binary_path or shutil.which("secret-tool")

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
        if completed.returncode in _SECRET_TOOL_NOT_FOUND_EXIT_CODES:
            return SecureStoreResult(STATUS_NOT_FOUND, message="Secret was not found.")
        if completed.returncode != 0:
            return _run_failure(STATUS_FAILED)
        secret = _secret_from_stdout(completed)
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
            input_text=f"{secret}\n",
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
        if completed.returncode in _SECRET_TOOL_NOT_FOUND_EXIT_CODES:
            return SecureStoreResult(STATUS_NOT_FOUND, message="Secret was not found.")
        if completed.returncode != 0:
            return _run_failure(STATUS_FAILED)
        return SecureStoreResult(STATUS_SUCCESS, message="Secret deleted.")


class WindowsSecureStore(SecureStore):
    """Honest placeholder until a verified Windows secure backend is added."""

    @staticmethod
    def _not_available() -> SecureStoreResult:
        return SecureStoreResult(
            STATUS_NOT_AVAILABLE,
            message="Windows secure storage is not available in this build.",
        )

    def _get(self, service: str, account: str) -> SecureStoreResult:
        return self._not_available()

    def _set(self, service: str, account: str, secret: str) -> SecureStoreResult:
        return self._not_available()

    def _delete(self, service: str, account: str) -> SecureStoreResult:
        return self._not_available()


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
