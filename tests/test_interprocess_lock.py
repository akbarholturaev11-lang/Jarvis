from __future__ import annotations

import tempfile
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from core.interprocess_lock import (
    InterProcessFileLock,
    InterProcessLockNotAvailable,
    InterProcessLockTimeout,
)


class InterProcessFileLockTests(unittest.TestCase):
    _CHILD_PROBE = (
        "import sys\n"
        "from core.interprocess_lock import InterProcessFileLock, InterProcessLockTimeout\n"
        "try:\n"
        "    with InterProcessFileLock(sys.argv[1], timeout_seconds=0.1).acquire():\n"
        "        pass\n"
        "except InterProcessLockTimeout:\n"
        "    raise SystemExit(7)\n"
    )

    def test_second_descriptor_times_out_until_first_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "state" / "identity.lock"
            first = InterProcessFileLock(path, timeout_seconds=1)
            second = InterProcessFileLock(path, timeout_seconds=0.1)

            with first.acquire():
                with self.assertRaises(InterProcessLockTimeout):
                    with second.acquire():
                        pass
            with second.acquire():
                self.assertTrue(path.is_file())

    def test_threads_are_serialized_by_same_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "identity.lock"
            entered = threading.Event()
            release = threading.Event()

            def holder() -> None:
                with InterProcessFileLock(path).acquire():
                    entered.set()
                    release.wait(timeout=2)

            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(entered.wait(timeout=1))
            try:
                with self.assertRaises(InterProcessLockTimeout):
                    with InterProcessFileLock(path, timeout_seconds=0.1).acquire():
                        pass
            finally:
                release.set()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_separate_process_cannot_enter_while_lock_is_held(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "identity.lock"
            with InterProcessFileLock(path).acquire():
                blocked = subprocess.run(
                    [sys.executable, "-c", self._CHILD_PROBE, str(path)],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
            released = subprocess.run(
                [sys.executable, "-c", self._CHILD_PROBE, str(path)],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )

        self.assertEqual(blocked.returncode, 7)
        self.assertEqual(released.returncode, 0)

    def test_unknown_platform_and_relative_path_fail_honestly(self):
        with self.assertRaises(ValueError):
            InterProcessFileLock("relative.lock")
        with tempfile.TemporaryDirectory() as temp:
            lock = InterProcessFileLock(Path(temp).resolve() / "x.lock", system="Plan9")
            with self.assertRaises(InterProcessLockNotAvailable):
                with lock.acquire():
                    pass

    def test_symlink_lock_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / "target"
            target.write_text("x", encoding="utf-8")
            link = root / "identity.lock"
            try:
                link.symlink_to(target)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(InterProcessLockNotAvailable):
                with InterProcessFileLock(link).acquire():
                    pass

    @unittest.skipUnless(os.name == "posix", "POSIX dir-fd hardening")
    def test_intermediate_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            real = root / "real"
            real.mkdir()
            link = root / "linked"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(InterProcessLockNotAvailable):
                with InterProcessFileLock(link / "identity.lock").acquire():
                    pass

    @unittest.skipUnless(os.name == "posix", "POSIX link-count hardening")
    def test_hard_link_lock_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / "target.lock"
            target.write_text("", encoding="utf-8")
            linked = root / "identity.lock"
            os.link(target, linked)
            with self.assertRaises(InterProcessLockNotAvailable):
                with InterProcessFileLock(linked).acquire():
                    pass


if __name__ == "__main__":
    unittest.main()
