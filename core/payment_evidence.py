"""Cross-platform, metadata-free payment screenshot preparation.

Only sanitized pixels leave this boundary.  POSIX files are opened relative to
trusted directory descriptors with ``O_NOFOLLOW`` and Windows files are opened
through reparse-point-aware native handles.  Paths and original bytes are never
retained in result objects or exception messages.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import ntpath
import os
import re
import stat
import threading
import warnings
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final


try:
    from PIL import Image as _PIL_IMAGE
    from PIL import ImageFile as _PIL_IMAGE_FILE
    from PIL import ImageOps as _PIL_IMAGE_OPS
except ImportError:  # pragma: no cover - exercised through a patched boundary
    _PIL_IMAGE = None
    _PIL_IMAGE_FILE = None
    _PIL_IMAGE_OPS = None


MAX_PAYMENT_EVIDENCE_BYTES: Final = 10 * 1024 * 1024
MAX_PAYMENT_EVIDENCE_PIXELS: Final = 16_000_000
_READ_CHUNK_BYTES: Final = 64 * 1024

_SUFFIX_TO_MIME: Final = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_MIME_TO_FORMAT: Final = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}
_MIME_TO_EXTENSION: Final = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
_PILLOW_LOCK = threading.RLock()


class PaymentEvidenceStatus(StrEnum):
    SUCCESS = "success"
    INVALID = "invalid"
    TOO_LARGE = "too_large"
    NOT_AVAILABLE = "not_available"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SanitizedPaymentEvidence:
    content: bytes = field(repr=False)
    content_type: str
    filename: str
    byte_size: int
    sha256: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.content) is not bytes
            or not self.content
            or len(self.content) > MAX_PAYMENT_EVIDENCE_BYTES
            or self.content_type not in _MIME_TO_FORMAT
            or self.filename != f"payment.{_MIME_TO_EXTENSION[self.content_type]}"
            or type(self.byte_size) is not int
            or self.byte_size != len(self.content)
            or type(self.sha256) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
            or not hashlib.sha256(self.content).hexdigest() == self.sha256
        ):
            raise ValueError("sanitized payment evidence is invalid")

    def __repr__(self) -> str:
        return (
            "SanitizedPaymentEvidence(content=<redacted>, "
            f"content_type={self.content_type!r}, byte_size={self.byte_size!r})"
        )


@dataclass(frozen=True, slots=True)
class PaymentEvidenceResult:
    status: PaymentEvidenceStatus
    evidence: SanitizedPaymentEvidence | None = field(default=None, repr=False)
    message: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if type(self.status) is not PaymentEvidenceStatus:
            raise TypeError("status must be a PaymentEvidenceStatus")
        if (self.status is PaymentEvidenceStatus.SUCCESS) != (
            self.evidence is not None
        ):
            raise ValueError("only successful results may contain evidence")

    @property
    def ok(self) -> bool:
        return self.status is PaymentEvidenceStatus.SUCCESS and self.evidence is not None

    def __repr__(self) -> str:
        authority = "sanitized" if self.evidence is not None else "none"
        return (
            f"PaymentEvidenceResult(status={self.status.value!r}, "
            f"evidence={authority!r})"
        )


class _EvidenceInvalid(ValueError):
    pass


class _EvidenceTooLarge(_EvidenceInvalid):
    pass


class _EvidenceNotAvailable(RuntimeError):
    pass


class _EvidenceIoFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    content: bytes = field(repr=False)
    suffix: str


def _result(
    status: PaymentEvidenceStatus,
    message: str,
    evidence: SanitizedPaymentEvidence | None = None,
) -> PaymentEvidenceResult:
    return PaymentEvidenceResult(status, evidence, message)


def _detected_os_name() -> str:
    return os.name


def prepare_payment_evidence(
    selected_path: str | os.PathLike[str],
) -> PaymentEvidenceResult:
    """Snapshot, validate and sanitize one selected screenshot.

    The returned object contains a generic filename and re-encoded image bytes;
    it never contains the selected path or original metadata.
    """

    try:
        if isinstance(selected_path, bytes):
            raise _EvidenceInvalid("Payment screenshot path is invalid.")
        raw_path = os.fspath(selected_path)
        if type(raw_path) is not str or not raw_path or "\x00" in raw_path:
            raise _EvidenceInvalid("Payment screenshot path is invalid.")
        platform_name = _detected_os_name()
        if platform_name == "posix":
            snapshot = _read_posix_snapshot(raw_path)
        elif platform_name == "nt":
            snapshot = _read_windows_snapshot(raw_path)
        else:
            raise _EvidenceNotAvailable(
                "Secure payment screenshot loading is not available."
            )
        content_type = _SUFFIX_TO_MIME.get(snapshot.suffix.casefold())
        if content_type is None:
            raise _EvidenceInvalid("Payment screenshot type is invalid.")
        sanitized = _sanitize_image(snapshot.content, content_type)
        evidence = SanitizedPaymentEvidence(
            sanitized,
            content_type,
            f"payment.{_MIME_TO_EXTENSION[content_type]}",
            len(sanitized),
            hashlib.sha256(sanitized).hexdigest(),
        )
        return _result(
            PaymentEvidenceStatus.SUCCESS,
            "Payment screenshot was validated and sanitized.",
            evidence,
        )
    except _EvidenceTooLarge:
        return _result(
            PaymentEvidenceStatus.TOO_LARGE,
            "Payment screenshot exceeds the size limit.",
        )
    except _EvidenceInvalid:
        return _result(
            PaymentEvidenceStatus.INVALID,
            "Payment screenshot is invalid or unsafe.",
        )
    except _EvidenceNotAvailable:
        return _result(
            PaymentEvidenceStatus.NOT_AVAILABLE,
            "Secure payment screenshot processing is not available.",
        )
    except (_EvidenceIoFailure, OSError, MemoryError):
        return _result(
            PaymentEvidenceStatus.FAILED,
            "Payment screenshot could not be processed.",
        )
    except Exception:
        return _result(
            PaymentEvidenceStatus.FAILED,
            "Payment screenshot could not be processed.",
        )


def _posix_backend_available() -> bool:
    return (
        os.name == "posix"
        and all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW"))
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )


def _read_posix_snapshot(raw_path: str) -> _FileSnapshot:
    if not _posix_backend_available():
        raise _EvidenceNotAvailable("Secure POSIX file loading is unavailable.")
    if not raw_path.startswith("/"):
        raise _EvidenceInvalid("Payment screenshot path must be absolute.")
    components = raw_path.split("/")
    if any(part in {".", ".."} for part in components):
        raise _EvidenceInvalid("Payment screenshot path is not trusted.")
    parts = [part for part in components if part]
    if not parts:
        raise _EvidenceInvalid("Payment screenshot path is invalid.")

    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    parent_fd: int | None = None
    descriptor: int | None = None
    try:
        parent_fd = os.open("/", directory_flags)
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        leaf_name = parts[-1]
        descriptor = os.open(leaf_name, file_flags, dir_fd=parent_fd)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
        ):
            raise _EvidenceInvalid("Payment screenshot file is not trusted.")
        if before.st_size > MAX_PAYMENT_EVIDENCE_BYTES:
            raise _EvidenceTooLarge("Payment screenshot exceeds the size limit.")

        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                raise _EvidenceIoFailure("Payment screenshot read was incomplete.")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise _EvidenceIoFailure("Payment screenshot changed while reading.")

        after = os.fstat(descriptor)
        named = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, name) != getattr(after, name) for name in stable_fields):
            raise _EvidenceIoFailure("Payment screenshot changed while reading.")
        if (
            not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
            or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
            or named.st_size != before.st_size
            or named.st_mtime_ns != before.st_mtime_ns
            or named.st_ctime_ns != before.st_ctime_ns
        ):
            raise _EvidenceIoFailure("Payment screenshot path changed while reading.")
        return _FileSnapshot(b"".join(chunks), Path(leaf_name).suffix.casefold())
    except _EvidenceInvalid:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _EvidenceInvalid("Payment screenshot path is not trusted.") from None
        raise _EvidenceIoFailure("Payment screenshot file is unavailable.") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


_WIN_GENERIC_READ: Final = 0x80000000
_WIN_FILE_SHARE_READ: Final = 0x00000001
_WIN_FILE_SHARE_WRITE: Final = 0x00000002
_WIN_OPEN_EXISTING: Final = 3
_WIN_FILE_ATTRIBUTE_DIRECTORY: Final = 0x00000010
_WIN_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x00000400
_WIN_FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000
_WIN_FILE_FLAG_OPEN_REPARSE_POINT: Final = 0x00200000
_WIN_FILE_FLAG_SEQUENTIAL_SCAN: Final = 0x08000000


class _WinFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


def _win_info_identity(info: _WinFileInformation) -> tuple[int, ...]:
    return (
        int(info.dwFileAttributes),
        int(info.dwVolumeSerialNumber),
        int(info.nFileSizeHigh),
        int(info.nFileSizeLow),
        int(info.nNumberOfLinks),
        int(info.nFileIndexHigh),
        int(info.nFileIndexLow),
        int(info.ftLastWriteTime.dwHighDateTime),
        int(info.ftLastWriteTime.dwLowDateTime),
    )


def _windows_path_parts(raw_path: str) -> tuple[str, tuple[str, ...]]:
    if raw_path.startswith(("\\\\", "\\?\\", "\\.\\")):
        raise _EvidenceInvalid("Network and device paths are not supported.")
    drive, tail = ntpath.splitdrive(raw_path)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith(("\\", "/")):
        raise _EvidenceInvalid("Payment screenshot path must be absolute.")
    parts = tuple(part for part in re.split(r"[\\/]+", tail) if part)
    if not parts or any(
        part in {".", ".."} or ":" in part or "\x00" in part for part in parts
    ):
        raise _EvidenceInvalid("Payment screenshot path is not trusted.")
    return drive.upper() + "\\", parts


def _read_windows_snapshot(raw_path: str) -> _FileSnapshot:
    loader = getattr(ctypes, "WinDLL", None)
    if os.name != "nt" or loader is None:
        raise _EvidenceNotAvailable("Windows secure file loading is unavailable.")
    root, parts = _windows_path_parts(raw_path)
    try:
        kernel32 = loader("kernel32", use_last_error=True)
    except Exception:
        raise _EvidenceNotAvailable("Windows secure file loading is unavailable.") from None

    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_WinFileInformation),
    ]
    get_info.restype = wintypes.BOOL
    read_file = kernel32.ReadFile
    read_file.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPVOID,
    ]
    read_file.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    invalid_handle = ctypes.c_void_p(-1).value
    handles: list[Any] = []

    def open_handle(path: str, access: int, share: int, flags: int) -> Any:
        handle = create_file(
            path,
            access,
            share,
            None,
            _WIN_OPEN_EXISTING,
            flags,
            None,
        )
        if handle in {None, invalid_handle}:
            raise _EvidenceIoFailure("Windows payment screenshot handle failed.")
        handles.append(handle)
        return handle

    def information(handle: Any) -> _WinFileInformation:
        value = _WinFileInformation()
        if not get_info(handle, ctypes.byref(value)):
            raise _EvidenceIoFailure("Windows payment screenshot information failed.")
        return value

    try:
        prefix = root
        for component in parts[:-1]:
            prefix = ntpath.join(prefix, component)
            parent_handle = open_handle(
                prefix,
                0,
                _WIN_FILE_SHARE_READ | _WIN_FILE_SHARE_WRITE,
                _WIN_FILE_FLAG_BACKUP_SEMANTICS | _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
            )
            parent = information(parent_handle)
            if (
                not parent.dwFileAttributes & _WIN_FILE_ATTRIBUTE_DIRECTORY
                or parent.dwFileAttributes & _WIN_FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise _EvidenceInvalid("Windows payment screenshot path is not trusted.")

        final_path = ntpath.join(prefix, parts[-1])
        descriptor = open_handle(
            final_path,
            _WIN_GENERIC_READ,
            _WIN_FILE_SHARE_READ,
            _WIN_FILE_FLAG_OPEN_REPARSE_POINT | _WIN_FILE_FLAG_SEQUENTIAL_SCAN,
        )
        before = information(descriptor)
        if (
            before.dwFileAttributes & _WIN_FILE_ATTRIBUTE_DIRECTORY
            or before.dwFileAttributes & _WIN_FILE_ATTRIBUTE_REPARSE_POINT
            or before.nNumberOfLinks != 1
        ):
            raise _EvidenceInvalid("Windows payment screenshot file is not trusted.")
        size = (int(before.nFileSizeHigh) << 32) | int(before.nFileSizeLow)
        if size <= 0:
            raise _EvidenceInvalid("Payment screenshot is empty.")
        if size > MAX_PAYMENT_EVIDENCE_BYTES:
            raise _EvidenceTooLarge("Payment screenshot exceeds the size limit.")

        chunks: list[bytes] = []
        remaining = size
        while remaining:
            amount = min(_READ_CHUNK_BYTES, remaining)
            buffer = ctypes.create_string_buffer(amount)
            received = wintypes.DWORD()
            if not read_file(
                descriptor,
                buffer,
                amount,
                ctypes.byref(received),
                None,
            ):
                raise _EvidenceIoFailure("Windows payment screenshot read failed.")
            count = int(received.value)
            if count <= 0:
                raise _EvidenceIoFailure("Windows payment screenshot read was incomplete.")
            chunks.append(buffer.raw[:count])
            remaining -= count
        extra = ctypes.create_string_buffer(1)
        extra_count = wintypes.DWORD()
        if not read_file(
            descriptor,
            extra,
            1,
            ctypes.byref(extra_count),
            None,
        ) or extra_count.value:
            raise _EvidenceIoFailure("Windows payment screenshot changed while reading.")
        after = information(descriptor)
        if _win_info_identity(before) != _win_info_identity(after):
            raise _EvidenceIoFailure("Windows payment screenshot changed while reading.")
        return _FileSnapshot(b"".join(chunks), ntpath.splitext(parts[-1])[1].casefold())
    except (_EvidenceInvalid, _EvidenceNotAvailable, _EvidenceIoFailure):
        raise
    except Exception:
        raise _EvidenceIoFailure("Windows payment screenshot processing failed.") from None
    finally:
        for handle in reversed(handles):
            try:
                close_handle(handle)
            except Exception:
                pass


def _container_has_exact_end(content: bytes, content_type: str) -> bool:
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
            while marker_code_at < len(content) and content[marker_code_at] == 0xFF:
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
        segment_length = int.from_bytes(content[after_marker : after_marker + 2], "big")
        if segment_length < 2:
            return False
        segment_end = after_marker + segment_length
        if segment_end > len(content):
            return False
        position = segment_end
        in_scan = marker_code == 0xDA
    return False


def _validate_image(image: Any, content_type: str) -> tuple[int, int]:
    if image.format != _MIME_TO_FORMAT[content_type]:
        raise _EvidenceInvalid("Payment screenshot content does not match its type.")
    if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
        raise _EvidenceInvalid("Payment screenshot must have one frame.")
    width, height = image.size
    if (
        type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
        or width * height > MAX_PAYMENT_EVIDENCE_PIXELS
    ):
        raise _EvidenceInvalid("Payment screenshot dimensions are invalid.")
    return width, height


def _normalized_mode(image: Any, content_type: str) -> str:
    has_alpha = "A" in image.getbands() or (
        content_type != "image/jpeg" and "transparency" in image.info
    )
    return "RGB" if content_type == "image/jpeg" or not has_alpha else "RGBA"


def _sanitize_image(content: bytes, content_type: str) -> bytes:
    if (
        _PIL_IMAGE is None
        or _PIL_IMAGE_FILE is None
        or _PIL_IMAGE_OPS is None
    ):
        raise _EvidenceNotAvailable("Secure image processing is unavailable.")
    if not _container_has_exact_end(content, content_type):
        raise _EvidenceInvalid("Payment screenshot container is malformed.")

    with _PILLOW_LOCK:
        previous = _PIL_IMAGE_FILE.LOAD_TRUNCATED_IMAGES
        _PIL_IMAGE_FILE.LOAD_TRUNCATED_IMAGES = False
        try:
            _PIL_IMAGE.init()
            image_format = _MIME_TO_FORMAT[content_type]
            if image_format not in _PIL_IMAGE.OPEN or image_format not in _PIL_IMAGE.SAVE:
                raise _EvidenceNotAvailable("Secure image codec is unavailable.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", _PIL_IMAGE.DecompressionBombWarning)
                with _PIL_IMAGE.open(io.BytesIO(content)) as probe:
                    _validate_image(probe, content_type)
                    probe.verify()
                with _PIL_IMAGE.open(io.BytesIO(content)) as decoded:
                    _validate_image(decoded, content_type)
                    decoded.load()
                    oriented = _PIL_IMAGE_OPS.exif_transpose(decoded)
                    try:
                        width, height = oriented.size
                        if width * height > MAX_PAYMENT_EVIDENCE_PIXELS:
                            raise _EvidenceInvalid(
                                "Payment screenshot dimensions are invalid."
                            )
                        mode = _normalized_mode(oriented, content_type)
                        clean = oriented.convert(mode)
                    finally:
                        if oriented is not decoded:
                            oriented.close()
                with clean, io.BytesIO() as output:
                    if content_type == "image/png":
                        clean.save(output, format="PNG", compress_level=9)
                    elif content_type == "image/jpeg":
                        clean.save(
                            output,
                            format="JPEG",
                            quality=95,
                            subsampling=0,
                            progressive=False,
                            optimize=False,
                        )
                    else:
                        clean.save(output, format="WEBP", lossless=True, method=4)
                    sanitized = output.getvalue()
            if not sanitized:
                raise _EvidenceInvalid("Sanitized payment screenshot is empty.")
            if len(sanitized) > MAX_PAYMENT_EVIDENCE_BYTES:
                raise _EvidenceTooLarge("Sanitized payment screenshot is too large.")
            if not _container_has_exact_end(sanitized, content_type):
                raise _EvidenceInvalid("Sanitized payment screenshot is invalid.")
            with _PIL_IMAGE.open(io.BytesIO(sanitized)) as verified:
                _validate_image(verified, content_type)
                verified.load()
                if any(
                    key in verified.info
                    for key in ("exif", "xmp", "XML:com.adobe.xmp", "icc_profile", "comment")
                ):
                    raise _EvidenceInvalid("Payment screenshot metadata was not stripped.")
            return sanitized
        except (_EvidenceInvalid, _EvidenceTooLarge, _EvidenceNotAvailable):
            raise
        except MemoryError:
            raise
        except Exception:
            raise _EvidenceInvalid("Payment screenshot is malformed or unsafe.") from None
        finally:
            _PIL_IMAGE_FILE.LOAD_TRUNCATED_IMAGES = previous


__all__ = [
    "MAX_PAYMENT_EVIDENCE_BYTES",
    "MAX_PAYMENT_EVIDENCE_PIXELS",
    "PaymentEvidenceResult",
    "PaymentEvidenceStatus",
    "SanitizedPaymentEvidence",
    "prepare_payment_evidence",
]
