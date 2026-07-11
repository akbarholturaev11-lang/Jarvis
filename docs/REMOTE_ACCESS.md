# Remote Access & Mobile Control

How to control JARVIS from your phone — on the local network or from anywhere —
plus sleep resilience, quick-command macros, and the desktop settings window.

## 1. The mobile app (web + PWA)

JARVIS serves a phone dashboard from `dashboard/server.py`. Open it in the phone
browser and **Add to Home Screen** to install it as an app (standalone, own icon —
served via `manifest.webmanifest` + `sw.js`).

- **Pair with PIN or QR:** on the desktop press **Remote Control** (or the gear →
  **Show QR / PIN**). Scan the QR, or type the 6-character PIN on `/login`.
- A paired phone stores a device token and reconnects automatically next time.

## 2. Control from anywhere (Cloudflare Tunnel)

By default the dashboard is LAN-only. To reach it over mobile data / another
network, JARVIS can expose it through a Cloudflare tunnel — no port forwarding.

1. **Install cloudflared** (one time):
   - macOS: `brew install cloudflared`
   - Windows: `winget install --id Cloudflare.cloudflared`
   - Linux: see Cloudflare's downloads page.
2. **Enable it:** desktop gear → **Remote access (anywhere)** ON, or set in
   `config/settings.json`:
   ```json
   "remote_tunnel": { "enabled": true, "provider": "cloudflare", "mode": "quick", "hostname": "" }
   ```
3. On launch (or when toggled on) JARVIS runs `cloudflared tunnel --url
   http://localhost:8000`, waits for the public `https://…trycloudflare.com` URL,
   and points the QR/PIN pairing at it. The URL changes each run (quick mode).
   HTTPS also removes the phone-microphone `chrome://flags` workaround.
4. **Stable URL (optional):** set `"mode": "named"` and a `"hostname"`; configure
   the named tunnel with `cloudflared` — its credentials live in `~/.cloudflared`
   (outside this repo; **no secrets are stored in the project**).

If `cloudflared` is not installed, JARVIS reports it honestly and stays LAN-only —
it never fakes a public URL. The public URL is gated by the one-time PIN, per-token
AES-encrypted commands, and `/login` brute-force rate limiting.

## 3. Sleep resilience

- **Phone never gets kicked out:** if the Mac sleeps (server unreachable), the app
  shows a calm *Reconnecting…* state and retries with backoff. When the Mac wakes,
  it reconnects automatically — via the stored device token if the process
  restarted.
- **Keep the computer awake:** while a phone is connected, JARVIS keeps the machine
  awake so a remote session isn't cut off (macOS `caffeinate`, Windows
  `SetThreadExecutionState`, Linux `systemd-inhibit`). It releases shortly after
  the last phone disconnects. Toggle it in the gear → **Keep computer awake**.
  Note: this prevents *idle* sleep, not a manual lid-close — the auto-reconnect
  above covers that case.

## 4. Quick commands / macros (one tap → several actions)

Above the phone keyboard is a row of command chips. Tap **＋** to build a new
command from JARVIS's own capabilities (screen, media, briefing, open app, search,
reminder, …) — pick the actions, name it, save. One macro can bundle several
actions ("look at my screen and pause music and read stats"). Macros are stored on
the computer (`config/macros.json`, gitignored) and shared with the desktop
settings window. The same builder is in the desktop gear → **Command automation**.

## 5. Desktop settings window

A small **⚙** gear sits in the top-right corner of the JARVIS window. It opens a
settings panel with: remote access on/off, Show QR / PIN, keep-awake on/off,
interface language (RU/EN), paired devices (+ revoke all), connection status/URL,
and command automation (macros).

The desktop UI is native PyQt6; Unlumen UI is used only as an animation/visual
reference there (web-only per `AI_RULES.md`), while the phone app uses its own
CSS/JS animations.
