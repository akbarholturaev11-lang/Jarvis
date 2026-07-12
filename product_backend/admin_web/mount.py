"""Mount the dependency-free administration console under an isolated prefix."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.staticfiles import StaticFiles


ADMIN_WEB_CSP = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "img-src 'self' blob:; "
    "manifest-src 'none'; "
    "object-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'"
)

_MOUNT_PATH_RE = re.compile(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*")
_STATIC_ROOT = Path(__file__).resolve().parent / "static"


class _AdminWebHeadersMiddleware:
    """Override API-only CSP solely for the mounted same-origin web console."""

    def __init__(self, app: Any, *, path_prefix: str) -> None:
        self.app = app
        self.path_prefix = path_prefix

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope.get("type") != "http" or not self._matches(path):
            await self.app(scope, receive, send)
            return

        async def send_with_admin_headers(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Pragma"] = "no-cache"
                headers["Content-Security-Policy"] = ADMIN_WEB_CSP
                headers["Cross-Origin-Opener-Policy"] = "same-origin"
                headers["Cross-Origin-Resource-Policy"] = "same-origin"
                headers["Permissions-Policy"] = (
                    "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
                )
                headers["Referrer-Policy"] = "no-referrer"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
            await send(message)

        await self.app(scope, receive, send_with_admin_headers)

    def _matches(self, path: object) -> bool:
        return isinstance(path, str) and (
            path == self.path_prefix or path.startswith(f"{self.path_prefix}/")
        )


def _normalize_mount_path(path: object) -> str:
    if not isinstance(path, str):
        raise TypeError("admin web mount path must be a string")
    normalized = path.rstrip("/")
    if normalized in {"", "/", "/api"} or _MOUNT_PATH_RE.fullmatch(normalized) is None:
        raise ValueError("admin web mount path is invalid")
    return normalized


def mount_admin_web(app: Any, *, path: str = "/admin") -> str:
    """Mount static files without catching API routes and return the prefix.

    Call this once during application construction, after API routes have been
    registered and before the application starts serving requests.
    """

    if not callable(getattr(app, "mount", None)) or not callable(
        getattr(app, "add_middleware", None)
    ):
        raise TypeError("admin web host must support mount and add_middleware")
    prefix = _normalize_mount_path(path)
    app.add_middleware(_AdminWebHeadersMiddleware, path_prefix=prefix)
    app.mount(
        prefix,
        StaticFiles(directory=_STATIC_ROOT, html=True, check_dir=True),
        name="jarvis-admin-web",
    )
    return prefix


__all__ = ["ADMIN_WEB_CSP", "mount_admin_web"]
