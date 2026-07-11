"""core/remote_tunnel.py — expose the local dashboard from anywhere via Cloudflare.

Wraps the `cloudflared` binary. Quick mode needs no account: it runs
`cloudflared tunnel --url http://localhost:PORT`, and Cloudflare returns a public
`https://<random>.trycloudflare.com` URL that forwards to the local dashboard, so
the phone can reach JARVIS over mobile data with no port-forwarding. Named mode
uses a stable hostname whose credentials cloudflared keeps in ~/.cloudflared
(outside this repo — no secrets are stored here).

Honest by construction: if cloudflared is not installed, `start()` reports
`not_installed` and never fakes a URL. The public URL is only reported once
cloudflared actually prints it. A daemon thread reads cloudflared's output, and
`stop()` terminates the process cleanly. Cross-platform: cloudflared exists on
macOS, Windows, and Linux; detection and process handling are OS-neutral.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

_TRYCLOUDFLARE_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")


def cloudflared_path() -> str:
    """Return the cloudflared executable path, or '' if not installed."""
    return shutil.which("cloudflared") or ""


def install_hint() -> str:
    """OS-appropriate one-line install hint (bilingual-safe, ASCII)."""
    import sys
    if sys.platform == "darwin":
        return "Install: brew install cloudflared"
    if sys.platform == "win32":
        return "Install: winget install --id Cloudflare.cloudflared"
    return "Install cloudflared from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"


class CloudflareTunnel:
    STATUS_STOPPED       = "stopped"
    STATUS_STARTING      = "starting"
    STATUS_ACTIVE        = "active"
    STATUS_NOT_INSTALLED = "not_installed"
    STATUS_FAILED        = "failed"

    def __init__(
        self,
        port: int = 8000,
        mode: str = "quick",
        hostname: str = "",
        on_url=None,
        on_status=None,
    ) -> None:
        self._port      = int(port)
        self._mode      = (mode or "quick").strip()
        self._hostname  = (hostname or "").strip()
        self._on_url    = on_url        # callback(url: str)
        self._on_status = on_status     # callback(status: str, detail: str)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._public_url: str | None = None
        self._status = self.STATUS_STOPPED

    @property
    def public_url(self) -> str | None:
        return self._public_url

    @property
    def status(self) -> str:
        return self._status

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> tuple[str, str]:
        """Begin the tunnel in a background thread. Returns (status, detail)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._status, "already running"
            if not cloudflared_path():
                self._set_status(self.STATUS_NOT_INSTALLED, install_hint())
                return self.STATUS_NOT_INSTALLED, install_hint()
            self._stop.clear()
            self._public_url = None
            self._set_status(self.STATUS_STARTING, "launching cloudflared")
            self._thread = threading.Thread(
                target=self._run, name="cloudflare-tunnel", daemon=True
            )
            self._thread.start()
            return self.STATUS_STARTING, "starting"

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
            except Exception:
                pass
        self._proc = None
        self._public_url = None
        self._set_status(self.STATUS_STOPPED, "stopped")

    # ── internals ─────────────────────────────────────────────────────────────

    def _build_cmd(self, exe: str) -> list[str]:
        if self._mode == "named" and self._hostname:
            # Ingress/credentials are configured by the user via `cloudflared tunnel`
            # and ~/.cloudflared; we just run the named tunnel.
            return [exe, "tunnel", "run"]
        return [
            exe, "tunnel", "--no-autoupdate",
            "--url", f"http://localhost:{self._port}",
        ]

    def _normalized_hostname(self) -> str:
        h = self._hostname
        if h.startswith("http://") or h.startswith("https://"):
            return h
        return f"https://{h}"

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            exe = cloudflared_path()
            if not exe:
                self._set_status(self.STATUS_NOT_INSTALLED, install_hint())
                return
            try:
                self._proc = subprocess.Popen(
                    self._build_cmd(exe),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except Exception as e:
                self._set_status(self.STATUS_FAILED, f"spawn failed: {e}")
                return

            # Named mode has no trycloudflare URL — its public URL is the hostname.
            if self._mode == "named" and self._hostname:
                self._publish_url(self._normalized_hostname())

            try:
                assert self._proc.stdout is not None
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    m = _TRYCLOUDFLARE_RE.search(line)
                    if m and self._public_url is None:
                        self._publish_url(m.group(0))
            except Exception:
                pass

            # cloudflared exited
            if self._stop.is_set():
                self._set_status(self.STATUS_STOPPED, "stopped")
                return
            self._public_url = None
            self._set_status(self.STATUS_STARTING, "cloudflared exited — restarting")
            waited = 0.0
            while waited < backoff and not self._stop.is_set():
                time.sleep(0.1)
                waited += 0.1
            backoff = min(backoff * 2, 30.0)

    def _publish_url(self, url: str) -> None:
        self._public_url = url
        self._set_status(self.STATUS_ACTIVE, url)
        if self._on_url:
            try:
                self._on_url(url)
            except Exception:
                pass

    def _set_status(self, status: str, detail: str = "") -> None:
        self._status = status
        if self._on_status:
            try:
                self._on_status(status, detail)
            except Exception:
                pass
