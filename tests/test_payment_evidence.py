from __future__ import annotations

import ctypes
import io
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin, features

import core.payment_evidence as payment_evidence
from core.payment_evidence import (
    MAX_PAYMENT_EVIDENCE_BYTES,
    PaymentEvidenceStatus,
    prepare_payment_evidence,
)


PRIVATE_METADATA = "sensitive-payment-metadata-marker"


class _FakeWindowsFunction:
    def __init__(self, callback):
        self.callback = callback
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class _FakeKernel32:
    def __init__(self, content: bytes, *, reparse_parent: bool = False) -> None:
        self.content = content
        self.reparse_parent = reparse_parent
        self.created = []
        self.closed = []
        self.offset = 0
        self.CreateFileW = _FakeWindowsFunction(self._create_file)
        self.GetFileInformationByHandle = _FakeWindowsFunction(self._get_info)
        self.ReadFile = _FakeWindowsFunction(self._read_file)
        self.CloseHandle = _FakeWindowsFunction(self._close_handle)

    def _create_file(self, path, access, share, security, creation, flags, template):
        handle = 100 + len(self.created)
        self.created.append((handle, path, access, share, flags))
        return handle

    def _get_info(self, handle, pointer):
        info = pointer._obj
        # The first two calls are directory handles for this fixed test path.
        is_parent = handle in {100, 101}
        info.dwFileAttributes = (
            payment_evidence._WIN_FILE_ATTRIBUTE_DIRECTORY if is_parent else 0
        )
        if self.reparse_parent and handle == 100:
            info.dwFileAttributes |= payment_evidence._WIN_FILE_ATTRIBUTE_REPARSE_POINT
        info.dwVolumeSerialNumber = 77
        size = 0 if is_parent else len(self.content)
        info.nFileSizeHigh = size >> 32
        info.nFileSizeLow = size & 0xFFFFFFFF
        info.nNumberOfLinks = 1
        info.nFileIndexHigh = 0
        info.nFileIndexLow = handle
        info.ftLastWriteTime.dwHighDateTime = 10
        info.ftLastWriteTime.dwLowDateTime = 20
        return 1

    def _read_file(self, handle, buffer, amount, received_pointer, overlapped):
        chunk = self.content[self.offset : self.offset + amount]
        if chunk:
            ctypes.memmove(buffer, chunk, len(chunk))
        received_pointer._obj.value = len(chunk)
        self.offset += len(chunk)
        return 1

    def _close_handle(self, handle):
        self.closed.append(handle)
        return 1


def _image_bytes(image_format: str, *, metadata: bool = False) -> bytes:
    image = Image.new("RGBA", (16, 12), (12, 120, 210, 180))
    output = io.BytesIO()
    if image_format == "PNG":
        info = PngImagePlugin.PngInfo()
        if metadata:
            info.add_text("Comment", PRIVATE_METADATA)
        image.save(output, format="PNG", pnginfo=info)
    elif image_format == "JPEG":
        exif = Image.Exif()
        if metadata:
            exif[0x010E] = PRIVATE_METADATA
        image.convert("RGB").save(output, format="JPEG", exif=exif)
    elif image_format == "WEBP":
        image.save(output, format="WEBP", lossless=True)
    else:  # pragma: no cover - test helper misuse
        raise AssertionError("unsupported test image format")
    return output.getvalue()


def _oversized_png_header() -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 4_001, 4_000, 8, 6, 0, 0, 0)

    def chunk(kind: bytes, value: bytes) -> bytes:
        return (
            len(value).to_bytes(4, "big")
            + kind
            + value
            + zlib.crc32(kind + value).to_bytes(4, "big")
        )

    return signature + chunk(b"IHDR", ihdr_data) + chunk(b"IEND", b"")


class PaymentEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # macOS exposes /var as a symlink. Resolve the temporary root so the
        # production no-follow parent walk receives a canonical safe path.
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def test_png_jpeg_and_webp_are_reencoded_without_private_metadata(self) -> None:
        cases = [
            ("proof.png", "PNG", "image/png", True),
            ("proof.jpeg", "JPEG", "image/jpeg", True),
        ]
        if features.check("webp"):
            cases.append(("proof.webp", "WEBP", "image/webp", False))

        for filename, image_format, mime, metadata in cases:
            with self.subTest(image_format=image_format):
                original = _image_bytes(image_format, metadata=metadata)
                path = self._write(filename, original)

                result = prepare_payment_evidence(path)

                self.assertTrue(result.ok, result)
                self.assertEqual(result.status, PaymentEvidenceStatus.SUCCESS)
                evidence = result.evidence
                self.assertIsNotNone(evidence)
                self.assertEqual(evidence.content_type, mime)
                self.assertEqual(evidence.byte_size, len(evidence.content))
                self.assertLessEqual(evidence.byte_size, MAX_PAYMENT_EVIDENCE_BYTES)
                self.assertNotIn(PRIVATE_METADATA.encode(), evidence.content)
                self.assertNotIn(str(path), repr(result))
                self.assertNotIn(str(path), repr(evidence))
                self.assertNotIn(evidence.sha256, repr(evidence))
                with Image.open(io.BytesIO(evidence.content)) as decoded:
                    self.assertEqual(decoded.format, image_format)
                    self.assertEqual(getattr(decoded, "n_frames", 1), 1)
                    self.assertNotIn("exif", decoded.info)
                    self.assertNotIn("comment", decoded.info)

    def test_mismatched_corrupt_polyglot_and_animated_images_are_invalid(self) -> None:
        mismatched = self._write("mismatch.jpg", _image_bytes("PNG"))
        corrupt = self._write("corrupt.png", b"not-an-image")
        polyglot = self._write(
            "polyglot.png",
            _image_bytes("PNG") + b"appended-private-payload",
        )
        first = Image.new("RGB", (8, 8), "red")
        second = Image.new("RGB", (8, 8), "blue")
        animated = self.root / "animated.png"
        first.save(
            animated,
            format="PNG",
            save_all=True,
            append_images=[second],
            duration=100,
            loop=0,
        )

        for path in (mismatched, corrupt, polyglot, animated):
            with self.subTest(path=path.name):
                result = prepare_payment_evidence(path)
                self.assertEqual(result.status, PaymentEvidenceStatus.INVALID)
                self.assertFalse(result.ok)
                self.assertIsNone(result.evidence)
                self.assertNotIn(str(path), repr(result))

    def test_raw_size_and_decoded_pixel_budgets_fail_closed(self) -> None:
        oversized = self.root / "oversized.png"
        with oversized.open("wb") as output:
            output.seek(MAX_PAYMENT_EVIDENCE_BYTES)
            output.write(b"x")
        oversized_pixels = self._write("pixel-budget.png", _oversized_png_header())

        self.assertEqual(
            prepare_payment_evidence(oversized).status,
            PaymentEvidenceStatus.TOO_LARGE,
        )
        self.assertEqual(
            prepare_payment_evidence(oversized_pixels).status,
            PaymentEvidenceStatus.INVALID,
        )

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor contract")
    def test_posix_symlink_hardlink_relative_and_mutating_files_are_rejected(self) -> None:
        original = self._write("original.png", _image_bytes("PNG"))
        symbolic = self.root / "symbolic.png"
        symbolic.symlink_to(original)
        self.assertEqual(
            prepare_payment_evidence(symbolic).status,
            PaymentEvidenceStatus.INVALID,
        )

        hard_link = self.root / "hard-link.png"
        os.link(original, hard_link)
        self.assertEqual(
            prepare_payment_evidence(hard_link).status,
            PaymentEvidenceStatus.INVALID,
        )
        hard_link.unlink()
        self.assertEqual(
            prepare_payment_evidence("relative.png").status,
            PaymentEvidenceStatus.INVALID,
        )

        mutable = self._write("mutable.png", _image_bytes("PNG"))
        original_read = payment_evidence.os.read
        changed = False

        def mutate_after_read(descriptor: int, amount: int) -> bytes:
            nonlocal changed
            chunk = original_read(descriptor, amount)
            if chunk and not changed:
                changed = True
                with mutable.open("ab") as output:
                    output.write(b"changed")
            return chunk

        with patch.object(payment_evidence.os, "read", side_effect=mutate_after_read):
            result = prepare_payment_evidence(mutable)
        self.assertEqual(result.status, PaymentEvidenceStatus.FAILED)

    def test_unknown_and_unavailable_platforms_are_honest(self) -> None:
        path = self._write("proof.png", _image_bytes("PNG"))
        with patch.object(payment_evidence, "_detected_os_name", return_value="java"):
            unknown = prepare_payment_evidence(path)
        self.assertEqual(unknown.status, PaymentEvidenceStatus.NOT_AVAILABLE)

        with (
            patch.object(payment_evidence, "_detected_os_name", return_value="nt"),
            patch.object(
                payment_evidence,
                "_read_windows_snapshot",
                side_effect=payment_evidence._EvidenceNotAvailable(
                    "native Windows backend unavailable"
                ),
            ) as windows_reader,
        ):
            windows = prepare_payment_evidence(r"C:\Users\Akbar\proof.png")
        windows_reader.assert_called_once()
        self.assertEqual(windows.status, PaymentEvidenceStatus.NOT_AVAILABLE)

    def test_windows_path_contract_rejects_unc_device_traversal_and_ads(self) -> None:
        root, parts = payment_evidence._windows_path_parts(
            r"C:\Users\Akbar\proof.PNG"
        )
        self.assertEqual(root, "C:\\")
        self.assertEqual(parts, ("Users", "Akbar", "proof.PNG"))
        invalid = (
            r"relative\proof.png",
            r"\\server\share\proof.png",
            r"\\?\C:\proof.png",
            r"C:\Users\..\proof.png",
            r"C:\Users\proof.png:private",
        )
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(
                payment_evidence._EvidenceInvalid
            ):
                payment_evidence._windows_path_parts(path)

    def test_windows_native_contract_holds_reparse_aware_handles(self) -> None:
        content = _image_bytes("PNG")
        kernel32 = _FakeKernel32(content)
        with (
            patch.object(payment_evidence.os, "name", "nt"),
            patch.object(payment_evidence.ctypes, "WinDLL", create=True) as loader,
        ):
            loader.return_value = kernel32
            snapshot = payment_evidence._read_windows_snapshot(
                r"C:\Users\Akbar\proof.png"
            )

        self.assertEqual(snapshot.content, content)
        self.assertEqual(snapshot.suffix, ".png")
        self.assertEqual(len(kernel32.created), 3)
        self.assertTrue(
            all(
                flags & payment_evidence._WIN_FILE_FLAG_OPEN_REPARSE_POINT
                for _handle, _path, _access, _share, flags in kernel32.created
            )
        )
        self.assertEqual(kernel32.closed, [102, 101, 100])

        reparse_kernel = _FakeKernel32(content, reparse_parent=True)
        with (
            patch.object(payment_evidence.os, "name", "nt"),
            patch.object(payment_evidence.ctypes, "WinDLL", create=True) as loader,
        ):
            loader.return_value = reparse_kernel
            with self.assertRaises(payment_evidence._EvidenceInvalid):
                payment_evidence._read_windows_snapshot(
                    r"C:\Users\Akbar\proof.png"
                )
        self.assertEqual(reparse_kernel.closed, [100])

    def test_missing_image_codec_returns_not_available_without_leaking_path(self) -> None:
        path = self._write("proof.png", _image_bytes("PNG"))
        with patch.object(payment_evidence, "_PIL_IMAGE", None):
            result = prepare_payment_evidence(path)
        self.assertEqual(result.status, PaymentEvidenceStatus.NOT_AVAILABLE)
        self.assertNotIn(str(path), repr(result))
        self.assertNotIn(str(path), result.message)


if __name__ == "__main__":
    unittest.main()
