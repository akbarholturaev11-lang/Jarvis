"""Cross-platform operations tooling for the JARVIS product backend.

These modules generate secret material, validate production configuration
fail-closed, back up and restore the SQLite databases and payment evidence,
run schema migrations, and stand up a local production-like TLS environment.

They are written in the standard library plus ``cryptography`` so they run on
macOS, Windows, and Linux.  Owner-only file hardening is applied with POSIX mode
bits where the platform supports it; on Windows the tooling reports an honest
``manual`` status with NTFS ACL guidance instead of faking ``0600`` permissions.
The production backend runtime itself enforces POSIX owner-only permissions, so
a customer-facing deployment host must be POSIX (Linux or macOS); Windows is
supported for running this tooling, not for hosting the hardened runtime.
"""

__all__: list[str] = []
