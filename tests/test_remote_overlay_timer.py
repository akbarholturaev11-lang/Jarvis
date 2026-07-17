"""RemoteKeyOverlay.mark_connected() must stop its QTimer on the Qt thread even
when the phone connects from a background thread (fixes the
'QObject::killTimer: Timers cannot be stopped from another thread' warning)."""

import os
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication
    import ui
    _QT_OK = True
except Exception:  # pragma: no cover - environment without a usable Qt
    _QT_OK = False


@unittest.skipUnless(_QT_OK, "PyQt6/offscreen not available")
class RemoteOverlayTimerThreadSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_mark_connected_from_worker_stops_timer_on_main_thread(self):
        overlay = ui.RemoteKeyOverlay("http://localhost:8000", "ABC123")
        self.addCleanup(overlay.deleteLater)

        stop_threads = []
        fake_timer = mock.MagicMock()
        fake_timer.stop.side_effect = lambda: stop_threads.append(threading.current_thread())
        overlay._ctimer = fake_timer

        # Emit from a worker thread, mimicking the dashboard connection callback.
        worker = threading.Thread(target=overlay.mark_connected)
        worker.start()
        worker.join()

        # The queued slot must NOT have run yet on the worker thread.
        self.assertEqual(stop_threads, [])

        # Pump the Qt event loop on the main thread to deliver the queued signal.
        deadline = time.time() + 2.0
        while not stop_threads and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.01)

        self.assertEqual(len(stop_threads), 1)
        self.assertEqual(stop_threads[0], threading.main_thread())


if __name__ == "__main__":
    unittest.main()
