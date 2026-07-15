# NEXT_STEPS.md

Current next steps for MARK XLVIII - AkbarCustom.

## Product Release Gates

The local product foundation is implemented: exact-version offline entitlement,
one-device binding/replacement history, manual payment approval, one-time
activation, signed update discovery/download, bilingual release information,
private payment-instructions config, admin panel, and an unsigned macOS DMG plan.

The following external gates must be cleared before any customer release:

1. Obtain commercial rights for the upstream CC BY-NC code/assets and confirm the
   PyQt6 distribution license model.
2. Confirm cleared product name, icon and final bundle identifier.
3. Supply production HTTPS origin, release public keys, entitlement signing key,
   activation pepper, admin auth material, the owner-only admin MFA master key
   (`JARVIS_ADMIN_MFA_KEY_FILE`, fail-closed and mandatory by default), any
   `JARVIS_TRUSTED_PROXIES` in front of the API, optional
   `JARVIS_ADMIN_ALLOWED_NETWORKS` CIDRs/VPN boundary, and owner-only payment
   instructions. Enroll each admin operator's authenticator before go-live,
   store recovery codes offline, and rotate the bootstrap password through the
   authenticated admin password-change flow.
4. Provide Apple Developer ID signing, hardened-runtime entitlements,
   notarization/stapling and Gatekeeper verification.
5. Implement and audit a signed privileged macOS updater helper; the current app
   only downloads/verifies and honestly reports install `not_available`.
6. Run the pinned PyInstaller build on a controlled macOS build environment and
   complete clean-Mac install, activation, permission, offline, payment, update,
   rollback and uninstall tests. The local smoke-build is currently blocked by
   unavailable external package-download approval, not by a source-tree error.

## Known Bugs

- Jarvisda Zerno orqali statistika eshitganda ovoz chiqmayapti.
- macOS metadata current Desktop `.venv` ichidagi Qt pluginlarga `UF_HIDDEN`
  flagini qayta qo‘ymoqda; bir martalik repair barqaror emas va Cocoa startupni
  bloklaydi.

## Immediate Next Steps

0. Manually verify the new mobile remote-control feature in the full Mac app:
   - With explicit approval, rebuild the venv in a non-hidden runtime directory
     outside Desktop, symlink project `.venv` to it, and confirm the read-only Qt
     preflight remains stable across repeated processes.
   - Double-click `scripts/launch_jarvis.command`; confirm `logs/launcher.log`
     records `Qt runtime preflight: OK` and the app reaches `LISTENING`.
   - Press the corner ⚙ gear → settings window opens with remote on/off, Show QR/PIN,
     keep-awake, language RU/EN, paired devices (revoke), connection status, and
     command automation.
   - Pair a phone via QR/PIN; add the JARVIS PWA to the home screen; confirm it opens
     standalone.
   - `brew install cloudflared`, toggle **Remote access** ON, and confirm the QR shows
     a `trycloudflare.com` URL reachable from mobile data. With cloudflared absent,
     confirm the honest `not_installed` message and LAN-only behavior.
   - Let the Mac idle → confirm keep-awake holds; put it to short sleep → confirm the
     phone shows *Reconnecting…* (not kicked to login) and resumes on wake.
   - Build a macro from capabilities on the phone and in the settings window; confirm
     one tap runs the composed multi-action command and it appears on both surfaces.

0.1 Verify admin MFA (BOSQICH 4) against a real `runtime.py` deployment (not only
   the in-process test server): export the full `JARVIS_*` env including an
   owner-only `JARVIS_ADMIN_MFA_KEY_FILE`, start the backend, and confirm
   password-only login lands on the enrollment screen, the QR activates a real
   authenticator app, recovery-code login works once, an idle session expires,
   `revoke-all` signs the operator out everywhere, and starting the backend
   without the MFA key file fails closed. Confirm password change survives a
   process restart, revokes every existing session, and an excluded client IP
   cannot reach any admin API when a CIDR allowlist is configured.


1. Long-run test Gemini Live reconnect / `APIError 1006` recovery on Mac.
2. Test Mac permissions:
   - Microphone
   - Accessibility
   - Screen Recording
   - Camera
3. Manually verify Universal Device Intelligence:
   - Start with no `config/device_profile.json` and confirm it is created.
   - Ask `qaysi qurilmada ishlayapsan?`
   - Ask `asosiy browser qaysi?`
   - Ask `Telegram bormi?`
   - Ask `musiqani to'xtat`
   - Ask `qurilmani qayta tekshir`
   - Confirm unknown permissions/capabilities are reported honestly.
4. Test core commands:
   - Open Safari
   - Look at my screen
   - Open Telegram
   - Set reminder
   - Web search
   - File processor
5. Review any remaining English-only action/tool result wording that should be promoted into the shared English/Russian UI dictionary.
6. Add Akbar-specific assistant personality/rules.
7. Manually verify UI language switching in the full Mac app with restart: Russian -> English -> Russian.
8. Manually verify v0.3.1 runtime routing in the Mac app:
   - YouTube/media play then `to'xtat` resolves to media pause, not close/settings close.
   - `hali ham o'ynayapti` attaches correction and uses safe fallback without claiming success.
   - message `yubor` follow-ups ask confirmation unless target/chat/delivery are verified.
9. Manually verify Personal Operations Briefing in the full Gemini Live app:
   - Start the app and confirm startup gives Personal Operations Briefing, not generic world news.
   - Say `men uydaman` and confirm the `personal_briefing` route is used.
   - Say `dunyo yangiliklarini ayt` and confirm the existing world-news route is used.
   - Before Zerno setup, ask `Telegram kanalim statistikasi qanday?` and confirm `not_configured` with no numbers.
   - After Zerno setup, repeat it and confirm only API-returned groups/numbers appear.
   - Confirm local project output contains evidence-based `foyda`, `zarar`, and `next_action`.
10. Confirm a fresh runtime terminal is not flooded by the sounddevice NumPy 2.5 shape warning; unrelated warnings must remain visible.
11. Complete the local two-input Zerno setup and full-app check:
   - Run `bash scripts/setup_zerno_stats.sh`.
   - Run `source config/local_env.zsh`.
   - Run `python scripts/check_zerno_stats.py` and confirm `status: connected`.
   - Run `python main.py`, say `men uydaman`, and compare the shown metrics with the Zerno source.
   - If Zerno uses a different auth contract than Bearer GET + JSON, document the official contract before changing the adapter.
12. Keep standalone Telegram/Instagram/Messenger adapters `not_configured` until real supported APIs are provided; Zerno may surface those groups only when its real JSON contains them.
13. Later add custom features.

## Context System Follow-Up

- Keep the markdown knowledge graph in `PROJECT_MAP.md` current when architecture changes.
- Keep `AI_RESOURCES.md` current when important files or risk levels change.
- Keep `PROJECT_MEMORY.md` focused on durable context only.
- Log meaningful implementation changes in `CHANGELOG_AKBAR.md`.

## Not Planned Right Now

- Do not install Graphiti/Gravity-style external dependencies unless Akbar explicitly asks and the benefit is clear.
- Do not refactor runtime logic during context-foundation work.
- Do not change API key config.
- Do not modify `.venv/`.
