"""Tests for ChatGPT Atlas support in actions/browser_control.py.

Atlas cannot be driven by Playwright, so Jarvis opens URLs in it through the
macOS URL opener and reports automation actions as unavailable. These tests keep
that routing honest and platform-aware without launching a real browser.
"""

import actions.browser_control as bc


def test_is_atlas_recognizes_aliases():
    for name in ["atlas", "Atlas", "ChatGPT Atlas", "chatgpt-atlas",
                 "openai atlas", " ATLAS "]:
        assert bc.is_atlas(name) is True, name
    for name in ["chrome", "safari", "firefox", "", None, "atlantic"]:
        assert bc.is_atlas(name) is False, name


def test_handle_atlas_unsupported_off_macos(monkeypatch):
    monkeypatch.setattr(bc, "_OS", "Windows")
    out = bc._handle_atlas("go_to", {"url": "youtube.com"})
    assert "macOS" in out
    assert "Bajara olmadim" in out


def test_handle_atlas_not_installed(monkeypatch):
    monkeypatch.setattr(bc, "_OS", "Darwin")
    monkeypatch.setattr(bc, "_atlas_app_path", lambda: None)
    out = bc._handle_atlas("go_to", {"url": "youtube.com"})
    assert "not installed" in out


def test_handle_atlas_go_to_opens_url(monkeypatch):
    monkeypatch.setattr(bc, "_OS", "Darwin")
    monkeypatch.setattr(bc, "_atlas_app_path", lambda: bc.Path("/Applications/ChatGPT Atlas.app"))
    captured = {}

    def _fake_open(url):
        captured["url"] = url
        return True

    monkeypatch.setattr(bc, "_open_url_in_atlas", _fake_open)
    out = bc._handle_atlas("go_to", {"url": "youtube.com"})
    assert captured["url"] == "https://youtube.com"
    assert "Opened in ChatGPT Atlas" in out


def test_handle_atlas_search_builds_engine_url(monkeypatch):
    monkeypatch.setattr(bc, "_OS", "Darwin")
    monkeypatch.setattr(bc, "_atlas_app_path", lambda: bc.Path("/x"))
    captured = {}

    def _fake_open(url):
        captured["url"] = url
        return True

    monkeypatch.setattr(bc, "_open_url_in_atlas", _fake_open)
    out = bc._handle_atlas("search", {"query": "hello world", "engine": "duckduckgo"})
    assert captured["url"] == "https://duckduckgo.com/?q=hello+world"
    assert "Opened in ChatGPT Atlas" in out


def test_handle_atlas_automation_action_is_honest(monkeypatch):
    monkeypatch.setattr(bc, "_OS", "Darwin")
    monkeypatch.setattr(bc, "_atlas_app_path", lambda: bc.Path("/x"))
    out = bc._handle_atlas("click", {"text": "Login"})
    assert "automation is not available" in out
    assert "Aniq tasdiqlay olmadim" in out


def test_handle_atlas_missing_url(monkeypatch):
    monkeypatch.setattr(bc, "_OS", "Darwin")
    monkeypatch.setattr(bc, "_atlas_app_path", lambda: bc.Path("/x"))
    out = bc._handle_atlas("go_to", {"url": ""})
    assert "No URL" in out
    assert "Bajara olmadim" in out


def test_handle_atlas_open_failure_reported(monkeypatch):
    monkeypatch.setattr(bc, "_OS", "Darwin")
    monkeypatch.setattr(bc, "_atlas_app_path", lambda: bc.Path("/x"))
    monkeypatch.setattr(bc, "_open_url_in_atlas", lambda url: False)
    out = bc._handle_atlas("go_to", {"url": "youtube.com"})
    assert "Could not open ChatGPT Atlas" in out
    assert "Bajara olmadim" in out


def test_browser_control_routes_atlas_without_playwright(monkeypatch):
    """Explicit Atlas requests must never create a Playwright session."""
    monkeypatch.setattr(bc, "_OS", "Darwin")
    monkeypatch.setattr(bc, "_atlas_app_path", lambda: bc.Path("/x"))
    captured = {}

    def _fake_open(url):
        captured["url"] = url
        return True

    monkeypatch.setattr(bc, "_open_url_in_atlas", _fake_open)

    def _boom(*a, **k):
        raise AssertionError("registry must not be used for Atlas")

    monkeypatch.setattr(bc._registry, "get", _boom)
    out = bc.browser_control({"action": "go_to", "browser": "ChatGPT Atlas", "url": "openai.com"})
    assert captured["url"] == "https://openai.com"
    assert "Opened in ChatGPT Atlas" in out
