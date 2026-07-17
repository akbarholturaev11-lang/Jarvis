"""Compose the non-secret client ``config/product.json`` from public trust material.

The trust material (entitlement + release public keys) is produced by
``ops.gen_secrets`` as ``client-trust.json`` — the only moment the entitlement
public key is known alongside the release public key without ever exposing a
private key. This tool combines that trust material with the operator-supplied
real HTTPS API origin to produce the strict ``jarvis.product-client.v1`` client
config that ``core.product_config.load_product_client_config`` accepts and that
the macOS build script pins via ``--product-config``.

Usage::

    python -m ops.build_client_config \\
        --trust-file /path/to/client-trust.json \\
        --api-base-url https://api.example.com \\
        --out config/product.json

The output contains only public material. It is validated by round-trip loading
through the real client loader before it is written, and ``allow_insecure_localhost``
defaults to ``false`` so a production origin is required unless a development
profile explicitly opts in.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from core.app_paths import resolve_app_paths
from core.product_api_client import ProductApiClient
from core.product_config import (
    CONFIG_SCHEMA,
    MAX_CONFIG_BYTES,
    load_product_client_config,
)

TRUST_SCHEMA: Final = "jarvis.product-client-trust.v1"
_TRUST_FIELDS: Final = frozenset(
    {"schema", "entitlement_public_keys", "release_public_keys"}
)


def _copy_key_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or not 1 <= len(value) <= 16:
        raise ValueError("client trust keys are invalid")
    result: dict[str, str] = {}
    for key_id, encoded in value.items():
        if type(key_id) is not str or type(encoded) is not str:
            raise ValueError("client trust key is invalid")
        result[key_id] = encoded
    return result


def build_client_document(
    trust: Mapping[str, object],
    *,
    api_base_url: str,
    allow_insecure_localhost: bool = False,
) -> dict[str, object]:
    """Return a strict client config document from public trust material.

    Raises ``ValueError`` for malformed trust material or a non-HTTPS origin.
    Full cryptographic key validation is performed by ``validate_document``.
    """

    if not isinstance(trust, Mapping) or frozenset(trust) != _TRUST_FIELDS:
        raise ValueError("client trust material is invalid")
    if trust["schema"] != TRUST_SCHEMA:
        raise ValueError("unsupported client trust schema")
    if type(api_base_url) is not str:
        raise ValueError("api_base_url must be a string")
    # Validate and canonicalize the origin exactly as the client will at runtime.
    canonical = ProductApiClient(
        api_base_url,
        allow_insecure_localhost=allow_insecure_localhost,
    ).base_url
    return {
        "schema": CONFIG_SCHEMA,
        "api_base_url": canonical,
        "allow_insecure_localhost": bool(allow_insecure_localhost),
        "entitlement_public_keys": _copy_key_map(trust["entitlement_public_keys"]),
        "release_public_keys": _copy_key_map(trust["release_public_keys"]),
    }


def validate_document(document: Mapping[str, object]) -> None:
    """Round-trip the document through the real client loader; raise on rejection."""

    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        candidate = temp_dir / "product.json"
        candidate.write_text(json.dumps(document), encoding="utf-8")
        # An explicit AppPaths avoids resolving/creating real per-user directories.
        paths = resolve_app_paths(
            platform_name="linux",
            home=temp_dir,
            environ={},
            resource_root=temp_dir,
        )
        result = load_product_client_config(
            app_paths=paths,
            source_config=candidate,
            packaged=True,
        )
    if not result.ok:
        raise ValueError(result.message or "produced client config failed validation")


def _read_trust_file(path: Path) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("client trust file is invalid")
    if not 1 <= path.stat().st_size <= MAX_CONFIG_BYTES:
        raise ValueError("client trust file size is invalid")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError("client trust file is invalid")
    return data


def _write_atomic(out_path: Path, text: str) -> None:
    if out_path.is_symlink():
        raise ValueError("output path must not be a symlink")
    parent = out_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=parent, prefix=".product-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, out_path)
    except BaseException:
        with_suppressed = Path(temp_name)
        if with_suppressed.exists():
            with_suppressed.unlink()
        raise


def build_client_config_file(
    *,
    trust_file: Path,
    api_base_url: str,
    out_path: Path,
    allow_insecure_localhost: bool = False,
) -> dict[str, object]:
    """Build, validate, and atomically write ``config/product.json``."""

    trust = _read_trust_file(trust_file)
    document = build_client_document(
        trust,
        api_base_url=api_base_url,
        allow_insecure_localhost=allow_insecure_localhost,
    )
    validate_document(document)
    _write_atomic(out_path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the non-secret client config/product.json.",
    )
    parser.add_argument(
        "--trust-file",
        required=True,
        type=Path,
        help="client-trust.json produced by ops.gen_secrets",
    )
    parser.add_argument(
        "--api-base-url",
        required=True,
        help="real HTTPS product API origin, e.g. https://api.example.com",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="output path for the client product.json (e.g. config/product.json)",
    )
    parser.add_argument(
        "--allow-insecure-localhost",
        action="store_true",
        help="DEVELOPMENT ONLY: permit an http loopback origin; never in production",
    )
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])

    try:
        build_client_config_file(
            trust_file=args.trust_file,
            api_base_url=args.api_base_url,
            out_path=args.out,
            allow_insecure_localhost=args.allow_insecure_localhost,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 1

    print(f"[ok] wrote client config: {args.out}")
    if args.allow_insecure_localhost:
        print(
            "[warning] allow_insecure_localhost is enabled; this is a development "
            "profile only and must be false for a customer-facing build.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
