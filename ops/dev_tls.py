"""Stand up a local production-like TLS environment for the backend.

Generates a self-signed certificate for ``localhost`` and runs uvicorn with TLS
so the app sees an ``https`` scheme locally, exercising the HTTPS-only policy and
HSTS exactly as in production (uvicorn terminates TLS directly, no proxy needed).

Usage::

    python -m ops.dev_tls --out-dir ./devcerts --serve
    # then browse https://localhost:8443/ (self-signed: expect a browser warning)

The generated key is written owner-only and is gitignored (``*.pem`` / ``*.key``).
This is a development convenience, not a production certificate.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import ipaddress
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ._common import emit, harden_file

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8443


def generate_self_signed_cert(
    out_dir: Path,
    *,
    hostname: str = "localhost",
    days: int = 825,
) -> tuple[Path, Path]:
    """Write a self-signed cert + owner-only key for local TLS; return paths."""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS Dev TLS"),
    ])
    now = _dt.datetime.now(_dt.timezone.utc)
    alt_names: list[x509.GeneralName] = [
        x509.DNSName(hostname),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )

    cert_path = out_dir / "dev-cert.pem"
    key_path = out_dir / "dev-key.pem"
    cert_path.write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
    )
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    harden_file(key_path)
    harden_file(cert_path, mode=0o644)
    return cert_path, key_path


def uvicorn_command(
    cert_path: Path,
    key_path: Path,
    *,
    host: str,
    port: int,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "product_backend.runtime:create_app_from_environment",
        "--factory",
        "--host",
        host,
        "--port",
        str(port),
        "--ssl-certfile",
        str(cert_path),
        "--ssl-keyfile",
        str(key_path),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate local TLS certs and optionally run the backend."
    )
    parser.add_argument("--out-dir", type=Path, default=Path("./devcerts"))
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--hostname", default="localhost")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)

    cert_path, key_path = generate_self_signed_cert(
        args.out_dir, hostname=args.hostname
    )
    command = uvicorn_command(
        cert_path, key_path, host=args.host, port=args.port
    )
    emit(f"[ok] wrote {cert_path} and {key_path} (owner-only key)")
    emit("[hint] set JARVIS_REQUIRE_HTTPS=true and JARVIS_API_ALLOWED_HOSTS to")
    emit("       include 'localhost' before starting the backend.")
    emit("[run] " + " ".join(command))
    if args.serve:
        os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
