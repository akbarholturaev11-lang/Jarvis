"""Shared bounded fields for the product release and purchase display contract."""

from __future__ import annotations

from typing import Final


MAX_PAYMENT_METHOD_TEXT_LENGTH: Final = 120
MAX_PAYMENT_RECIPIENT_TEXT_LENGTH: Final = 160
MAX_PAYMENT_INSTRUCTIONS_TEXT_LENGTH: Final = 2000


__all__ = [
    "MAX_PAYMENT_INSTRUCTIONS_TEXT_LENGTH",
    "MAX_PAYMENT_METHOD_TEXT_LENGTH",
    "MAX_PAYMENT_RECIPIENT_TEXT_LENGTH",
]
