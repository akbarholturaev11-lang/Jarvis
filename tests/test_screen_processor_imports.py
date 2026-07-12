from __future__ import annotations

import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch


class ScreenProcessorImportTests(unittest.TestCase):
    def test_module_import_does_not_eagerly_load_opencv(self):
        probe = (
            "import sys; "
            "import actions.screen_processor; "
            "raise SystemExit(1 if 'cv2' in sys.modules else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    def test_lazy_opencv_import_is_serialized_across_threads(self):
        import actions.screen_processor as screen_processor

        original_module = screen_processor._cv2
        original_attempted = screen_processor._cv2_import_attempted
        sentinel = object()
        call_count = 0
        count_lock = threading.Lock()
        barrier = threading.Barrier(6)
        results = []

        def fake_import(name):
            nonlocal call_count
            self.assertEqual(name, "cv2")
            with count_lock:
                call_count += 1
            time.sleep(0.05)
            return sentinel

        def worker():
            barrier.wait(timeout=5)
            results.append(screen_processor._cv2_module())

        try:
            screen_processor._cv2 = None
            screen_processor._cv2_import_attempted = False
            with patch.object(
                screen_processor.importlib,
                "import_module",
                side_effect=fake_import,
            ):
                threads = [threading.Thread(target=worker) for _ in range(6)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
        finally:
            screen_processor._cv2 = original_module
            screen_processor._cv2_import_attempted = original_attempted

        self.assertEqual(call_count, 1)
        self.assertEqual(results, [sentinel] * 6)


if __name__ == "__main__":
    unittest.main()
