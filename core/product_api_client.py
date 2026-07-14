"""Bounded stdlib HTTP boundary for JARVIS product services.

The client accepts only HTTPS origins in production.  Plain HTTP can be enabled
explicitly for loopback test servers and is never accepted for another host.
Errors and reprs deliberately retain no URL, request payload, response body, or
authorization material.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


DEFAULT_TIMEOUT_SECONDS: Final = 15.0
MAX_TIMEOUT_SECONDS: Final = 60.0
DEFAULT_MAX_JSON_BYTES: Final = 1024 * 1024
MAX_JSON_REQUEST_BYTES: Final = 256 * 1024
MAX_MULTIPART_FILE_BYTES: Final = 10 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES: Final = 256 * 1024
DEVICE_MISMATCH_ERROR_HEADER: Final = "X-Jarvis-Error-Code"
DEVICE_MISMATCH_ERROR_VALUE: Final = "device_mismatch"

_LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
_JSON_CONTENT_TYPES: Final = frozenset(
    {"application/json", "application/problem+json"}
)


class ApiErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    DEVICE_MISMATCH = "device_mismatch"
    NETWORK_UNAVAILABLE = "network_unavailable"
    SERVER_UNAVAILABLE = "server_unavailable"
    RESPONSE_INVALID = "response_invalid"
    RESPONSE_TOO_LARGE = "response_too_large"
    DOWNLOAD_FAILED = "download_failed"


_ERROR_MESSAGES: Final = {
    ApiErrorCode.INVALID_REQUEST: "Product API request is invalid.",
    ApiErrorCode.UNAUTHORIZED: "Product API authorization was rejected.",
    ApiErrorCode.NOT_FOUND: "Product API resource was not found.",
    ApiErrorCode.CONFLICT: "Product API request conflicts with current state.",
    ApiErrorCode.DEVICE_MISMATCH: "Product license is bound to another device.",
    ApiErrorCode.NETWORK_UNAVAILABLE: "Network is unavailable.",
    ApiErrorCode.SERVER_UNAVAILABLE: "Product API server is unavailable.",
    ApiErrorCode.RESPONSE_INVALID: "Product API response is invalid.",
    ApiErrorCode.RESPONSE_TOO_LARGE: "Product API response exceeds the limit.",
    ApiErrorCode.DOWNLOAD_FAILED: "Product artifact download failed.",
}


class ProductApiError(RuntimeError):
    """Sanitized API failure with no untrusted or sensitive text."""

    def __init__(self, code: ApiErrorCode) -> None:
        if type(code) is not ApiErrorCode:
            raise TypeError("code must be an ApiErrorCode")
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])

    def __repr__(self) -> str:
        return f"ProductApiError(code={self.code.value!r})"


@runtime_checkable
class ApiTransportResponse(Protocol):
    status: int
    headers: object

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...

    def geturl(self) -> str: ...


@runtime_checkable
class ApiTransport(Protocol):
    def open(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> ApiTransportResponse: ...


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, *, allow_insecure_localhost: bool) -> None:
        super().__init__()
        self._allow_insecure_localhost = allow_insecure_localhost

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        original_url = req.full_url
        _validate_absolute_url(
            original_url,
            allow_insecure_localhost=self._allow_insecure_localhost,
        )
        _validate_absolute_url(
            newurl,
            allow_insecure_localhost=self._allow_insecure_localhost,
        )
        if _canonical_origin(original_url) != _canonical_origin(newurl):
            raise ValueError("cross-origin redirect is forbidden")
        sensitive_headers = {
            "authorization",
            "cookie",
            "x-csrf-token",
            "x-device-grant",
            "x-purchase-grant",
        }
        supplied_headers = {
            str(name).casefold()
            for source in (req.headers, req.unredirected_hdrs)
            for name in source
        }
        if supplied_headers & sensitive_headers:
            raise ValueError("credential-bearing redirect is forbidden")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibApiTransport:
    """Default transport using only Python's standard library and system TLS."""

    def __init__(self, *, allow_insecure_localhost: bool = False) -> None:
        context = ssl.create_default_context()
        self._opener = build_opener(
            HTTPSHandler(context=context),
            _SafeRedirectHandler(
                allow_insecure_localhost=allow_insecure_localhost
            ),
        )

    def open(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> ApiTransportResponse:
        request = Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        return self._opener.open(request, timeout=timeout_seconds)

    def __repr__(self) -> str:
        return "UrllibApiTransport(tls=<system-default>)"


@dataclass(frozen=True, slots=True)
class ApiDownloadReceipt:
    destination: Path = field(repr=False)
    byte_size: int
    sha256: str


def _validate_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= MAX_TIMEOUT_SECONDS
    ):
        raise ValueError("timeout_seconds is outside the allowed range")
    return float(value)


def _validate_absolute_url(
    value: object,
    *,
    allow_insecure_localhost: bool,
) -> str:
    if type(value) is not str or not value or len(value) > 2048:
        raise ValueError("product API URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("product API URL is invalid") from exc
    host = parsed.hostname.casefold() if isinstance(parsed.hostname, str) else ""
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.path
    ):
        raise ValueError("product API URL is invalid")
    if parsed.scheme == "https":
        pass
    elif not (
        parsed.scheme == "http"
        and allow_insecure_localhost
        and host in _LOOPBACK_HOSTS
    ):
        raise ValueError("product API requires HTTPS")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("product API URL is invalid")
    decoded_parts = unquote(parsed.path).split("/")
    if any(part in {".", ".."} for part in decoded_parts):
        raise ValueError("product API URL path is invalid")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )


def _canonical_origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    host = parsed.hostname.casefold() if isinstance(parsed.hostname, str) else ""
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, host, port


def _request_path(value: object) -> str:
    if type(value) is not str or not value.startswith("/") or len(value) > 1024:
        raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.path
        or parsed.path.startswith("//")
        or any(part in {".", ".."} for part in unquote(parsed.path).split("/"))
    ):
        raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
    return parsed.path


def _header_value(headers: object, name: str) -> str | None:
    if hasattr(headers, "get"):
        try:
            value = headers.get(name)  # type: ignore[union-attr]
        except Exception:
            return None
        return value if isinstance(value, str) else None
    return None


def _validate_headers(headers: object | None) -> dict[str, str]:
    if headers is None:
        return {}
    if type(headers) is not dict or len(headers) > 32:
        raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if (
            type(key) is not str
            or type(value) is not str
            or not key
            or len(key) > 128
            or len(value) > 8192
            or any(character in key + value for character in "\x00\r\n")
            or key.casefold() in {"host", "content-length"}
        ):
            raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
        normalized[key] = value
    return normalized


def _http_error_code(status: int, headers: object | None = None) -> ApiErrorCode:
    if status in {401, 403}:
        return ApiErrorCode.UNAUTHORIZED
    if status == 404:
        return ApiErrorCode.NOT_FOUND
    if (
        status == 409
        and _header_value(headers, DEVICE_MISMATCH_ERROR_HEADER)
        == DEVICE_MISMATCH_ERROR_VALUE
    ):
        return ApiErrorCode.DEVICE_MISMATCH
    if status in {409, 412}:
        return ApiErrorCode.CONFLICT
    if 500 <= status <= 599:
        return ApiErrorCode.SERVER_UNAVAILABLE
    return ApiErrorCode.RESPONSE_INVALID


class ProductApiClient:
    """Same-origin JSON/download API client with bounded inputs and outputs."""

    __slots__ = (
        "_allow_insecure_localhost",
        "_base_url",
        "_timeout_seconds",
        "_transport",
    )

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        allow_insecure_localhost: bool = False,
        transport: ApiTransport | None = None,
    ) -> None:
        if type(allow_insecure_localhost) is not bool:
            raise TypeError("allow_insecure_localhost must be a boolean")
        self._allow_insecure_localhost = allow_insecure_localhost
        self._base_url = _validate_absolute_url(
            base_url,
            allow_insecure_localhost=allow_insecure_localhost,
        )
        self._timeout_seconds = _validate_timeout(timeout_seconds)
        selected = transport or UrllibApiTransport(
            allow_insecure_localhost=allow_insecure_localhost
        )
        if not isinstance(selected, ApiTransport):
            raise TypeError("transport must implement ApiTransport")
        self._transport = selected

    @property
    def base_url(self) -> str:
        return self._base_url

    def __repr__(self) -> str:
        return "ProductApiClient(base_url=<redacted>, transport=<configured>)"

    def _url(self, path: str) -> str:
        return self._base_url + _request_path(path)

    def _open(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> ApiTransportResponse:
        try:
            response = self._transport.open(
                method=method,
                url=self._url(path),
                headers=headers,
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
        except ProductApiError:
            raise
        except HTTPError as exc:
            try:
                code = _http_error_code(exc.code, getattr(exc, "headers", None))
                exc.close()
            finally:
                raise ProductApiError(code) from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise ProductApiError(ApiErrorCode.NETWORK_UNAVAILABLE) from None
        except Exception:
            raise ProductApiError(ApiErrorCode.SERVER_UNAVAILABLE) from None
        if not isinstance(response, ApiTransportResponse):
            raise ProductApiError(ApiErrorCode.RESPONSE_INVALID)
        try:
            final_url = response.geturl()
            _validate_absolute_url(
                final_url,
                allow_insecure_localhost=self._allow_insecure_localhost,
            )
            base = urlsplit(self._base_url)
            final = urlsplit(final_url)
            if (
                base.scheme != final.scheme
                or base.hostname != final.hostname
                or base.port != final.port
            ):
                raise ValueError("cross-origin response")
            status = response.status
            if type(status) is not int or not 200 <= status <= 299:
                raise ProductApiError(
                    _http_error_code(
                        status if type(status) is int else 0,
                        response.headers,
                    )
                )
        except ProductApiError:
            response.close()
            raise
        except Exception:
            response.close()
            raise ProductApiError(ApiErrorCode.RESPONSE_INVALID) from None
        return response

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        maximum_response_bytes: int = DEFAULT_MAX_JSON_BYTES,
    ) -> dict[str, object]:
        normalized_method = method.upper() if type(method) is str else ""
        if normalized_method not in {"GET", "POST", "PUT", "DELETE"}:
            raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
        if (
            type(maximum_response_bytes) is not int
            or not 1 <= maximum_response_bytes <= DEFAULT_MAX_JSON_BYTES
        ):
            raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
        request_headers = _validate_headers(headers)
        body: bytes | None = None
        if payload is not None:
            if type(payload) is not dict:
                raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
            try:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise ProductApiError(ApiErrorCode.INVALID_REQUEST) from None
            if not body or len(body) > MAX_JSON_REQUEST_BYTES:
                raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
            request_headers["Content-Type"] = "application/json"
        request_headers.setdefault("Accept", "application/json")
        response = self._open(
            method=normalized_method,
            path=path,
            headers=request_headers,
            body=body,
        )
        try:
            content_type = (_header_value(response.headers, "Content-Type") or "")
            media_type = content_type.partition(";")[0].strip().casefold()
            if media_type not in _JSON_CONTENT_TYPES:
                raise ProductApiError(ApiErrorCode.RESPONSE_INVALID)
            declared = _header_value(response.headers, "Content-Length")
            if declared is not None:
                try:
                    if int(declared) > maximum_response_bytes:
                        raise ProductApiError(ApiErrorCode.RESPONSE_TOO_LARGE)
                except ValueError:
                    raise ProductApiError(ApiErrorCode.RESPONSE_INVALID) from None
            raw = response.read(maximum_response_bytes + 1)
            if type(raw) is not bytes:
                raise ProductApiError(ApiErrorCode.RESPONSE_INVALID)
            if len(raw) > maximum_response_bytes:
                raise ProductApiError(ApiErrorCode.RESPONSE_TOO_LARGE)
            try:
                document = json.loads(raw.decode("utf-8", errors="strict"))
            except (UnicodeError, json.JSONDecodeError, RecursionError):
                raise ProductApiError(ApiErrorCode.RESPONSE_INVALID) from None
            if type(document) is not dict:
                raise ProductApiError(ApiErrorCode.RESPONSE_INVALID)
            return document
        finally:
            response.close()

    def download_to_file(
        self,
        path: str,
        destination: str | os.PathLike[str],
        *,
        maximum_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> ApiDownloadReceipt:
        if type(maximum_bytes) is not int or maximum_bytes <= 0:
            raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
        target = Path(destination)
        if not target.is_absolute() or target.exists() or target.is_symlink():
            raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
        parent = target.parent
        if not parent.is_dir() or parent.is_symlink():
            raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
        request_headers = _validate_headers(headers)
        request_headers.setdefault("Accept", "application/octet-stream")
        response = self._open(
            method="GET",
            path=path,
            headers=request_headers,
            body=None,
        )
        descriptor: int | None = None
        byte_size = 0
        digest = hashlib.sha256()
        try:
            declared = _header_value(response.headers, "Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError:
                    raise ProductApiError(ApiErrorCode.RESPONSE_INVALID) from None
                if declared_size < 0 or declared_size > maximum_bytes:
                    raise ProductApiError(ApiErrorCode.RESPONSE_TOO_LARGE)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o600)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ProductApiError(ApiErrorCode.DOWNLOAD_FAILED)
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                descriptor = None
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if type(chunk) is not bytes:
                        raise ProductApiError(ApiErrorCode.RESPONSE_INVALID)
                    if not chunk:
                        break
                    byte_size += len(chunk)
                    if byte_size > maximum_bytes:
                        raise ProductApiError(ApiErrorCode.RESPONSE_TOO_LARGE)
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            return ApiDownloadReceipt(target, byte_size, digest.hexdigest())
        except ProductApiError:
            target.unlink(missing_ok=True)
            raise
        except Exception:
            target.unlink(missing_ok=True)
            raise ProductApiError(ApiErrorCode.DOWNLOAD_FAILED) from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            response.close()

    def request_multipart_json(
        self,
        path: str,
        *,
        fields: dict[str, str],
        file_field: str,
        filename: str,
        content_type: str,
        content: bytes,
        headers: dict[str, str] | None = None,
        maximum_response_bytes: int = DEFAULT_MAX_JSON_BYTES,
    ) -> dict[str, object]:
        """Send one bounded in-memory evidence file and parse a JSON object."""

        import re
        import secrets

        token_re = re.compile(r"[A-Za-z0-9._-]{1,128}")
        if (
            type(fields) is not dict
            or not fields
            or len(fields) > 16
            or type(file_field) is not str
            or token_re.fullmatch(file_field) is None
            or type(filename) is not str
            or token_re.fullmatch(filename) is None
            or type(content_type) is not str
            or content_type not in {"image/png", "image/jpeg", "image/webp"}
            or type(content) is not bytes
            or not 1 <= len(content) <= MAX_MULTIPART_FILE_BYTES
            or type(maximum_response_bytes) is not int
            or not 1 <= maximum_response_bytes <= DEFAULT_MAX_JSON_BYTES
        ):
            raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
        normalized_fields: list[tuple[str, str]] = []
        for name, value in fields.items():
            if (
                type(name) is not str
                or token_re.fullmatch(name) is None
                or type(value) is not str
                or not value
                or len(value.encode("utf-8", errors="strict")) > 8192
                or any(character in value for character in "\x00\r\n")
            ):
                raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
            normalized_fields.append((name, value))
        boundary = "jarvis-" + secrets.token_hex(24)
        boundary_bytes = boundary.encode("ascii")
        if boundary_bytes in content:
            raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
        parts: list[bytes] = []
        for name, value in sorted(normalized_fields):
            parts.extend(
                (
                    b"--" + boundary_bytes + b"\r\n",
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                        "ascii"
                    ),
                    value.encode("utf-8"),
                    b"\r\n",
                )
            )
        parts.extend(
            (
                b"--" + boundary_bytes + b"\r\n",
                (
                    f'Content-Disposition: form-data; name="{file_field}"; '
                    f'filename="{filename}"\r\n'
                ).encode("ascii"),
                f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                content,
                b"\r\n--" + boundary_bytes + b"--\r\n",
            )
        )
        body = b"".join(parts)
        if len(body) > MAX_MULTIPART_FILE_BYTES + (64 * 1024):
            raise ProductApiError(ApiErrorCode.INVALID_REQUEST)
        request_headers = _validate_headers(headers)
        request_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        request_headers.setdefault("Accept", "application/json")
        response = self._open(
            method="POST",
            path=path,
            headers=request_headers,
            body=body,
        )
        try:
            media_type = (
                _header_value(response.headers, "Content-Type") or ""
            ).partition(";")[0].strip().casefold()
            if media_type not in _JSON_CONTENT_TYPES:
                raise ProductApiError(ApiErrorCode.RESPONSE_INVALID)
            raw = response.read(maximum_response_bytes + 1)
            if type(raw) is not bytes or len(raw) > maximum_response_bytes:
                raise ProductApiError(ApiErrorCode.RESPONSE_TOO_LARGE)
            try:
                document = json.loads(raw.decode("utf-8", errors="strict"))
            except (UnicodeError, json.JSONDecodeError, RecursionError):
                raise ProductApiError(ApiErrorCode.RESPONSE_INVALID) from None
            if type(document) is not dict:
                raise ProductApiError(ApiErrorCode.RESPONSE_INVALID)
            return document
        finally:
            response.close()


__all__ = [
    "DEFAULT_MAX_JSON_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "MAX_MULTIPART_FILE_BYTES",
    "ApiDownloadReceipt",
    "ApiErrorCode",
    "ApiTransport",
    "ApiTransportResponse",
    "ProductApiClient",
    "ProductApiError",
    "UrllibApiTransport",
]
