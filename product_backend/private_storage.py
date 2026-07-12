"""Private, fail-closed POSIX object storage for payment evidence.

The local adapter deliberately reports ``not_available`` on Windows until a
Windows implementation can enforce private ACLs and reject reparse-point
traversal.  On POSIX, every path component is opened relative to an already
trusted directory descriptor with ``O_NOFOLLOW``; screenshot bytes are decoded
and re-encoded before an atomic no-replace publication.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import stat
import threading
import uuid
import warnings
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from .models import (
    MAX_PAYMENT_SCREENSHOT_BYTES,
    PAYMENT_SCREENSHOT_MIME_TYPES,
    format_utc_timestamp,
    validate_sha256,
    validate_storage_key,
)

try:
    from PIL import Image as _PIL_IMAGE
    from PIL import ImageFile as _PIL_IMAGE_FILE
except ImportError:  # pragma: no cover - exercised through a patched boundary
    _PIL_IMAGE = None
    _PIL_IMAGE_FILE = None


# A 16 MP ceiling covers a 5K desktop screenshot (5120 x 2880) while bounding
# one decoded RGBA image to about 64 MiB.  The sanitizer deliberately processes
# only one normalized/full-decode image at a time below.
MAX_PAYMENT_SCREENSHOT_PIXELS: Final = 16_000_000

_MIME_EXTENSIONS: Final = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
_MIME_FORMATS: Final = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}
_READ_CHUNK_BYTES: Final = 64 * 1024
_PILLOW_LOCK = threading.RLock()


class PrivateStorageError(RuntimeError):
    """Base class for fixed, sanitized private-storage failures."""


class PrivateStorageValidationError(PrivateStorageError, ValueError):
    """Object input or key failed a validation rule."""


class PrivateStorageNotAvailableError(PrivateStorageError):
    """The secure local adapter is unavailable on this platform/filesystem."""


class PrivateStorageIntegrityError(PrivateStorageError):
    """Stored bytes or a path component failed an integrity check."""


@dataclass(frozen=True, slots=True)
class PrivateObjectMetadata:
    """Non-secret metadata persisted by the commerce repository."""

    storage_key: str = field(repr=False)
    sha256: str
    byte_size: int
    content_type: str
    created_at: str


def _posix_capabilities_available() -> bool:
    if os.name != "posix":
        return False
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")):
        return False
    required_dir_fd = (os.open, os.mkdir, os.link, os.unlink)
    if any(function not in os.supports_dir_fd for function in required_dir_fd):
        return False
    if os.link not in os.supports_follow_symlinks:
        return False
    return all(hasattr(os, name) for name in ("fchmod", "fsync", "fstat"))


def _require_secure_backend() -> None:
    if not _posix_capabilities_available():
        raise PrivateStorageNotAvailableError(
            "Secure local private storage is not available on this platform."
        )
    if _PIL_IMAGE is None or _PIL_IMAGE_FILE is None:
        raise PrivateStorageNotAvailableError(
            "Secure image validation is not available."
        )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_open_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _container_has_exact_end(content: bytes, content_type: str) -> bool:
    """Reject bytes appended after a declared image container."""

    if content_type == "image/png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        offset = 8
        first_chunk = True
        while offset + 12 <= len(content):
            length = int.from_bytes(content[offset : offset + 4], "big")
            chunk_type = content[offset + 4 : offset + 8]
            chunk_end = offset + 12 + length
            if chunk_end > len(content):
                return False
            if first_chunk and chunk_type != b"IHDR":
                return False
            first_chunk = False
            if chunk_type == b"IEND":
                return length == 0 and chunk_end == len(content)
            offset = chunk_end
        return False
    if content_type == "image/jpeg":
        return _jpeg_has_exact_end(content)
    if content_type == "image/webp":
        return (
            len(content) >= 12
            and content.startswith(b"RIFF")
            and content[8:12] == b"WEBP"
            and int.from_bytes(content[4:8], "little") + 8 == len(content)
        )
    return False


def _jpeg_has_exact_end(content: bytes) -> bool:
    """Parse JPEG markers/scans and require the first real EOI to end the file."""

    if not content.startswith(b"\xff\xd8"):
        return False
    position = 2
    in_scan = False
    while position < len(content):
        if in_scan:
            marker_start = content.find(b"\xff", position)
            if marker_start < 0:
                return False
            marker_code_at = marker_start + 1
            while (
                marker_code_at < len(content)
                and content[marker_code_at] == 0xFF
            ):
                marker_code_at += 1
            if marker_code_at >= len(content):
                return False
            marker_code = content[marker_code_at]
            if marker_code == 0x00 or 0xD0 <= marker_code <= 0xD7:
                position = marker_code_at + 1
                continue
            position = marker_start
            in_scan = False
            continue

        if content[position] != 0xFF:
            return False
        marker_code_at = position + 1
        while marker_code_at < len(content) and content[marker_code_at] == 0xFF:
            marker_code_at += 1
        if marker_code_at >= len(content):
            return False
        marker_code = content[marker_code_at]
        after_marker = marker_code_at + 1
        if marker_code == 0xD9:
            return after_marker == len(content)
        if marker_code == 0x00:
            return False
        if marker_code in (0x01, 0xD8) or 0xD0 <= marker_code <= 0xD7:
            position = after_marker
            continue
        if after_marker + 2 > len(content):
            return False
        segment_length = int.from_bytes(
            content[after_marker : after_marker + 2], "big"
        )
        if segment_length < 2:
            return False
        segment_end = after_marker + segment_length
        if segment_end > len(content):
            return False
        position = segment_end
        in_scan = marker_code == 0xDA
    return False


def _validate_image_header(image: Any, content_type: str) -> tuple[int, int]:
    if image.format != _MIME_FORMATS[content_type]:
        raise PrivateStorageValidationError(
            "Payment screenshot content does not match its type."
        )
    if getattr(image, "n_frames", 1) != 1 or getattr(
        image, "is_animated", False
    ):
        raise PrivateStorageValidationError(
            "Payment screenshot must contain exactly one image frame."
        )
    width, height = image.size
    if (
        type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
        or width * height > MAX_PAYMENT_SCREENSHOT_PIXELS
    ):
        raise PrivateStorageValidationError(
            "Payment screenshot dimensions are invalid."
        )
    return width, height


def _normalized_image_mode(image: Any, content_type: str) -> str:
    has_alpha = "A" in image.getbands() or (
        content_type != "image/jpeg" and "transparency" in image.info
    )
    return "RGB" if content_type == "image/jpeg" or not has_alpha else "RGBA"


def _decode_normalized_image(
    content: bytes,
    content_type: str,
) -> tuple[str, tuple[int, int], Any]:
    """Fully verify/decode one image and return one metadata-free image."""

    if not _container_has_exact_end(content, content_type):
        raise PrivateStorageValidationError(
            "Payment screenshot container is malformed or has trailing data."
        )
    if _PIL_IMAGE is None or _PIL_IMAGE_FILE is None:
        raise PrivateStorageNotAvailableError(
            "Secure image validation is not available."
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", _PIL_IMAGE.DecompressionBombWarning)
            with _PIL_IMAGE.open(io.BytesIO(content)) as probe:
                _validate_image_header(probe, content_type)
                probe.verify()
            with _PIL_IMAGE.open(io.BytesIO(content)) as decoded:
                size = _validate_image_header(decoded, content_type)
                decoded.load()
                mode = _normalized_image_mode(decoded, content_type)
                return mode, size, decoded.convert(mode)
    except PrivateStorageError:
        raise
    except Exception as exc:
        raise PrivateStorageValidationError(
            "Payment screenshot is malformed or unsafe."
        ) from exc


def _verify_decoded_image(
    content: bytes,
    content_type: str,
) -> tuple[str, tuple[int, int]]:
    """Fully verify a sanitized image without allocating another pixel copy."""

    if not _container_has_exact_end(content, content_type):
        raise PrivateStorageValidationError(
            "Payment screenshot container is malformed or has trailing data."
        )
    if _PIL_IMAGE is None or _PIL_IMAGE_FILE is None:
        raise PrivateStorageNotAvailableError(
            "Secure image validation is not available."
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", _PIL_IMAGE.DecompressionBombWarning)
            with _PIL_IMAGE.open(io.BytesIO(content)) as probe:
                _validate_image_header(probe, content_type)
                probe.verify()
            with _PIL_IMAGE.open(io.BytesIO(content)) as decoded:
                size = _validate_image_header(decoded, content_type)
                decoded.load()
                return _normalized_image_mode(decoded, content_type), size
    except PrivateStorageError:
        raise
    except Exception as exc:
        raise PrivateStorageValidationError(
            "Payment screenshot is malformed or unsafe."
        ) from exc


def _sanitize_image(content: bytes, content_type: str) -> bytes:
    """Decode then re-encode pixels only, stripping metadata and extra bytes."""

    with _PILLOW_LOCK:
        if _PIL_IMAGE_FILE is None or _PIL_IMAGE is None:
            raise PrivateStorageNotAvailableError(
                "Secure image validation is not available."
            )
        previous_truncated_policy = _PIL_IMAGE_FILE.LOAD_TRUNCATED_IMAGES
        _PIL_IMAGE_FILE.LOAD_TRUNCATED_IMAGES = False
        try:
            _PIL_IMAGE.init()
            image_format = _MIME_FORMATS[content_type]
            if (
                image_format not in _PIL_IMAGE.OPEN
                or image_format not in _PIL_IMAGE.SAVE
            ):
                raise PrivateStorageNotAvailableError(
                    "Secure image format support is not available."
                )
            mode, size, clean_image = _decode_normalized_image(
                content, content_type
            )
            with clean_image, io.BytesIO() as output:
                if content_type == "image/png":
                    clean_image.save(output, format="PNG", compress_level=9)
                elif content_type == "image/jpeg":
                    clean_image.save(
                        output,
                        format="JPEG",
                        quality=95,
                        subsampling=0,
                        progressive=False,
                        optimize=False,
                    )
                else:
                    clean_image.save(
                        output,
                        format="WEBP",
                        lossless=True,
                        method=4,
                    )
                sanitized = output.getvalue()
            if not sanitized or len(sanitized) > MAX_PAYMENT_SCREENSHOT_BYTES:
                raise PrivateStorageValidationError(
                    "Sanitized payment screenshot size is invalid."
                )
            sanitized_mode, sanitized_size = _verify_decoded_image(
                sanitized, content_type
            )
            if sanitized_mode != mode or sanitized_size != size:
                raise PrivateStorageIntegrityError(
                    "Sanitized payment screenshot verification failed."
                )
            return sanitized
        except PrivateStorageError:
            raise
        except Exception as exc:
            raise PrivateStorageNotAvailableError(
                "Secure image sanitization is not available."
            ) from exc
        finally:
            _PIL_IMAGE_FILE.LOAD_TRUNCATED_IMAGES = previous_truncated_policy


class LocalPrivateObjectStore:
    """Atomic private POSIX storage rooted at an explicitly ensured directory."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        root_path = Path(root).expanduser()
        if not root_path.is_absolute():
            raise PrivateStorageValidationError(
                "Private storage root must be absolute."
            )
        lexical_root = Path(os.path.abspath(os.fspath(root_path)))
        if os.path.islink(lexical_root):
            raise PrivateStorageValidationError(
                "Private storage root must not be a symbolic link."
            )
        if lexical_root == Path(os.sep):
            raise PrivateStorageValidationError(
                "Private storage root must not be the filesystem root."
            )
        self._root = lexical_root
        self._root_identity: tuple[int, int] | None = None
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    def ensure(self) -> LocalPrivateObjectStore:
        """Explicitly create and pin the trusted private root inode."""

        _require_secure_backend()
        with self._lock:
            root_fd = self._open_root_fd(create=True)
            try:
                os.fchmod(root_fd, 0o700)
                os.fsync(root_fd)
            except OSError as exc:
                raise PrivateStorageNotAvailableError(
                    "Private storage is not available."
                ) from exc
            finally:
                os.close(root_fd)
        return self

    def store_payment_screenshot(
        self,
        content: bytes,
        *,
        content_type: str,
        now: datetime | None = None,
    ) -> PrivateObjectMetadata:
        """Validate, sanitize, and atomically publish one private screenshot."""

        _require_secure_backend()
        if type(content) is not bytes:
            raise PrivateStorageValidationError(
                "Payment screenshot must be bytes."
            )
        if not content or len(content) > MAX_PAYMENT_SCREENSHOT_BYTES:
            raise PrivateStorageValidationError(
                "Payment screenshot size is invalid."
            )
        if content_type not in PAYMENT_SCREENSHOT_MIME_TYPES:
            raise PrivateStorageValidationError(
                "Payment screenshot type is not supported."
            )
        with self._lock:
            if self._root_identity is None:
                raise PrivateStorageNotAvailableError(
                    "Private storage has not been explicitly ensured."
                )
        sanitized = _sanitize_image(content, content_type)

        timestamp = now or datetime.now(timezone.utc)
        created_at = format_utc_timestamp(timestamp)
        normalized_time = timestamp.astimezone(timezone.utc)
        storage_key = (
            f"payments/{normalized_time:%Y/%m}/"
            f"{uuid.uuid4().hex}{_MIME_EXTENSIONS[content_type]}"
        )
        temp_name = f".tmp-{uuid.uuid4().hex}"

        with self._lock:
            parent_fd, final_name = self._open_key_parent(
                storage_key, create=True
            )
            descriptor: int | None = None
            temp_created = False
            published = False
            try:
                descriptor = os.open(
                    temp_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
                temp_created = True
                os.fchmod(descriptor, 0o600)
                view = memoryview(sanitized)
                written = 0
                while written < len(view):
                    count = os.write(descriptor, view[written:])
                    if count <= 0:
                        raise OSError("short private-object write")
                    written += count
                os.fsync(descriptor)
                os.link(
                    temp_name,
                    final_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                published = True
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_created = False
                os.fsync(parent_fd)
            except FileExistsError as exc:
                raise PrivateStorageIntegrityError(
                    "Private object key collision was detected."
                ) from exc
            except PrivateStorageError:
                raise
            except OSError as exc:
                if published:
                    with suppress(OSError):
                        os.unlink(final_name, dir_fd=parent_fd)
                    with suppress(OSError):
                        os.fsync(parent_fd)
                raise PrivateStorageNotAvailableError(
                    "Private storage write failed."
                ) from exc
            finally:
                if descriptor is not None:
                    with suppress(OSError):
                        os.close(descriptor)
                if temp_created:
                    with suppress(OSError):
                        os.unlink(temp_name, dir_fd=parent_fd)
                    with suppress(OSError):
                        os.fsync(parent_fd)
                os.close(parent_fd)

        return PrivateObjectMetadata(
            storage_key=storage_key,
            sha256=hashlib.sha256(sanitized).hexdigest(),
            byte_size=len(sanitized),
            content_type=content_type,
            created_at=created_at,
        )

    def read_private_object(
        self,
        metadata: PrivateObjectMetadata,
        *,
        maximum_bytes: int = MAX_PAYMENT_SCREENSHOT_BYTES,
    ) -> bytes:
        """Open relative to a trusted parent descriptor and re-verify bytes."""

        _require_secure_backend()
        if not isinstance(metadata, PrivateObjectMetadata):
            raise PrivateStorageValidationError(
                "Private object metadata is invalid."
            )
        if type(maximum_bytes) is not int or maximum_bytes <= 0:
            raise PrivateStorageValidationError(
                "Private object read limit is invalid."
            )
        if (
            type(metadata.byte_size) is not int
            or metadata.byte_size <= 0
            or metadata.byte_size > maximum_bytes
            or metadata.byte_size > MAX_PAYMENT_SCREENSHOT_BYTES
        ):
            raise PrivateStorageValidationError(
                "Private object exceeds the read limit."
            )
        if metadata.content_type not in PAYMENT_SCREENSHOT_MIME_TYPES:
            raise PrivateStorageValidationError(
                "Private object type is invalid."
            )
        try:
            expected_digest = validate_sha256(metadata.sha256)
        except ValueError as exc:
            raise PrivateStorageValidationError(
                "Private object digest is invalid."
            ) from exc

        with self._lock:
            parent_fd, leaf_name = self._open_key_parent(
                metadata.storage_key, create=False
            )
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    leaf_name,
                    _file_open_flags(),
                    dir_fd=parent_fd,
                )
                file_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or stat.S_IMODE(file_stat.st_mode) != 0o600
                    or file_stat.st_uid != os.geteuid()
                    or file_stat.st_nlink != 1
                ):
                    raise PrivateStorageIntegrityError(
                        "Private object file is not trusted."
                    )
                if file_stat.st_size != metadata.byte_size:
                    raise PrivateStorageIntegrityError(
                        "Private object size verification failed."
                    )
                chunks: list[bytes] = []
                remaining = metadata.byte_size
                while remaining:
                    chunk = os.read(
                        descriptor, min(_READ_CHUNK_BYTES, remaining)
                    )
                    if not chunk:
                        raise PrivateStorageIntegrityError(
                            "Private object read was incomplete."
                        )
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1):
                    raise PrivateStorageIntegrityError(
                        "Private object grew during verification."
                    )
                if os.fstat(descriptor).st_size != file_stat.st_size:
                    raise PrivateStorageIntegrityError(
                        "Private object changed during verification."
                    )
            except PrivateStorageError:
                raise
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise PrivateStorageIntegrityError(
                        "Private object path is not trusted."
                    ) from exc
                raise PrivateStorageNotAvailableError(
                    "Private object is not available."
                ) from exc
            finally:
                if descriptor is not None:
                    with suppress(OSError):
                        os.close(descriptor)
                os.close(parent_fd)

        content = b"".join(chunks)
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise PrivateStorageIntegrityError(
                "Private object digest verification failed."
            )
        return content

    def discard_payment_screenshot(
        self,
        metadata: PrivateObjectMetadata,
    ) -> None:
        """Delete only the exact verified object after a failed DB transaction."""

        _require_secure_backend()
        if not isinstance(metadata, PrivateObjectMetadata):
            raise PrivateStorageValidationError(
                "Private object metadata is invalid."
            )
        try:
            expected_digest = validate_sha256(metadata.sha256)
        except ValueError as exc:
            raise PrivateStorageValidationError(
                "Private object digest is invalid."
            ) from exc
        with self._lock:
            parent_fd, leaf_name = self._open_key_parent(
                metadata.storage_key, create=False
            )
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    leaf_name,
                    _file_open_flags(),
                    dir_fd=parent_fd,
                )
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_uid != os.geteuid()
                    or opened.st_nlink != 1
                    or opened.st_size != metadata.byte_size
                ):
                    raise PrivateStorageIntegrityError(
                        "Private object file is not trusted."
                    )
                digest = hashlib.sha256()
                remaining = opened.st_size
                while remaining:
                    chunk = os.read(
                        descriptor, min(_READ_CHUNK_BYTES, remaining)
                    )
                    if not chunk:
                        raise PrivateStorageIntegrityError(
                            "Private object read was incomplete."
                        )
                    digest.update(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1) or digest.hexdigest() != expected_digest:
                    raise PrivateStorageIntegrityError(
                        "Private object digest verification failed."
                    )
                named = os.stat(
                    leaf_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                    raise PrivateStorageIntegrityError(
                        "Private object changed before deletion."
                    )
                os.unlink(leaf_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                return
            except PrivateStorageError:
                raise
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise PrivateStorageIntegrityError(
                        "Private object path is not trusted."
                    ) from exc
                raise PrivateStorageNotAvailableError(
                    "Private object deletion failed."
                ) from exc
            finally:
                if descriptor is not None:
                    with suppress(OSError):
                        os.close(descriptor)
                os.close(parent_fd)

    def _open_root_fd(self, *, create: bool) -> int:
        """Walk the absolute root component-by-component without symlinks."""

        _require_secure_backend()
        parts = self._root.parts
        current_fd: int | None = None
        try:
            current_fd = os.open(os.sep, _directory_open_flags())
            for index, component in enumerate(parts[1:], start=1):
                created = False
                if create:
                    try:
                        os.mkdir(component, 0o700, dir_fd=current_fd)
                        created = True
                        os.fsync(current_fd)
                    except FileExistsError:
                        pass
                child_fd = os.open(
                    component, _directory_open_flags(), dir_fd=current_fd
                )
                child_stat = os.fstat(child_fd)
                if not stat.S_ISDIR(child_stat.st_mode):
                    os.close(child_fd)
                    raise PrivateStorageNotAvailableError(
                        "Private storage root is not a trusted directory."
                    )
                if created or (create and index == len(parts) - 1):
                    os.fchmod(child_fd, 0o700)
                    os.fsync(child_fd)
                os.close(current_fd)
                current_fd = child_fd

            root_stat = os.fstat(current_fd)
            if root_stat.st_uid != os.geteuid():
                raise PrivateStorageNotAvailableError(
                    "Private storage root ownership is invalid."
                )
            identity = (root_stat.st_dev, root_stat.st_ino)
            if self._root_identity is None:
                if not create:
                    raise PrivateStorageNotAvailableError(
                        "Private storage has not been explicitly ensured."
                    )
                self._root_identity = identity
            elif identity != self._root_identity:
                raise PrivateStorageIntegrityError(
                    "Private storage root identity changed."
                )
            result = current_fd
            current_fd = None
            return result
        except PrivateStorageError:
            raise
        except OSError as exc:
            raise PrivateStorageNotAvailableError(
                "Private storage root is not available."
            ) from exc
        finally:
            if current_fd is not None:
                with suppress(OSError):
                    os.close(current_fd)

    def _open_key_parent(self, key: str, *, create: bool) -> tuple[int, str]:
        try:
            normalized = validate_storage_key(key, field="storage_key")
        except ValueError as exc:
            raise PrivateStorageValidationError(
                "Private object key is invalid."
            ) from exc
        if "\\" in normalized:
            raise PrivateStorageValidationError(
                "Private object key is invalid."
            )
        parts = normalized.split("/")
        if len(parts) < 2 or any(part in ("", ".", "..") for part in parts):
            raise PrivateStorageValidationError(
                "Private object key is invalid."
            )

        current_fd = self._open_root_fd(create=False)
        try:
            for component in parts[:-1]:
                created = False
                if create:
                    try:
                        os.mkdir(component, 0o700, dir_fd=current_fd)
                        created = True
                        os.fsync(current_fd)
                    except FileExistsError:
                        pass
                try:
                    child_fd = os.open(
                        component,
                        _directory_open_flags(),
                        dir_fd=current_fd,
                    )
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise PrivateStorageIntegrityError(
                            "Private object path is not trusted."
                        ) from exc
                    raise
                child_stat = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or child_stat.st_uid != os.geteuid()
                ):
                    os.close(child_fd)
                    raise PrivateStorageIntegrityError(
                        "Private object path is not trusted."
                    )
                if create or created:
                    os.fchmod(child_fd, 0o700)
                    os.fsync(child_fd)
                os.close(current_fd)
                current_fd = child_fd
            result = current_fd
            current_fd = None
            return result, parts[-1]
        except PrivateStorageError:
            raise
        except OSError as exc:
            raise PrivateStorageNotAvailableError(
                "Private object path is not available."
            ) from exc
        finally:
            if current_fd is not None:
                with suppress(OSError):
                    os.close(current_fd)


__all__ = [
    "MAX_PAYMENT_SCREENSHOT_PIXELS",
    "LocalPrivateObjectStore",
    "PrivateObjectMetadata",
    "PrivateStorageError",
    "PrivateStorageIntegrityError",
    "PrivateStorageNotAvailableError",
    "PrivateStorageValidationError",
]
