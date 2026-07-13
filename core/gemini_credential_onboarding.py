"""Testable validation-before-storage orchestration for Gemini onboarding."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from core.credential_service import CredentialResult, store_gemini_api_key
from core.gemini_credential_validator import (
    GeminiCredentialValidationResult,
    validate_gemini_api_key,
)


@dataclass(frozen=True, slots=True)
class GeminiCredentialOnboardingResult:
    ok: bool
    phase: str
    status: str


def validate_and_store_gemini_api_key(
    value: str,
    *,
    validator: Callable[[str], GeminiCredentialValidationResult] = validate_gemini_api_key,
    storage: Callable[[str], CredentialResult] = store_gemini_api_key,
) -> GeminiCredentialOnboardingResult:
    """Validate first; storage is never attempted after validation failure."""

    validation = validator(value)
    if not validation.ok:
        return GeminiCredentialOnboardingResult(
            ok=False,
            phase="validation",
            status=validation.status,
        )
    stored = storage(value)
    return GeminiCredentialOnboardingResult(
        ok=stored.ok,
        phase="storage",
        status="success" if stored.ok else stored.status,
    )


__all__ = [
    "GeminiCredentialOnboardingResult",
    "validate_and_store_gemini_api_key",
]
