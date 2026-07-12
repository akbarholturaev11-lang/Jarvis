from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import uuid
import zlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from PIL import Image, PngImagePlugin, features

from product_backend.private_storage import (
    MAX_PAYMENT_SCREENSHOT_PIXELS,
    LocalPrivateObjectStore,
    PrivateStorageIntegrityError,
    PrivateStorageNotAvailableError,
    PrivateStorageValidationError,
)


STORE_TIME = datetime(2026, 7, 13, 1, 2, 3, tzinfo=timezone.utc)


def image_bytes(
    image_format: str,
    *,
    size: tuple[int, int] = (12, 8),
    color: tuple[int, int, int, int] = (20, 80, 160, 255),
    metadata: bool = False,
) -> bytes:
    mode = "RGBA" if image_format in ("PNG", "WEBP") else "RGB"
    image = Image.new(mode, size, color if mode == "RGBA" else color[:3])
    output = io.BytesIO()
    kwargs: dict[str, object] = {}
    if image_format == "PNG" and metadata:
        png_info = PngImagePlugin.PngInfo()
        png_info.add_text("private-note", "remove-this-secret-metadata")
        kwargs["pnginfo"] = png_info
    elif image_format == "JPEG" and metadata:
        exif = Image.Exif()
        exif[0x010E] = "remove-this-secret-metadata"
        kwargs["exif"] = exif
    if image_format == "WEBP":
        kwargs["lossless"] = True
    image.save(output, format=image_format, **kwargs)
    image.close()
    return output.getvalue()


def animated_png_bytes() -> bytes:
    first = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
    second = Image.new("RGBA", (4, 4), (0, 255, 0, 255))
    output = io.BytesIO()
    first.save(
        output,
        format="PNG",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )
    first.close()
    second.close()
    return output.getvalue()


def low_quality_noisy_jpeg() -> bytes:
    size = (256, 256)
    pixels = bytes(
        ((index * 37) + (index // 97) * 13) % 256
        for index in range(size[0] * size[1] * 3)
    )
    image = Image.frombytes("RGB", size, pixels)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=5, optimize=True)
    image.close()
    return output.getvalue()


def png_with_declared_dimensions(width: int, height: int) -> bytes:
    """Change only IHDR dimensions and CRC; storage must reject before decode."""

    content = bytearray(image_bytes("PNG", size=(1, 1)))
    if content[12:16] != b"IHDR":
        raise AssertionError("unexpected Pillow PNG layout")
    content[16:20] = width.to_bytes(4, "big")
    content[20:24] = height.to_bytes(4, "big")
    content[29:33] = zlib.crc32(content[12:29]).to_bytes(4, "big")
    return bytes(content)


@unittest.skipUnless(os.name == "posix", "secure local adapter is POSIX-only")
class PosixLocalPrivateObjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.temp_root = Path(os.path.realpath(self.temp.name))
        self.root = self.temp_root / "private-objects"
        self.store = LocalPrivateObjectStore(self.root)
        self.png = image_bytes("PNG")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_constructor_is_read_only_and_ensure_pins_private_root(self) -> None:
        self.assertFalse(self.root.exists())
        with self.assertRaises(PrivateStorageNotAvailableError):
            self.store.store_payment_screenshot(
                self.png, content_type="image/png"
            )
        self.store.ensure()
        self.assertTrue(self.root.is_dir())
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_real_png_jpeg_and_webp_are_sanitized_private_and_valid(self) -> None:
        self.store.ensure()
        cases = (("PNG", "image/png"), ("JPEG", "image/jpeg"))
        if features.check("webp"):
            cases += (("WEBP", "image/webp"),)
        for image_format, content_type in cases:
            with self.subTest(content_type=content_type):
                original = image_bytes(image_format, metadata=True)
                metadata = self.store.store_payment_screenshot(
                    original,
                    content_type=content_type,
                    now=STORE_TIME,
                )
                stored = self.store.read_private_object(metadata)
                self.assertNotIn(b"remove-this-secret-metadata", stored)
                self.assertEqual(metadata.byte_size, len(stored))
                self.assertEqual(metadata.sha256, hashlib.sha256(stored).hexdigest())
                self.assertNotIn("://", metadata.storage_key)
                with Image.open(io.BytesIO(stored)) as decoded:
                    decoded.load()
                    self.assertEqual(decoded.format, image_format)
                    self.assertEqual(decoded.size, (12, 8))
                    self.assertEqual(getattr(decoded, "n_frames", 1), 1)
                    self.assertNotIn("private-note", decoded.info)
                    self.assertFalse(decoded.getexif())
                path = self.root / metadata.storage_key
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.stat().st_nlink, 1)
                for directory in (
                    self.root / "payments",
                    self.root / "payments" / "2026",
                    self.root / "payments" / "2026" / "07",
                ):
                    self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertFalse(any(self.root.rglob(".tmp-*")))

    def test_compensating_discard_deletes_only_verified_exact_object(self) -> None:
        self.store.ensure()
        metadata = self.store.store_payment_screenshot(
            self.png, content_type="image/png", now=STORE_TIME
        )
        path = self.root / metadata.storage_key
        self.assertTrue(path.is_file())

        with self.assertRaises(PrivateStorageIntegrityError):
            self.store.discard_payment_screenshot(
                replace(metadata, sha256="0" * 64)
            )
        self.assertTrue(path.is_file())

        self.store.discard_payment_screenshot(metadata)
        self.assertFalse(path.exists())
        self.store.discard_payment_screenshot(metadata)

    def test_malformed_header_only_polyglot_truncated_and_animated_rejected(self) -> None:
        self.store.ensure()
        invalid = (
            (b"not-an-image", "image/png"),
            (b"\x89PNG\r\n\x1a\n", "image/png"),
            (self.png + b"<script>polyglot</script>", "image/png"),
            (self.png[:-10], "image/png"),
            (animated_png_bytes(), "image/png"),
            (
                image_bytes("JPEG") + b"polyglot-payload\xff\xd9",
                "image/jpeg",
            ),
        )
        for content, content_type in invalid:
            with self.subTest(
                length=len(content), content_type=content_type
            ), self.assertRaises(
                PrivateStorageValidationError
            ):
                self.store.store_payment_screenshot(
                    content, content_type=content_type
                )
        self.assertFalse(any(self.root.rglob(".tmp-*")))

    def test_bomb_pixel_limit_and_oversized_declared_dimensions_rejected(self) -> None:
        self.store.ensure()
        self.assertGreaterEqual(MAX_PAYMENT_SCREENSHOT_PIXELS, 5120 * 2880)
        self.assertLessEqual(MAX_PAYMENT_SCREENSHOT_PIXELS, 4096 * 4096)
        six_pixels = image_bytes("PNG", size=(3, 2))
        with mock.patch(
            "product_backend.private_storage.MAX_PAYMENT_SCREENSHOT_PIXELS", 4
        ):
            with self.assertRaises(PrivateStorageValidationError):
                self.store.store_payment_screenshot(
                    six_pixels, content_type="image/png"
                )
        oversized = png_with_declared_dimensions(8_000, 6_000)
        with self.assertRaises(PrivateStorageValidationError):
            self.store.store_payment_screenshot(
                oversized, content_type="image/png"
            )

    def test_sanitizer_does_not_round_trip_full_pixel_byte_copies(self) -> None:
        self.store.ensure()
        with (
            mock.patch.object(
                Image,
                "frombytes",
                side_effect=AssertionError("full pixel reconstruction is forbidden"),
            ),
            mock.patch.object(
                Image.Image,
                "tobytes",
                side_effect=AssertionError("full pixel byte copy is forbidden"),
            ),
        ):
            metadata = self.store.store_payment_screenshot(
                self.png,
                content_type="image/png",
                now=STORE_TIME,
            )
        stored = self.store.read_private_object(metadata)
        with Image.open(io.BytesIO(stored)) as decoded:
            decoded.load()
            self.assertEqual(decoded.size, (12, 8))

    def test_input_type_mime_and_raw_or_sanitized_size_fail_closed(self) -> None:
        self.store.ensure()
        invalid = (
            (b"", "image/png"),
            (self.png, "image/jpeg"),
            (self.png, "application/octet-stream"),
        )
        for content, content_type in invalid:
            with self.subTest(content_type=content_type), self.assertRaises(
                PrivateStorageValidationError
            ):
                self.store.store_payment_screenshot(
                    content, content_type=content_type
                )
        with self.assertRaises(PrivateStorageValidationError):
            self.store.store_payment_screenshot(  # type: ignore[arg-type]
                bytearray(self.png), content_type="image/png"
            )
        with mock.patch(
            "product_backend.private_storage.MAX_PAYMENT_SCREENSHOT_BYTES",
            len(self.png) - 1,
        ):
            with self.assertRaises(PrivateStorageValidationError):
                self.store.store_payment_screenshot(
                    self.png, content_type="image/png"
                )
        compact_jpeg = low_quality_noisy_jpeg()
        with mock.patch(
            "product_backend.private_storage.MAX_PAYMENT_SCREENSHOT_BYTES",
            len(compact_jpeg) + 100,
        ):
            with self.assertRaises(PrivateStorageValidationError):
                self.store.store_payment_screenshot(
                    compact_jpeg, content_type="image/jpeg"
                )

    def test_intermediate_symlink_cannot_escape_trusted_root(self) -> None:
        self.store.ensure()
        outside = self.temp_root / "outside"
        outside.mkdir()
        (self.root / "payments").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(PrivateStorageIntegrityError):
            self.store.store_payment_screenshot(
                self.png,
                content_type="image/png",
                now=STORE_TIME,
            )
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_key_digest_mode_and_leaf_symlink_are_rejected(self) -> None:
        self.store.ensure()
        metadata = self.store.store_payment_screenshot(
            self.png, content_type="image/png"
        )
        with self.assertRaises(PrivateStorageValidationError):
            self.store.read_private_object(
                replace(metadata, storage_key="../outside.png")
            )
        with self.assertRaises(PrivateStorageValidationError):
            self.store.read_private_object(
                replace(metadata, storage_key="https://invalid/evidence.png")
            )
        with self.assertRaises(PrivateStorageIntegrityError):
            self.store.read_private_object(replace(metadata, sha256="0" * 64))

        path = self.root / metadata.storage_key
        path.chmod(0o644)
        with self.assertRaises(PrivateStorageIntegrityError):
            self.store.read_private_object(metadata)
        path.chmod(0o600)
        target = path.with_name("target.png")
        target.write_bytes(self.store.read_private_object(metadata))
        target.chmod(0o600)
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(PrivateStorageIntegrityError):
            self.store.read_private_object(metadata)

    def test_atomic_final_collision_never_overwrites_existing_object(self) -> None:
        self.store.ensure()
        fixed_uuid = uuid.UUID(int=1)
        with mock.patch(
            "product_backend.private_storage.uuid.uuid4",
            return_value=fixed_uuid,
        ):
            first = self.store.store_payment_screenshot(
                self.png,
                content_type="image/png",
                now=STORE_TIME,
            )
            original = self.store.read_private_object(first)
            replacement_image = image_bytes(
                "PNG", color=(240, 20, 20, 255)
            )
            with self.assertRaises(PrivateStorageIntegrityError):
                self.store.store_payment_screenshot(
                    replacement_image,
                    content_type="image/png",
                    now=STORE_TIME,
                )
        self.assertEqual(self.store.read_private_object(first), original)
        self.assertFalse(any(self.root.rglob(".tmp-*")))

    def test_link_failure_cleans_temp_and_path_chmod_is_never_used(self) -> None:
        self.store.ensure()
        with (
            mock.patch(
                "product_backend.private_storage.os.link",
                side_effect=OSError("simulated link failure"),
            ),
            mock.patch(
                "product_backend.private_storage.os.chmod",
                side_effect=AssertionError("path chmod must not be called"),
            ),
        ):
            with self.assertRaises(PrivateStorageNotAvailableError):
                self.store.store_payment_screenshot(
                    self.png, content_type="image/png"
                )
        self.assertFalse(any(self.root.rglob(".tmp-*")))
        self.assertFalse(any(path.is_file() for path in self.root.rglob("*")))

    def test_root_symlink_and_root_inode_replacement_fail_closed(self) -> None:
        real_root = self.temp_root / "real-root"
        real_root.mkdir(mode=0o700)
        symlink_root = self.temp_root / "symlink-root"
        symlink_root.symlink_to(real_root, target_is_directory=True)
        with self.assertRaises(PrivateStorageValidationError):
            LocalPrivateObjectStore(symlink_root)

        self.store.ensure()
        old_root = self.temp_root / "old-root"
        self.root.rename(old_root)
        self.root.mkdir(mode=0o700)
        with self.assertRaises(PrivateStorageIntegrityError):
            self.store.store_payment_screenshot(
                self.png, content_type="image/png"
            )


class LocalPrivateObjectStoreAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.temp.name)) / "private-objects"
        self.store = LocalPrivateObjectStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_windows_is_honestly_not_available_without_acl_adapter(self) -> None:
        with mock.patch("product_backend.private_storage.os.name", "nt"):
            with self.assertRaises(PrivateStorageNotAvailableError):
                self.store.ensure()
        self.assertFalse(self.root.exists())

    def test_missing_posix_primitives_or_pillow_is_not_available(self) -> None:
        with mock.patch(
            "product_backend.private_storage._posix_capabilities_available",
            return_value=False,
        ):
            with self.assertRaises(PrivateStorageNotAvailableError):
                self.store.ensure()
        self.assertFalse(self.root.exists())

        with (
            mock.patch("product_backend.private_storage._PIL_IMAGE", None),
            mock.patch("product_backend.private_storage._PIL_IMAGE_FILE", None),
        ):
            with self.assertRaises(PrivateStorageNotAvailableError):
                self.store.ensure()
        self.assertFalse(self.root.exists())

    def test_relative_or_filesystem_root_is_rejected(self) -> None:
        with self.assertRaises(PrivateStorageValidationError):
            LocalPrivateObjectStore("relative/private")
        with self.assertRaises(PrivateStorageValidationError):
            LocalPrivateObjectStore(os.sep)


if __name__ == "__main__":
    unittest.main()
