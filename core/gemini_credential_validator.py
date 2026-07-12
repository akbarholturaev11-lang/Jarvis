"""Bounded Gemini credential validation before secure persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final


STATUS_SUCCESS: Final = "success"
STATUS_INVALID: Final = "invalid"
STATUS_NETWORK_UNAVAILABLE: Final = "network_unavailable"
STATUS_SERVER_UNAVAILABLE: Final = "server_unavailable"
_VALID_STATUSES: Final = frozenset(
    {
        STATUS_SUCCESS,
        STATUS_INVALID,
        STATUS_NETWORK_UNAVAILABLE,
        STATUS_SERVER_UNAVAILABLE,
    }
)


@dataclass(frozen=True, slots=True)
class GeminiCredentialValidationResult:
    status: str
    message: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError("unsupported credential validation status")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS


def validate_gemini_api_key(
    api_key: str,
    *,
    probe: Callable[[str], None] | None = None,
) -> GeminiCredentialValidationResult:
    """Authenticate with a bounded model-list request; never persist or log key."""

    if (
        type(api_key) is not str
        or api_key != api_key.strip()
        or not 16 <= len(api_key) <= 512
        or any(ord(character) < 33 or ord(character) == 127 for character in api_key)
    ):
        return GeminiCredentialValidationResult(
            STATUS_INVALID, "Gemini credential is invalid."
        )
    selected = _default_probe if probe is None else probe
    try:
        selected(api_key)
    except Exception as exc:
        response = getattr(exc, "response", None)
        code = getattr(exc, "code", None)
        if not isinstance(code, int):
            code = getattr(exc, "status_code", None)
        if not isinstance(code, int) and response is not None:
            code = getattr(response, "status_code", None)
        if code in {400, 401, 403}:
            return GeminiCredentialValidationResult(
                STATUS_INVALID, "Gemini credential was rejected."
            )
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return GeminiCredentialValidationResult(
                STATUS_NETWORK_UNAVAILABLE,
                "Network is unavailable for credential validation.",
            )
        return GeminiCredentialValidationResult(
            STATUS_SERVER_UNAVAILABLE,
            "Gemini credential validation is temporarily unavailable.",
        )
    return GeminiCredentialValidationResult(
        STATUS_SUCCESS, "Gemini credential was validated."
    )


def _default_probe(api_key: str) -> None:
    from google import genai

    client = genai.Client(
        api_key=api_key,
        http_options={"api_version": "v1beta", "timeout": 10_000},
    )
    try:
        pager = client.models.list(config={"page_size": 1})
        next(iter(pager), None)
    finally:
        client.close()


__all__ = [
    "STATUS_INVALID",
    "STATUS_NETWORK_UNAVAILABLE",
    "STATUS_SERVER_UNAVAILABLE",
    "STATUS_SUCCESS",
    "GeminiCredentialValidationResult",
    "validate_gemini_api_key",
]
