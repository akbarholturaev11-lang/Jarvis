"""core/power_manager.py — cross-platform keep-awake facade.

While a phone is remotely connected, the computer should not idle-sleep out from
under the session. This routes prevent/allow-sleep through the active platform
adapter (macOS caffeinate, Windows SetThreadExecutionState, Linux systemd-inhibit).

It is honest by construction: if the OS or its tool is unavailable, `acquire()`
returns `(False, reason)` and nothing is faked. The manager is idempotent and
thread-safe; the adapter instance that created a keep-awake token is retained so
`release()` is applied by the same adapter (adapters are otherwise stateless and
re-created per call).
"""

from __future__ import annotations

import threading

from core.environment_discovery import select_platform_adapter


class KeepAwakeManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._adapter = None
        self._token: object | None = None
        self._status = ""

    @property
    def active(self) -> bool:
        return self._token is not None

    @property
    def status(self) -> str:
        return self._status

    def acquire(self, reason: str = "JARVIS remote session") -> tuple[bool, str]:
        """Start keeping the machine awake. Idempotent — a second call is a no-op."""
        with self._lock:
            if self._token is not None:
                return True, self._status or "keep-awake already active"
            adapter = select_platform_adapter()
            try:
                token, status = adapter.prevent_sleep(reason)
            except Exception as e:  # never let a keep-awake failure crash the caller
                self._status = f"keep-awake error: {e}"
                return False, self._status
            self._status = status
            if token is None:
                return False, status  # honest unsupported / failed
            self._adapter = adapter
            self._token = token
            return True, status

    def release(self) -> tuple[bool, str]:
        """Allow the machine to sleep again. Idempotent."""
        with self._lock:
            if self._token is None:
                return True, "keep-awake not active"
            try:
                self._adapter.release_sleep(self._token)
            except Exception:
                pass
            self._adapter = None
            self._token = None
            status = self._status
            self._status = ""
            return True, status
