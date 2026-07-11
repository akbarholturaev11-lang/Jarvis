from __future__ import annotations

import unittest
from unittest import mock

from core.remote_tunnel import (
    CloudflareTunnel,
    _TRYCLOUDFLARE_RE,
    install_hint,
)


class TestRemoteTunnel(unittest.TestCase):
    def test_url_regex_matches_trycloudflare(self):
        line = "2026-07-11 INF |  https://abc-def-123.trycloudflare.com  |"
        m = _TRYCLOUDFLARE_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(0), "https://abc-def-123.trycloudflare.com")

    def test_url_regex_ignores_other_hosts(self):
        self.assertIsNone(_TRYCLOUDFLARE_RE.search("https://example.com/path"))

    def test_not_installed_is_honest(self):
        with mock.patch("core.remote_tunnel.shutil.which", return_value=None):
            tun = CloudflareTunnel(port=8000)
            status, _detail = tun.start()
            self.assertEqual(status, CloudflareTunnel.STATUS_NOT_INSTALLED)
            self.assertIsNone(tun.public_url)
            self.assertEqual(tun.status, CloudflareTunnel.STATUS_NOT_INSTALLED)

    def test_install_hint_nonempty(self):
        self.assertTrue(install_hint())

    def test_named_hostname_normalized(self):
        self.assertEqual(
            CloudflareTunnel(mode="named", hostname="jarvis.example.com")._normalized_hostname(),
            "https://jarvis.example.com",
        )
        self.assertEqual(
            CloudflareTunnel(mode="named", hostname="https://x.example.com")._normalized_hostname(),
            "https://x.example.com",
        )

    def test_quick_cmd_targets_local_port(self):
        cmd = CloudflareTunnel(port=8000)._build_cmd("cloudflared")
        self.assertIn("--url", cmd)
        self.assertIn("http://localhost:8000", cmd)


if __name__ == "__main__":
    unittest.main()
