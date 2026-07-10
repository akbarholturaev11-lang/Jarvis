from __future__ import annotations

import threading
import unittest
import warnings

from core.runtime_warnings import install_runtime_warning_filters

install_runtime_warning_filters()

import sounddevice as sd


class RuntimeWarningFilterTests(unittest.TestCase):
    def test_sounddevice_shape_warning_is_suppressed_in_worker_thread(self):
        unrelated_message = "Unrelated runtime deprecation remains visible"

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            install_runtime_warning_filters()

            def emit_warnings() -> None:
                for _ in range(10):
                    sd._array(memoryview(bytearray(8)), 1, "int16")
                warnings.warn(unrelated_message, DeprecationWarning)

            worker = threading.Thread(target=emit_warnings)
            worker.start()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            messages = [str(item.message) for item in caught]
            self.assertNotIn(
                "Setting the shape on a NumPy array has been deprecated",
                "\n".join(messages),
            )
            self.assertIn(unrelated_message, messages)


if __name__ == "__main__":
    unittest.main()
