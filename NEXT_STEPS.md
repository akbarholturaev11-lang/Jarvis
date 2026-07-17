# NEXT_STEPS.md

Current next steps for MARK XLVIII - AkbarCustom in `~/Desktop/Jarvis`.

## Verified Local Product Foundation

- Exact-version offline entitlement, one-device binding/replacement history,
  manual payment approval, activation, signed update discovery/download, secure
  Gemini credential migration/storage contracts, MFA, and the responsive Admin
  PWA are implemented and locally tested at their documented evidence level.
- The macOS updater transaction is locally tested on synthetic `.app` bundles,
  but frozen production install remains `not_available` without the fixed signed
  helper.
- A real unsigned `JARVIS.app` (624 MB) and
  `JARVIS-0.1.0-build1-macos-arm64.dmg` (240 MB) were built and smoke-tested on
  2026-07-16. They launch without system Python, Terminal or the source `.venv`;
  the ad-hoc signature is rejected by Gatekeeper and is not distributable.
- Stage 8 provides locally tested HTTPS/host/proxy/logging/health/migration and
  deployment interfaces. Secure backup/restore and secret mutation are POSIX
  only; backup requires a stopped service, restore requires a fresh target, and
  manifest hashes are integrity evidence rather than authenticity.
- Stage 9 records 21 `pass`, 9 `not_available`, 0 `fail` across the 30-scenario
  map. It always reports `production_ready=false` and
  `production_verified=false`; local evidence is not production evidence.
- Client config provisioning is complete: `ops.gen_secrets` emits a non-secret
  `client-trust.json` (entitlement + release public keys) and
  `ops.build_client_config` builds the pinned HTTPS `config/product.json` from it,
  validated through the real client loader. The purchase flow's remaining blocker
  is the external VPS + domain + TLS deployment, not missing client-config code.

## Remaining Internal Implementation Gaps

1. Implement and independently audit the final macOS signing/notarization
   executor. The current signing adapter only plans commands,
   `packaging/macos/sign_artifact.sh --execute` mechanically returns
   `not_available`, and CI intentionally builds unsigned artifacts without
   production signing secrets.
2. Implement and audit the fixed signed macOS updater helper and its
   safe-shutdown/privilege protocol. Keep the frozen adapter `not_available`
   until helper signature, notarization and rollback evidence all pass.
3. Add one authenticated product-wide audit projection and a browser-visible
   post-baseline pending-payment notification; these account for E2E scenarios 7
   and 30 remaining unavailable.
4. Add transactional admin-MFA master-key rotation and automated evidence/audit
   retention enforcement. Current rotation tooling generates material and
   guidance; it does not perform every live cutover.
5. Build native/background-push mobile interfaces if required. The responsive
   PWA and visible-online polling are implemented; native iOS/Android and push
   providers remain `not_available`.
6. Deliver Windows and Linux packages plus atomic updater helpers. Their neutral
   contract exists, but the platform adapters honestly return `not_available`.
7. Before multi-instance deployment, replace SQLite/in-memory session, grant and
   rate state with PostgreSQL/shared stores and reviewed private object storage.

## External Operational Blockers

- A registered domain, valid TLS, provisioned server/VPN and production-like
  backup storage.
- Production entitlement/release keys, activation pepper, admin credentials,
  MFA key and payment instructions held outside the repository.
- Apple Developer ID certificate/private key and notarization account/profile.
- A clean Mac plus representative iOS/Android browsers for fresh Keychain save
  and restart, install, activation, offline, payment, MFA, update, forced
  rollback and revoke checks.
- A real production deployment/restart/restore/key-cutover exercise. If Caddy is
  selected, add provider/WAF or separately reviewed rate limiting; stock Caddy
  has none. nginx already has the example edge limit.

## Legal And License Blockers

1. Obtain upstream CC BY-NC commercial permission/relicensing or replace affected
   material with a rights-clean implementation.
2. Select and document a lawful PyQt6 distribution model.
3. Clear the product name, bundle identifier, branding, icons, copy and bundled
   asset rights.

No customer release may be described as ready until these legal gates and all
applicable signing/production verification gates are closed.

## Exact Product Next Order

1. Complete the signing executor and fixed updater helper without embedding any
   credential.
2. Close the unified audit/notification, retention and key-rotation gaps.
3. Add Windows/Linux packaging and decide whether native mobile/push is required.
4. When operators provide external infrastructure and credentials, deploy with
   the nginx path (or add a reviewed Caddy rate limiter), enroll real MFA, run a
   stopped-service backup/fresh-target restore drill, and verify key cutovers.
5. Produce a Developer-ID-signed/notarized/stapled DMG and run the full clean-Mac
   30-scenario exercise, including forced rollback. Only then update
   `production_verified` claims.

## Known Runtime Bug

- Jarvisda Zerno orqali statistika eshitganda ovoz chiqmayapti.

The historical Desktop-vended Qt `UF_HIDDEN` issue was mitigated by moving the
development venv to `~/Library/Application Support/JARVIS/venv` and using a local
`.venv` symlink in the primary workspace. It is not a current known blocker;
keep the launcher preflight mandatory and do not recreate the real venv under
Desktop.

## Separate Personal Runtime Verification

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
- Do not manually edit `config/api_keys.json`; it is migration-only. Use
  `core/credential_service.py` for credential lifecycle changes.
- Do not modify `.venv/`.
