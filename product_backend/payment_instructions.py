"""Private, fail-closed loader for bilingual manual-payment instructions."""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from core.product_offer import (
    MAX_PAYMENT_INSTRUCTIONS_TEXT_LENGTH,
    MAX_PAYMENT_METHOD_TEXT_LENGTH,
    MAX_PAYMENT_RECIPIENT_TEXT_LENGTH,
)


STATUS_CONFIGURED: Final = "configured"
STATUS_NOT_CONFIGURED: Final = "not_configured"
PAYMENT_INSTRUCTIONS_SCHEMA: Final = "jarvis.payment-instructions.v1"
MAX_PAYMENT_INSTRUCTIONS_BYTES: Final = 16 * 1024
MAX_RECIPIENT_TEXT_LENGTH: Final = MAX_PAYMENT_RECIPIENT_TEXT_LENGTH
MAX_METHOD_TEXT_LENGTH: Final = MAX_PAYMENT_METHOD_TEXT_LENGTH
MAX_INSTRUCTIONS_TEXT_LENGTH: Final = MAX_PAYMENT_INSTRUCTIONS_TEXT_LENGTH

_ROOT_FIELDS: Final = frozenset(
    {"schema", "recipient", "method", "instructions"}
)
_LOCALIZED_FIELDS: Final = frozenset({"en", "ru"})
_WHITESPACE_RE: Final = re.compile(r"\s+")
_MARKDOWN_LINK_RE: Final = re.compile(r"\[[^\]]*\]\([^)]*\)")
_SECRET_ASSIGNMENT_RE: Final = re.compile(
    r"(?i)\b(api[ _-]?key|token|password|secret|authorization)"
    r"\s*[:=]\s*\S+"
)
_BEARER_RE: Final = re.compile(r"(?i)\bbearer\s+\S+")
_PRIVATE_KEY_RE: Final = re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----")


class _PaymentInstructionsInvalid(ValueError):
    """Internal marker; untrusted details never escape the loader."""


@dataclass(frozen=True, slots=True, repr=False)
class LocalizedPaymentText:
    en: str = field(repr=False)
    ru: str = field(repr=False)

    def __repr__(self) -> str:
        return "LocalizedPaymentText(content=<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class PaymentInstructions:
    schema: str
    recipient: str = field(repr=False)
    method: LocalizedPaymentText = field(repr=False)
    instructions: LocalizedPaymentText = field(repr=False)

    def __post_init__(self) -> None:
        if self.schema != PAYMENT_INSTRUCTIONS_SCHEMA:
            raise ValueError("payment instructions schema is invalid")
        if not isinstance(self.method, LocalizedPaymentText) or not isinstance(
            self.instructions,
            LocalizedPaymentText,
        ):
            raise TypeError("localized payment text is invalid")
        object.__setattr__(
            self,
            "recipient",
            _safe_text(
                self.recipient,
                maximum_length=MAX_RECIPIENT_TEXT_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "method",
            LocalizedPaymentText(
                _safe_text(
                    self.method.en,
                    maximum_length=MAX_METHOD_TEXT_LENGTH,
                ),
                _safe_text(
                    self.method.ru,
                    maximum_length=MAX_METHOD_TEXT_LENGTH,
                ),
            ),
        )
        object.__setattr__(
            self,
            "instructions",
            LocalizedPaymentText(
                _safe_text(
                    self.instructions.en,
                    maximum_length=MAX_INSTRUCTIONS_TEXT_LENGTH,
                ),
                _safe_text(
                    self.instructions.ru,
                    maximum_length=MAX_INSTRUCTIONS_TEXT_LENGTH,
                ),
            ),
        )

    def __repr__(self) -> str:
        return (
            "PaymentInstructions("
            f"schema={self.schema!r}, content=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class PaymentInstructionsLoadResult:
    status: str
    instructions: PaymentInstructions | None = field(default=None, repr=False)
    message: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.status not in {STATUS_CONFIGURED, STATUS_NOT_CONFIGURED}:
            raise ValueError("unsupported payment-instructions status")
        if (self.status == STATUS_CONFIGURED) != (
            self.instructions is not None
        ):
            raise ValueError("configured status must match instructions")

    @property
    def configured(self) -> bool:
        return self.status == STATUS_CONFIGURED and self.instructions is not None

    def __repr__(self) -> str:
        claims = "available" if self.configured else "none"
        return (
            "PaymentInstructionsLoadResult("
            f"status={self.status!r}, claims={claims!r})"
        )

    __str__ = __repr__


def _result(
    status: str,
    instructions: PaymentInstructions | None = None,
) -> PaymentInstructionsLoadResult:
    message = (
        "Payment instructions are configured."
        if status == STATUS_CONFIGURED
        else "Payment instructions are not configured."
    )
    return PaymentInstructionsLoadResult(status, instructions, message)


def _reject_duplicate_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise _PaymentInstructionsInvalid("duplicate JSON key")
        document[key] = value
    return document


def _reject_non_finite(_value: str) -> None:
    raise _PaymentInstructionsInvalid("non-finite JSON value")


def _safe_text(value: object, *, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise _PaymentInstructionsInvalid("payment text must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise _PaymentInstructionsInvalid("payment text contains control characters")
    if (
        any(character in normalized for character in "<>`*")
        or normalized.lstrip().startswith("#")
        or _MARKDOWN_LINK_RE.search(normalized)
    ):
        raise _PaymentInstructionsInvalid("payment text contains markup")
    if (
        _SECRET_ASSIGNMENT_RE.search(normalized)
        or _BEARER_RE.search(normalized)
        or _PRIVATE_KEY_RE.search(normalized)
    ):
        raise _PaymentInstructionsInvalid("payment text contains secret material")
    sanitized = _WHITESPACE_RE.sub(" ", normalized).strip()
    if (
        not sanitized
        or len(sanitized) > maximum_length
        or len(sanitized.encode("utf-8")) > maximum_length * 4
    ):
        raise _PaymentInstructionsInvalid("payment text length is invalid")
    return sanitized


def _localized_text(
    value: object,
    *,
    maximum_length: int,
) -> LocalizedPaymentText:
    if not isinstance(value, Mapping) or frozenset(value) != _LOCALIZED_FIELDS:
        raise _PaymentInstructionsInvalid("localized payment text is invalid")
    return LocalizedPaymentText(
        _safe_text(value["en"], maximum_length=maximum_length),
        _safe_text(value["ru"], maximum_length=maximum_length),
    )


def _parse(raw: bytes) -> PaymentInstructions:
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_finite,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        _PaymentInstructionsInvalid,
    ) as exc:
        raise _PaymentInstructionsInvalid(
            "payment instructions JSON is invalid"
        ) from exc
    if type(document) is not dict or frozenset(document) != _ROOT_FIELDS:
        raise _PaymentInstructionsInvalid("payment instructions schema is invalid")
    if document.get("schema") != PAYMENT_INSTRUCTIONS_SCHEMA:
        raise _PaymentInstructionsInvalid("payment instructions schema is invalid")
    return PaymentInstructions(
        PAYMENT_INSTRUCTIONS_SCHEMA,
        _safe_text(
            document.get("recipient"),
            maximum_length=MAX_RECIPIENT_TEXT_LENGTH,
        ),
        _localized_text(
            document.get("method"),
            maximum_length=MAX_METHOD_TEXT_LENGTH,
        ),
        _localized_text(
            document.get("instructions"),
            maximum_length=MAX_INSTRUCTIONS_TEXT_LENGTH,
        ),
    )


def _file_snapshot(opened: os.stat_result) -> tuple[int, ...]:
    return (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mode,
        opened.st_uid,
        opened.st_nlink,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_absolute_no_follow(path: Path) -> int:
    parts = path.parts
    if (
        not parts
        or parts[0] != os.sep
        or len(parts) < 2
        or any(component in {"", ".", ".."} for component in parts[1:])
    ):
        raise _PaymentInstructionsInvalid("private configuration path is invalid")
    root_descriptor: int | None = None
    current_descriptor: int | None = None
    try:
        root_descriptor = os.open(os.sep, _directory_flags())
        current_descriptor = root_descriptor
        for component in parts[1:-1]:
            next_descriptor = os.open(
                component,
                _directory_flags(),
                dir_fd=current_descriptor,
            )
            if current_descriptor != root_descriptor:
                os.close(current_descriptor)
            current_descriptor = next_descriptor
        return os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=current_descriptor,
        )
    except _PaymentInstructionsInvalid:
        raise
    except OSError as exc:
        raise _PaymentInstructionsInvalid(
            "private configuration is not available"
        ) from exc
    finally:
        if current_descriptor is not None and current_descriptor != root_descriptor:
            try:
                os.close(current_descriptor)
            except OSError:
                pass
        if root_descriptor is not None:
            try:
                os.close(root_descriptor)
            except OSError:
                pass


def _read_private_json(path: Path) -> bytes:
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "geteuid")
        or os.open not in getattr(os, "supports_dir_fd", set())
        or not path.is_absolute()
        or path == Path(os.sep)
        or path.is_symlink()
    ):
        raise _PaymentInstructionsInvalid("private configuration path is invalid")
    descriptor: int | None = None
    try:
        descriptor = _open_absolute_no_follow(path)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) not in {0o400, 0o600}
            or not 1 <= before.st_size <= MAX_PAYMENT_INSTRUCTIONS_BYTES
        ):
            raise _PaymentInstructionsInvalid(
                "private configuration file is not trusted"
            )
        raw = os.read(descriptor, MAX_PAYMENT_INSTRUCTIONS_BYTES + 1)
        if len(raw) != before.st_size or os.read(descriptor, 1):
            raise _PaymentInstructionsInvalid(
                "private configuration read is incomplete"
            )
        after = os.fstat(descriptor)
        if _file_snapshot(before) != _file_snapshot(after):
            raise _PaymentInstructionsInvalid(
                "private configuration changed during read"
            )
        return raw
    except _PaymentInstructionsInvalid:
        raise
    except OSError as exc:
        raise _PaymentInstructionsInvalid(
            "private configuration is not available"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def load_payment_instructions(
    path: str | os.PathLike[str] | None,
) -> PaymentInstructionsLoadResult:
    """Load private instructions or return an explicit claims-free state."""

    if path is None:
        return _result(STATUS_NOT_CONFIGURED)
    try:
        supplied = Path(path).expanduser()
        raw = _read_private_json(supplied)
        instructions = _parse(raw)
    except (
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        _PaymentInstructionsInvalid,
    ):
        return _result(STATUS_NOT_CONFIGURED)
    return _result(STATUS_CONFIGURED, instructions)


__all__ = [
    "MAX_INSTRUCTIONS_TEXT_LENGTH",
    "MAX_METHOD_TEXT_LENGTH",
    "MAX_PAYMENT_INSTRUCTIONS_BYTES",
    "MAX_RECIPIENT_TEXT_LENGTH",
    "PAYMENT_INSTRUCTIONS_SCHEMA",
    "STATUS_CONFIGURED",
    "STATUS_NOT_CONFIGURED",
    "LocalizedPaymentText",
    "PaymentInstructions",
    "PaymentInstructionsLoadResult",
    "load_payment_instructions",
]
