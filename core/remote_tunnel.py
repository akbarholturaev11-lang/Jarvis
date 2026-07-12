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
        origin_https: bool = False,
    ) -> None:
        self._port      = int(port)
        self._mode      = (mode or "quick").strip()
        self._hostname  = (hostname or "").strip()
        self._origin_https = bool(origin_https)
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
        # The local dashboard serves HTTPS with a self-signed cert whenever certs
        # exist, so an http origin would 502. Point cloudflared at the real origin
        # scheme; Cloudflare terminates real TLS at the edge, so origin cert
        # verification is skipped for localhost only.
        scheme = "https" if self._origin_https else "http"
        cmd = [
            exe, "tunnel", "--no-autoupdate",
            "--url", f"{scheme}://localhost:{self._port}",
        ]
        if self._origin_https:
            cmd.append("--no-tls-verify")
        return cmd

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


# ── Tailscale Funnel provider ─────────────────────────────────────────────────
# Exposes the local dashboard on a STABLE public URL (https://<host>.<tailnet>.ts.net)
# with real TLS and no domain of your own. The Funnel config lives in tailscaled, so
# the URL survives Jarvis restarts and reboots. Honest by construction: if tailscale
# is missing or not logged in, start() reports not_installed / failed and never fakes
# a URL. NOTE: the machine cannot reach its own Funnel URL (MagicDNS resolves it to
# the internal tailnet IP) — verify from an off-tailnet device (e.g. phone on cellular).

def tailscale_path() -> str:
    """Return the tailscale CLI path, or '' if not found."""
    import os
    found = shutil.which("tailscale")
    if found:
        return found
    for p in (
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/opt/homebrew/bin/tailscale",
        "/usr/local/bin/tailscale",
    ):
        if os.path.exists(p):
            return p
    return ""


def tailscale_install_hint() -> str:
    return ("Install Tailscale (https://tailscale.com/download), log in, and enable "
            "Funnel")


class TailscaleFunnel:
    STATUS_STOPPED       = "stopped"
    STATUS_STARTING      = "starting"
    STATUS_ACTIVE        = "active"
    STATUS_NOT_INSTALLED = "not_installed"
    STATUS_FAILED        = "failed"

    def __init__(
        self,
        port: int = 8000,
        origin_https: bool = False,
        on_url=None,
        on_status=None,
    ) -> None:
        self._port         = int(port)
        self._origin_https = bool(origin_https)
        self._on_url       = on_url
        self._on_status    = on_status
        self._public_url: str | None = None
        self._status = self.STATUS_STOPPED

    @property
    def public_url(self) -> str | None:
        return self._public_url

    @property
    def status(self) -> str:
        return self._status

    def _tailscale(self, args: list[str], timeout: float = 30.0):
        exe = tailscale_path()
        if not exe:
            return None
        try:
            return subprocess.run(
                [exe, *args], capture_output=True, text=True, timeout=timeout
            )
        except Exception:
            return None

    def _self_dnsname(self) -> str:
        r = self._tailscale(["status", "--json"], timeout=15)
        if not r or r.returncode != 0:
            return ""
        try:
            import json
            data = json.loads(r.stdout)
            return str((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
        except Exception:
            return ""

    def start(self) -> tuple[str, str]:
        if not tailscale_path():
            self._set_status(self.STATUS_NOT_INSTALLED, tailscale_install_hint())
            return self.STATUS_NOT_INSTALLED, tailscale_install_hint()
        host = self._self_dnsname()
        if not host:
            detail = "Tailscale is not logged in (run: tailscale up)."
            self._set_status(self.STATUS_FAILED, detail)
            return self.STATUS_FAILED, detail
        scheme = "https+insecure" if self._origin_https else "http"
        target = f"{scheme}://localhost:{self._port}"
        r = self._tailscale(["funnel", "--bg", target], timeout=30)
        if r is not None and r.returncode != 0:
            err = (r.stderr or "").strip()
            if "already" not in err.lower():  # idempotent: already-serving is fine
                self._set_status(self.STATUS_FAILED, err or "funnel start failed")
                return self.STATUS_FAILED, err or "funnel start failed"
        url = f"https://{host}"
        self._publish_url(url)
        return self.STATUS_ACTIVE, url

    def stop(self) -> None:
        self._tailscale(["funnel", "reset"], timeout=15)
        self._public_url = None
        self._set_status(self.STATUS_STOPPED, "stopped")

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
