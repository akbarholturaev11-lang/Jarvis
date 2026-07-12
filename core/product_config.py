"""Strict, non-secret client configuration for product services."""

from __future__ import annotations

import base64
import binascii
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from core.app_paths import AppPaths, resolve_app_paths
from core.product_api_client import ProductApiClient


STATUS_SUCCESS: Final = "success"
STATUS_NOT_CONFIGURED: Final = "not_configured"
STATUS_INVALID: Final = "invalid"
CONFIG_SCHEMA: Final = "jarvis.product-client.v1"
CONFIG_FILE_NAME: Final = "product.json"
MAX_CONFIG_BYTES: Final = 64 * 1024
_KEY_ID_RE: Final = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")


@dataclass(frozen=True, slots=True)
class ProductClientConfig:
    api_base_url: str = field(repr=False)
    allow_insecure_localhost: bool
    entitlement_public_keys: Mapping[str, bytes] = field(repr=False)
    release_public_keys: Mapping[str, bytes] = field(repr=False)

    def api_client(self) -> ProductApiClient:
        return ProductApiClient(
            self.api_base_url,
            allow_insecure_localhost=self.allow_insecure_localhost,
        )

    def __repr__(self) -> str:
        return (
            "ProductClientConfig(api_base_url=<redacted>, "
            f"entitlement_keys={len(self.entitlement_public_keys)}, "
            f"release_keys={len(self.release_public_keys)})"
        )


@dataclass(frozen=True, slots=True)
class ProductConfigResult:
    status: str
    config: ProductClientConfig | None = field(default=None, repr=False)
    message: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS and self.config is not None


def load_product_client_config(
    *,
    app_paths: AppPaths | None = None,
    source_config: Path | None = None,
    packaged: bool | None = None,
) -> ProductConfigResult:
    paths = resolve_app_paths() if app_paths is None else app_paths
    user_path = paths.config_dir / CONFIG_FILE_NAME
    fallback = (
        paths.resource_root / "config" / CONFIG_FILE_NAME
        if source_config is None
        else Path(source_config)
    )
    is_packaged = bool(getattr(sys, "frozen", False)) if packaged is None else packaged
    # Pinned API origin and trust roots in a packaged build are immutable bundle
    # resources. A same-user writable config must never replace them.
    selected = fallback if is_packaged else user_path if user_path.is_file() else fallback
    if not selected.is_file():
        return ProductConfigResult(
            STATUS_NOT_CONFIGURED, message="Product service is not configured."
        )
    try:
        if selected.is_symlink() or not 1 <= selected.stat().st_size <= MAX_CONFIG_BYTES:
            raise ValueError("invalid product config path")
        data = json.loads(selected.read_text(encoding="utf-8"))
        if type(data) is not dict or frozenset(data) != {
            "schema",
            "api_base_url",
            "allow_insecure_localhost",
            "entitlement_public_keys",
            "release_public_keys",
        }:
            raise ValueError("invalid product config schema")
        if data["schema"] != CONFIG_SCHEMA:
            raise ValueError("unsupported product config schema")
        if type(data["allow_insecure_localhost"]) is not bool:
            raise ValueError("invalid localhost policy")
        api = ProductApiClient(
            data["api_base_url"],
            allow_insecure_localhost=data["allow_insecure_localhost"],
        )
        entitlement_keys = _decode_key_map(data["entitlement_public_keys"])
        release_keys = _decode_key_map(data["release_public_keys"])
        return ProductConfigResult(
            STATUS_SUCCESS,
            ProductClientConfig(
                api.base_url,
                data["allow_insecure_localhost"],
                entitlement_keys,
                release_keys,
            ),
            "Product service configuration loaded.",
        )
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return ProductConfigResult(
            STATUS_INVALID, message="Product service configuration is invalid."
        )


def _decode_key_map(value: object) -> dict[str, bytes]:
    if not isinstance(value, Mapping) or not 1 <= len(value) <= 16:
        raise ValueError("trusted keys are invalid")
    decoded: dict[str, bytes] = {}
    for key_id, encoded in value.items():
        if (
            type(key_id) is not str
            or _KEY_ID_RE.fullmatch(key_id) is None
            or type(encoded) is not str
            or len(encoded) != 43
        ):
            raise ValueError("trusted key is invalid")
        try:
            raw = base64.b64decode(encoded + "=", altchars=b"-_", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("trusted key is invalid") from exc
        canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        if len(raw) != 32 or canonical != encoded:
            raise ValueError("trusted key is invalid")
        decoded[key_id] = raw
    return decoded


__all__ = [
    "CONFIG_FILE_NAME",
    "CONFIG_SCHEMA",
    "STATUS_INVALID",
    "STATUS_NOT_CONFIGURED",
    "STATUS_SUCCESS",
    "ProductClientConfig",
    "ProductConfigResult",
    "load_product_client_config",
]
