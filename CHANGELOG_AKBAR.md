# CHANGELOG_AKBAR.md

## 2026-07-16 - Hardened production backend deployment + ops tooling (BOSQICH 8)

### Added

- `product_backend/observability.py`: standard-library structured JSON logging
  with secret redaction (`redact_text`/`redact_mapping`), correlation-ID helpers
  (`resolve_request_id`), and a bounded in-process `InMemoryMetrics` counter
  registry with Prometheus text exposition (`NullMetrics` when disabled).
- `product_backend/api_operational.py`: an OUTERMOST ASGI `OperationalMiddleware`
  serving `/healthz` (liveness, host-agnostic), `/readyz` (readiness via a DB
  read probe), and `/metrics` (Bearer-gated, 404 when no token), plus HTTPS
  enforcement (reject or 308-redirect), HSTS, correlation-ID echo, and a JSON
  access log line per request. `OperationalPolicy.from_env` reads
  `JARVIS_REQUIRE_HTTPS`, `JARVIS_HTTPS_REDIRECT`, `JARVIS_HSTS_MAX_AGE`,
  `JARVIS_METRICS_TOKEN`. Forwarded scheme is trusted only from
  `JARVIS_TRUSTED_PROXIES` peers (never by default).
- `product_backend/migrations.py`: read-only schema inspection + fail-closed
  `verify` + standalone commerce forward-migration via the real repository code
  path (commerce is the one versioned DB, `user_version = 4`).
- `ops/` cross-platform tooling (stdlib + cryptography): `gen_secrets`,
  `validate_config` (fail-closed, assembles the real app), `backup`/`restore`
  (online SQLite snapshot + SHA-256 manifest, verified restore), `migrate`,
  `rotate` (session-secret/mfa-key/activation-pepper/entitlement-key/release-key
  with honest overlap + re-enrol side effects), `dev_tls` (self-signed cert +
  uvicorn TLS). POSIX applies `0600`/`0700`; Windows returns an honest `manual`
  NTFS-ACL status, never a faked mode.
- `deploy/`: systemd unit (sandboxed, `ExecStartPre` validation), slim non-root
  Docker image + compose (backend port not published), nginx + Caddy TLS proxy
  configs (HSTS, trusted host, edge rate limit, admin IP allowlist, health
  passthrough, `/metrics` denied publicly), and `env/backend.env.example`.
- `docs/PRODUCTION_DEPLOYMENT.md`: topology, config, HTTPS/forwarded policy,
  operational endpoints, edge+app rate limit, backup/restore, migration,
  payment-evidence + audit retention, key rotation, SQLite single-process
  constraint, PostgreSQL/shared-state multi-instance plan, local TLS dev env,
  cross-platform hosting note.
- Wired the operational layer into `api_app.py` (new keyword args, installed as
  the outermost middleware with a DB-backed readiness probe) and `runtime.py`
  (`OperationalPolicy.from_env`, JSON access logger, metrics registry).

### Why

Stage 8: prepare the admin/backend for a real HTTPS server safely — HTTPS-only,
trusted hosts, explicit trusted-proxy list, no default X-Forwarded trust, health
+ readiness, structured redacted logs, correlation IDs, metrics, backup/restore,
migration, retention, key rotation, admin allowlist/VPN, edge+app rate limits,
fail-closed config validation, secrets outside the repo, documented SQLite
single-process mode + multi-instance migration plan, and a local TLS dev env.

### Verification

- `.venv/bin/python -m py_compile main.py product_backend/*.py ops/*.py` — OK.
- Full suite: `730 passed, 473 subtests` (adds `test_backend_observability`,
  `test_backend_operational`, `test_backend_migrations`, `test_ops_tooling`).
- Local production-like smoke: uvicorn + factory over self-signed TLS on 8443 →
  `/healthz` 200, `/readyz` 200, `/api/releases` 200 with HSTS + `X-Request-ID`;
  plain HTTP with `JARVIS_REQUIRE_HTTPS=true` → probes 200 (exempt),
  `/api/releases` 400 `https is required`; assembled app rejects a foreign Host
  (400) while `/healthz` still answers.

### Constraints kept

- Secrets never enter the repo (`*.key`/`*.pem`/`*.sqlite3`/`.env*` gitignored);
  only `*.example` templates are committed. No commercial-release claim; the
  distribution gates in `PRODUCT_RELEASE_CONTRACT.md` still apply. Backend runtime
  stays POSIX-hardened; Windows hosting is via container, tooling is
  cross-platform with honest status. No entitlement/version model change.

## 2026-07-16 - Validated the local unsigned macOS app + DMG (BOSQICH 7)

### Real build result

- Built a real `JARVIS.app` (624 MB) and `JARVIS-0.1.0-build1-macos-arm64.dmg`
  (240 MB, SHA-256 `fab1c73e…220f`) with the pinned PyInstaller 6.21.0 in an
  isolated build venv (the customer runtime venv was never modified).
- The frozen app launches from its embedded interpreter with no system Python,
  no `.venv`, and no Terminal, reaches the Qt event loop, and shows the product
  **license gate** UI (`ТРЕБУЕТСЯ ЛИЦЕНЗИЯ ПРОДУКТА`, `Версия 0.1.0 · сборка 1`).
  PyQt6 cocoa plugin loads; a copy of the app run from a second location also
  launches cleanly.
- The DMG mounts with `JARVIS.app` + an `/Applications` shortcut; verify-app
  reports the bundled interpreter and **no secret files**; nothing is written
  into the bundle at runtime.

### Honest signing status

- `codesign -dv`: `Signature=adhoc`, `TeamIdentifier=not set` (PyInstaller ad-hoc
  signature, required for Apple Silicon local run — **not** Developer ID).
- `codesign --verify --deep --strict`: valid on disk (ad-hoc is structurally
  valid). `spctl --assess`: **rejected** — not notarized, not distribution ready.

### Fixes found during validation

- `core/platform_adapters/release_base.py`: absolutize the build interpreter
  without following its final symlink, so a virtualenv `bin/python` is preserved
  instead of resolving out to the base interpreter (which broke the build).
- `scripts/release_pipeline.py`: the bundle secret scan now flags `.pem`/`.key`
  only when they contain a PRIVATE KEY block, so legitimate public CA trust
  stores (certifi `cacert.pem`, grpc `roots.pem`) are not false-positives.

### Remaining blockers

- Developer ID signing, notarization, stapling and Gatekeeper acceptance are not
  done (no Apple credentials); the artifact is an unsigned/ad-hoc dev build.
- A clean-Mac `/Applications` install/activation/permission pass is still pending.

### Scope (NOT cross-platform)

- This is the **macOS-only** packaging pipeline. It is not a cross-platform
  packaging deliverable. The desktop client still needs separate Windows
  (self-contained `.exe` + installer) and Linux (AppImage and/or `.deb`)
  distributables; those are explicit later stages (NEXT_STEPS 0.4, 0.5) and their
  `WindowsReleaseAdapter`/`LinuxReleaseAdapter` return honest `not_available`.

## 2026-07-16 - Self-contained macOS app + DMG build/sign/CI pipeline (BOSQICH 7)

### Added

- Added a granular, argv-only local packaging pipeline (`scripts/release_pipeline.py`
  + `packaging/macos/{clean,build_app,build_dmg,generate_manifest,verify_app,
  sign_artifact,smoke_launch,cleanup,make_icns,build_all}.sh`) driving the shared
  `MacOSReleaseAdapter` plan, so a customer needs no Python, Terminal, pip, repo or
  `.venv`.
- Added `packaging/macos/entitlements.plist` (hardened runtime for a frozen
  CPython + PyQt6 app; JIT/unsigned-memory/library-validation + mic/camera/Apple
  Events/network; no App Sandbox).
- Added `core/platform_adapters/release_signing.py`: a side-effect-free Developer
  ID signing + notarization planner using only public env labels
  (`JARVIS_MACOS_SIGN_IDENTITY`/`_TEAM_ID`/`_NOTARY_PROFILE`), nested-code-first
  ordering, `codesign`/`spctl`/`notarytool`/`stapler`, and an honest unsigned
  dev-build status when no credentials exist.
- Added `core/release_build_manifest.py` (non-secret build manifest: identity,
  SHA-256, byte size, signed/notarized/distribution_ready) reusing the shared
  product identity.
- Hardened `packaging/macos/Jarvis.spec` with hidden imports (Google GenAI SDK,
  PyQt6 plugins, cryptography, PIL, cv2, uvicorn), the updater-helper modules
  (`core.macos_update`/`core.installer`/`core.update_startup`), and an optional
  dev-only `JARVIS_APP_ICON`.
- Added `.github/workflows/macos-release.yml`: unit tests → unsigned build/verify/
  upload → secret-gated signing/notarization, with masked secrets and `set +x`.
- Added tests: `test_release_signing`, `test_release_build_manifest`,
  `test_release_bundle_contract`, `test_release_pipeline` (spec/resource/hidden-
  import/manifest/no-secret-files/updater-helper/no-Terminal/no-system-Python).

### Verification

- `py_compile` OK; full suite **681 passed, 473 subtests** (no regressions).
- Scripts exercised end-to-end: `verify_app`/`generate_manifest`/`smoke_launch`/
  `cleanup` pass on a fixture bundle; secret-in-bundle is rejected.

### Blocker (no fake success)

- PyInstaller is **not installed** in this environment, so **no real `JARVIS.app`
  or DMG was built**. The spec, scripts, signing planner, CI and validation tests
  are complete; the freeze must run on a controlled macOS build host with
  PyInstaller and real Developer ID credentials (see NEXT_STEPS 0.3, gates 4/6).

## 2026-07-16 - Verified macOS update and rollback transaction

### Added

- Added strict `jarvis_macos_app_zip_v1` extraction with bounded archive limits,
  exact bundle/version/build identity and safe in-bundle framework symlinks.
- Added a private tree-digested backup, same-volume atomic development `.app`
  swap, fresh-nonce health proof, durable journal recovery and verified rollback.
- Added install-time exact-target entitlement revalidation, an explicit desktop
  Install action, and fail-closed recovery before licensing, Gemini onboarding,
  payment resume or assistant startup.
- Added bilingual English/Russian install, preserve, rollback and blocked-recovery
  states plus adversarial unit/integration coverage.

### Security and release status

- The development updater requires explicit construction and is rejected in a
  frozen runtime. Default macOS, Windows and Linux factories never select it.
- The production macOS boundary assesses a fixed helper path for owner/mode/link,
  Team ID, designated requirement, `codesign`, Gatekeeper and notarization
  evidence, but mutation remains honest `not_available` until the privileged
  shutdown/swap protocol is implemented and independently audited.
- Local synthetic `.app` A-to-B, forced health failure/rollback and interrupted
  recovery pass. Real `/Applications`, signing/notarization and clean-Mac results
  are not claimed.

## 2026-07-15 - Secure Mobile Admin PWA (BOSQICH 5)

### Added and enforced

- The isolated `/admin/` console is now an installable responsive PWA with a
  same-origin manifest, local icon and a versioned service worker that caches
  only an explicit public shell. API, session, evidence, customer, release and
  audit responses are never intercepted or cached.
- Added authenticated, bounded, restart-persistent admin directories for
  accounts, licenses/active devices/exact-version entitlements and all
  draft/published releases with server-controlled price and currency.
- Added visible+online 30-second pending-payment polling with honest in-app
  notifications, offline denial, background privacy shielding/secret cleanup,
  external-navigation blocking and five-target mobile navigation.
- Admin CSP permits only same-origin manifest and worker loading. The ordinary
  remote-control PWA does not advertise Admin Mode; native iOS/Android and push
  delivery remain explicitly `not_available`.

### Verification

- Static/security, API authorization/persistence, EN/RU parity, service-worker
  allowlist, no-browser-storage and ordinary-PWA separation tests were added.
- Real ephemeral HTTPS Chromium smoke passed at 390×844 and 412×915: MFA,
  customer/license/release reads, active service worker with API-free cache,
  secure cookie flags, background cleanup, external navigation denial and
  offline no-data behavior.

## 2026-07-15 - Payment durability and abuse hardening (BOSQICH 3 review)

### Fixed

- A wrong purchase/release context no longer consumes an otherwise valid
  one-time initial-purchase upload grant; the same grant can still be used once
  with its bound context.
- Initial-purchase challenge creation now has a separate client-IP budget, so
  attacker-controlled random purchase IDs cannot create unlimited rate-limit
  buckets and exhaust the bounded challenge authority.
- Initial and update payment submissions now share the durable encrypted
  request envelope: response loss or restart reuses the original idempotency
  key and sanitized evidence bytes instead of creating a duplicate payment.
- Encrypted evidence uses immutable generation files with the OS secure-store
  metadata as the commit pointer. Failed/ambiguous secure-store writes preserve
  the previously committed request; path traversal, symlink, hard-link,
  non-regular file, wrong owner/mode and truncated blob cases fail closed.
- Product payment status labels are explicitly Qt plain text.

### Verification

- Added negative tests for wrong-context grant reuse, randomized-ID rate-limit
  bypass, response-loss/restart update retry, secure-store failure/ambiguity,
  legacy blob migration and unsafe filesystem objects.

## 2026-07-15 - Admin security requirement closure (BOSQICH 4 review)

### Fixed and enforced

- Added `SQLiteAdminCredentialStore`: the configured PBKDF2 credential is a
  one-time bootstrap, password rotations persist across restarts in an
  owner-private database, and plaintext passwords are never stored.
- Added recent-MFA + current-password rotation, all-session revocation, audit
  and bilingual password/step-up UI.
- Added optional admin CIDR/VPN allowlisting after explicit trusted-proxy
  resolution. Malformed configuration fails startup; excluded clients cannot
  reach admin APIs.
- Payment approval/rejection, release creation/artifact/publish, account/license
  creation, device bind/replacement and activation-key issuance now require
  CSRF plus recent authentication.
- Password and MFA brute-force limits are account-global as well as client
  bounded. The lower-level backend factory now fails closed without MFA unless
  its explicit password-only test/development override is supplied.

### Verification

- Added persistent password restart, invalid-current-password, revoke-all,
  named-session revoke, reset audit, secure-cookie, trusted proxy + allowlist,
  cross-IP password/TOTP brute-force and HTTP recent-step-up tests.
- Added static EN/RU parity and no-browser-storage checks for the new security
  forms. A real local browser smoke covers enrollment, recovery step-up,
  password rotation and re-login; production secrets are not used.

## 2026-07-15 - Admin TOTP MFA and hardened sessions (BOSQICH 4)

### Added

- `product_backend/api_totp.py`: standard-library RFC 6238 TOTP
  (HMAC-SHA1, 6 digits, 30-second step, ±1 step drift), constant-time code
  comparison that returns the matched time step for replay defence, base32
  secret encoding, `otpauth://` provisioning URIs, and single-use recovery-code
  generation/normalization. No secret is ever logged or placed in an exception.
- `product_backend/admin_mfa.py`: `MfaSecretCipher` (AES-256-GCM sealing of the
  TOTP secret under a subkey derived from an operator master key, AAD-bound to
  the admin subject) and `SQLiteAdminMfaManager` (a private `admin-mfa.sqlite3`
  store). The TOTP secret is never stored in plaintext; recovery codes are kept
  only as keyed HMAC-SHA256 digests, are single-use, and are revoked in bulk on
  regeneration. A used time step cannot be replayed. A missing/short master key
  is fail-closed. MFA events (enrollment started/completed, disable/reset, login
  success/failure, TOTP failure/replay, recovery use, session revoke) are
  audited without ever recording a secret or code.
- `product_backend/admin_mfa_api.py`: MFA enrollment (begin, server-rendered QR
  PNG, activate), recovery regenerate, disable/reset, session listing, revoke
  one/revoke all, TOTP/recovery step-up re-auth, plus the single-step login
  second-factor integration.
- Admin console UI (`admin_web/static`): a bilingual (EN+RU) Security panel with
  the QR enrollment flow, a TOTP field on login, a recovery-code screen carrying
  a download/print warning, and session management with revoke controls.
- Tests: `tests/test_product_backend_totp.py` and
  `tests/test_product_backend_mfa_sessions.py` cover valid/invalid/expired TOTP,
  clock drift, replay, incomplete/complete enrollment, recovery single-use and
  regeneration, the missing-encryption-key fail-closed path, session rotation,
  idle and absolute timeouts, logout, revoke-all, CSRF, rate limiting, trusted
  proxy resolution, audit, and absence of secret leakage.

### Why

- Bosqich 4 hardens the admin panel: a stolen or guessed admin password alone
  can no longer approve payments, grant entitlements, or issue activation keys.

### Changed

- `product_backend/api_auth.py`: session assurance levels (`mfa_pending` vs
  `mfa_satisfied`), idle timeout, absolute timeout, rotation on login/step-up,
  revoke-all-for-subject, per-session revoke, recent-auth window for sensitive
  actions, and a `TrustedProxyConfig` that only honors `X-Forwarded-For` from an
  explicitly configured trusted proxy.
- `product_backend/api_app.py`: single-step `subject + password + TOTP` login,
  a full-assurance gate on protected routes, and optional MFA wiring — when no
  MFA manager is injected the login stays single-factor, preserving existing
  callers and tests.
- `product_backend/runtime.py`: production assembly now requires
  `JARVIS_ADMIN_MFA_KEY_FILE` (owner-only, fail-closed), builds the MFA manager,
  makes MFA mandatory by default (`JARVIS_ADMIN_MFA_ALLOW_PASSWORD_ONLY` is an
  explicit dev opt-in), and reads `JARVIS_TRUSTED_PROXIES`.

### Constraints kept

- Cross-platform-neutral backend; new UI strings bilingual EN+RU; no real
  secret/key committed; `cryptography`/`qrcode` were already in
  `requirements.txt`; existing product/entitlement contract untouched.

### Verification

- `.venv/bin/python -m pytest tests/ -q` → 560 passed, 372 subtests passed.
- `py_compile` on `main.py` and the whole `product_backend` package.
- Manual browser smoke: local backend start, password login, QR enrollment,
  authenticator-code activation, recovery-code screen, active status, and
  session management all rendered and functioned bilingually.

## 2026-07-14 - Durable payment and activation flow (BOSQICH 3)

### Problem

- Fresh-purchase evidence upload could return a spurious `503` after the
  payment row was already durably persisted. The one-time upload grant was
  reserved for the request but was still time-pruned on expiry, so a slow or
  large upload that crossed the grant TTL made the closing `commit_grant` fail
  as if the authorization were lost — turning an accepted payment into an error
  and, on the client's retry, risking a duplicate. (P1-1)
- The client held the payment idempotency key only in memory. A lost response
  or an app restart discarded it, so a retry could mint a new key (and a new
  purchase account/license), creating a duplicate payment. (P1-2)

### Fix

- `InitialPurchaseAuthorizer` no longer time-prunes a grant that an in-flight
  request has reserved. Grant expiry now gates only admission at
  `reserve_grant`; once reserved, `commit_grant` is deterministic after the
  payment is persisted. This mirrors the already-correct
  `DeviceActionGrantManager`.
- Added `core/payment_request_store.py`: a durable, envelope-encrypted client
  payment-request store. Metadata (idempotency key, release/device/customer
  context, proof digest, state, timestamps, server ids, and the per-request
  data key) is one small secret inside the OS `SecureStore`; the sanitized
  screenshot is AES-256-GCM encrypted (AAD-bound to the envelope identity) in a
  private `0o600` blob. No secret or screenshot byte is ever written as plain
  JSON, and clearing the keychain secret cryptographically shreds the blob.
- `ProductRuntimeService` now persists the request before submitting, resumes a
  pending request after a restart or lost response with the exact same
  idempotency key and evidence (`resume_pending_payment`), and clears the
  request only after a confirmed submission. `main.py` best-effort resumes any
  pending request at startup. If the secure store is unavailable the submit
  returns an honest `not_available` instead of submitting without durability.
- This finalizes the BOSQICH 3 fresh-purchase path built on top of the earlier
  secure metadata-free screenshot sanitization, server idempotency by
  `client_submission_id`, explicit reject/resubmit supersession, and the real
  client↔backend end-to-end flow.

### Constraints kept

- One paid plan, per-exact-semantic-version entitlement, no subscription/kill
  switch. Payment screenshots stay private evidence objects.
- Every new capability is cross-platform through `SecureStore` and the
  `cryptography` AEAD, or returns an explicit honest `not_available`; no new
  visible UI string was introduced.

### Verification

- `.venv/bin/python -m pytest tests/ -q`: 519 passed, 367 subtests, 0 failures.
- New tests: durable envelope save/load/clear, corrupt/tampered/unavailable
  paths (`test_payment_request_store.py`); client restart, lost-response resume
  with a reused idempotency key, retry ignoring a new pick, and secure-store
  unavailable (`test_product_runtime.py`); grant-expiry commit determinism and
  replay rejection (`test_initial_purchase.py`); and an API-level slow-upload
  grant-expiry regression proving no spurious 503 and an idempotent retry
  (`test_product_initial_purchase_grant_race.py`).
- Temporarily reverting the P1-1 fix reproduced the 503 and failed the new
  grant-expiry tests, confirming they catch the defect.

## 2026-07-14 - Product license gate before Gemini onboarding

### Fix

- Startup now verifies a signed exact-version product entitlement before
  opening Gemini credential onboarding or constructing `JarvisLive`.
- Added a framework-independent, event-driven license gate and bootstrap
  coordinator. Source builds require the explicit
  `JARVIS_DEV_LICENSE_BYPASS=1` override; packaged builds always ignore it and
  fail closed.
- Added a non-dismissible bilingual English/Russian product gate with
  activation, refresh, device-replacement guidance and an honest initial
  purchase entry. The initial purchase button does not claim success while the
  server-backed BOSQICH 3 flow is unavailable.
- Activation now distinguishes a possible active-device conflict. The backend
  checks device binding before consuming the one-time activation credential,
  so an admin replacement can be followed by a retry.
- Successful activation is reported only after secure license-state readback
  and offline certificate verification. Cache availability failures remain
  distinct from invalid certificates.

### Verification

- Added frozen/source override, no-license, exact-version, paid-new-version,
  offline restart, corrupted certificate, device mismatch, event ordering,
  UI contract and credential non-consumption tests.
- Native macOS Qt smoke verified license overlay -> verified gate close ->
  Gemini setup ordering without reading a real Gemini credential.

## 2026-07-14 - Non-interactive cross-platform Gemini credential storage

### Problem

- macOS used `security add-generic-password -w` without a password argument.
  That command enters an interactive TTY prompt and could leave onboarding with
  an unavailable-storage result.
- The legacy JSON credential was a read-only plaintext fallback rather than a
  verified one-time migration, and Windows had no credential backend.

### Fix

- Replaced the macOS CLI boundary with native Security.framework `SecItem*`
  calls and explicit authentication-UI denial; no secret enters argv or a shell.
- Added native Windows Credential Manager CRUD while preserving Linux Secret
  Service stdin behavior and honest unavailable/failure statuses.
- Gemini writes now require secure-store readback verification. Added delete,
  update/restart persistence coverage, fixed-message exception handling, and a
  bounded, no-follow, atomic, idempotent legacy JSON migration that removes only
  `gemini_api_key` after verified secure persistence.
- Separated validation outcomes from storage outcomes and cleared/deallocated
  the password widget as soon as onboarding hands the value to its bounded
  worker.

### Constraints kept

- No real credential, API key, private file, environment secret, or command-line
  secret was added or printed.
- Other legacy JSON fields are preserved; secure-storage or cleanup uncertainty
  fails closed.
- Device identity and product-license users of the shared secure-store contract
  retain the same platform-neutral result vocabulary.

### Verification

- Focused credential and product secure-store regression tests passed.
- Full Python test suite passed.
- Real disposable macOS Keychain CRUD, no-prompt behavior, three-process restart
  persistence, update/delete, and native legacy migration/cleanup smoke tests
  passed outside the tool sandbox without printing test secret values.

## 2026-07-13 - Exact-version product, commerce, activation and release foundation

### Added

- Added the one-plan commerce model for account, license, one active device,
  bilingual release details, target artifacts, manual payment review,
  exact-version entitlement and append-only admin audit.
- Added one-time activation credentials, Ed25519 device proof, signed offline
  entitlement certificates, signed update manifests and single-use downloads.
- Added an environment-driven FastAPI backend and bilingual EN/RU `/admin/`
  interface for provisioning, releases, payments and audit.
- Added private payment-image storage and owner-only external payment-instructions
  configuration. No real payment details or signing material are stored in Git.
- Added desktop activation/update/payment controls, exact version/build/status,
  device ID, release features/fixes/platforms and verified download staging.
- Added Gemini-key validation before secure-store persistence and a bilingual
  permission note that does not request extra onboarding fields.
- Added macOS PyInstaller/DMG planning, strict packaged build metadata, a separate
  pinned `requirements-build.txt`, release checklist and operations docs.

### Security

- Frozen metadata errors and writable product config can no longer bypass the
  packaged entitlement/trust-root boundary.
- Release artifacts use pre-verified pinned-FD constant-memory streaming;
  payment images enforce a decoded-pixel budget.
- Admin login has pre-PBKDF2 client and subject budgets, route-aware request-body
  limits, CSRF and bounded one-time grants.
- The updater contract passes no raw staged path and requires a private copied
  artifact to be hash/size verified before any future mutation; all real OS
  install adapters still report honest `not_available`.

### Release status

- This is a tested product foundation, not a customer-ready release. Commercial
  rights, PyQt6 licensing, production origins/keys/payment data, Apple signing,
  notarization, a privileged atomic updater and clean-Mac verification remain
  external release gates.

## 2026-07-13 - Admin-controlled device replacement

### Added

- Added a CSRF-protected admin API route that replaces the one active device
  binding only when the submitted current fingerprint still matches.
- Added an English/Russian admin form with explicit current/new fingerprints,
  target platform and architecture, optional label, bounded reason, and a
  confirmation step.
- Added focused API and static UI tests for authorization, history retention,
  old-device deactivation, new-device activation, invalid requests, and replay
  rejection.

### Constraints kept

- Device replacement remains admin-only and platform-neutral.
- The old binding remains in history; replacement does not create a remote kill
  switch for an already installed offline copy.
- The API response does not echo the replacement reason or old fingerprint.

### Verification

- `.venv/bin/python -m py_compile main.py product_backend/api_app.py` passed.
- Focused device-replacement and admin static tests passed.
- Admin translations JSON and JavaScript syntax checks passed.

## 2026-07-13 - Naming consistency: drop leftover XLVIII from UI badge + readme

### Added

- HUD footer badge `badge.protocol` (EN + RU) changed from `PROTOCOL / XLVIII` to
  `PROTOCOL / JARVIS` in `core/i18n.py`, so no visible app string still carries the
  old MARK number after the rename to Jarvis.
- `readme.md` reframed: title is now **Jarvis**, described as an AkbarCustom fork of
  MARK XLVIII (48) by FatihMakes; project-structure folder label `Mark XLVIII/` →
  `Jarvis/`. Upstream attribution and the CC BY-NC license section were preserved.

### Why

- Follow-up to commit `3a57f8b` (naming cleanup): those two spots still showed the
  old name, causing visual/documentation inconsistency with the Jarvis rename.

### Verification

- `python -m py_compile main.py ui.py core/i18n.py` → OK.
- `python -m pytest tests/ -q` → 228 passed, 105 subtests passed.
- Grep confirms no `XLVIII` remains in any runtime code (`.py/.json/.html/.js`).

## 2026-07-12 - Mobile app: consistent language, in-app settings, voice reply

### Problem

- The phone web app mixed languages (Uzbek install banner over an English UI), had
  no in-app settings, and no way to hear replies by voice.

### Added

- Self-contained i18n (uz / ru / en) in `dashboard/static/app.html` and `login.html`,
  default **Uzbek**, so the UI is one consistent language. Strings are marked with
  `data-i18n` and swapped live by `applyLang()`; the choice persists in
  `localStorage.jarvis_lang` and `login.html` follows the same saved language.
- An in-app **settings** sheet (gear button in the header) with a language selector
  (O‘zbek / Русский / English) and a **Voice reply** toggle, styled as an
  Unlumen-style switch.
- **Voice reply**: when the toggle is on, each JARVIS text reply is read aloud on the
  phone via the browser `speechSynthesis` API (`_speak()`), in the selected language.
  Client-side only — no backend audio streaming. (Streaming JARVIS's own Gemini voice
  to the phone would be a larger backend change; noted as a possible follow-up.)

### Verification

- Rendered at 375×812 (in-app browser): header shows the ⚙ gear; UI defaults to Uzbek
  ("MASOFAVIY KIRISH", "ULANISH", "JARVIS'ga buyruq yozing", "YUBORISH"). Opening
  settings shows the language row + voice toggle. Live switch uz→ru updated the SEND
  button to "ОТПРАВИТЬ" and the title to "Настройки" instantly; voice toggle flips and
  `speechSynthesis` is available. Login page renders fully in Uzbek. No console errors
  on either page. Dashboard served the new markers after restart.

## 2026-07-12 - Result screenshot to phone + exact-fit viewport

### Added

- After every remote command, JARVIS sends the phone a screenshot of the resulting
  Mac screen. `main.py::_process_dashboard_commands` schedules
  `_send_result_screenshot()` ~2.5s after dispatch (time for the action to run); it
  reuses `actions/screen_processor._capture_screen()` (mss + JPEG compress) off the
  event loop, base64-encodes a data URI, and broadcasts `{type:"screenshot"}`. The
  phone (`app.html`) renders it as a tappable image bubble (`_onScreenshot`).
- `app.html` now pins the body to `visualViewport.height` (updated on resize/scroll/
  orientation) so the input row stays on-screen when the iOS Safari toolbar or the
  keyboard appears — CSS `100dvh` alone left the footer hidden behind them.

### Why

- Akbar asked that any mobile command return a screenshot of the executed screen,
  and that the phone app stop running off the screen edge.

### Verification

- `actions/screen_processor._capture_screen()` returns a real JPEG (171 KB) — Screen
  Recording permission is granted. `py_compile` OK; full suite 157 passed.
- Restarted Jarvis: `/` serves `_onScreenshot`, `msg-shot`, the `screenshot` message
  branch, and the `visualViewport` fit script. App page renders at 375×812 with no
  console errors and no horizontal/vertical overflow.
- NOTE: without macOS Screen Recording permission the capture shows only the desktop,
  not app windows — an honest OS limitation.

## 2026-07-12 - Tailscale Funnel provider: stable public URL, no own domain

### Problem

- Cloudflare quick tunnel works but mints a new random URL on every restart, so an
  installed PWA icon breaks. A Cloudflare *named* tunnel needs a domain the user does
  not have. Deploying JARVIS itself to a PaaS (the user tried Railway) is impossible —
  JARVIS is a Mac desktop app (PyQt6 GUI, local mic, local machine control), so it
  crashes on a headless cloud container.

### Fix

- Added a `TailscaleFunnel` provider to `core/remote_tunnel.py`: exposes the local
  dashboard on a STABLE `https://<host>.<tailnet>.ts.net` URL with real TLS and no
  domain of one's own. The Funnel config lives in `tailscaled`, so the URL survives
  Jarvis restarts and reboots. Uses `https+insecure://localhost:PORT` for the
  self-signed dashboard origin. Honest: not_installed / failed (not logged in) never
  fake a URL.
- `main.py::_apply_remote_tunnel` now selects the provider from
  `settings.remote_tunnel.provider` (`tailscale` | `cloudflare`); `config/settings.json`
  set to `tailscale`.

### Verification

- Tailscale 1.98.8, logged in; `tailscale funnel --bg https+insecure://localhost:8000`
  serving `https://macbook-air.<tailnet>.ts.net`.
- External check from off-tailnet (WebFetch from Anthropic infra + the user's phone on
  cellular): `https://<host>.ts.net/login` → 200, title "JARVIS", "Remote Access", the
  install banner rendered, valid TLS, no error. (The Mac cannot test its own Funnel URL
  — MagicDNS resolves it to the internal tailnet IP.)
- Jarvis restarted with `provider=tailscale`: dashboard 200, no cloudflared spawned,
  funnel still serving. `py_compile` OK; full suite 157 passed, 44 subtests.

## 2026-07-12 - Mobile fit + Unlumen-inspired skin for the phone PWA

### Problem

- On a phone the remote-control web app ran off the screen: the footer command
  row (input, attach, mic, SEND) was pushed below the visible area, and header
  content sat under the notch / home indicator. Akbar also asked to improve the
  UI with a matching skin and Unlumen-style animations.

### Fix (web PWA only — no desktop runtime touched)

- `dashboard/static/app.html` + `login.html`: switched the full-height layout
  from `height:100%` to `100dvh` (with a `100vh` fallback) so the visible
  viewport — not the taller layout viewport — drives sizing; the input row now
  stays on-screen when the mobile URL bar shows.
- Added `env(safe-area-inset-*)` padding to the header, footer, quick-bar and
  login card so nothing hides under the notch, home indicator, or rounded corners.
- `login.html`: card width is now `min(340px, 100%)` (was a fixed `340px` that
  overflowed on ≤372 px phones) and the page scrolls when the card is tall
  (install banner + voice note); the card centres when it fits.
- `app.html`: a long `trycloudflare.com` tunnel URL in the header now truncates
  with an ellipsis (`max-width:42vw`) instead of pushing the header off-screen.
- Skin/animation pass, Unlumen used as **animation/visual reference only** per
  `AI_RULES.md` (no desktop hover / cursor / magnetic effects forced onto touch):
  glass (backdrop-blur) header/footer/card, indigo gradient + glow SEND/CONNECT
  buttons, glowing status badge, shimmering "JARVIS" wordmark, spring-eased
  message reveal and press micro-interactions, plus a `prefers-reduced-motion`
  guard. Changes are additive CSS overrides at the end of each `<style>` block —
  small and reversible; all element ids/classes and JS behaviour unchanged.

### Constraints kept

- Web-design task only: no change to `main.py`, `ui.py`, `actions/*`, `core/*` or
  the desktop assistant runtime (Unlumen rule #10). No new backend strings, so no
  EN+RU i18n keys added. No dependency added (Unlumen stays a reference).

### Verification

- Served `dashboard/static` and rendered both pages in a mobile viewport:
  - 375×812: `login` card fits and centres; `app` footer bottom = 812 (on-screen),
    no horizontal overflow.
  - 320×720 (narrow phone): `login` no horizontal overflow (docW == winW == 320,
    card 288 px).
  - Long tunnel URL in `app` header: overflow before fix (scrollW 479 > 375),
    none after (375 == 375).
- Static-preview-only artefacts (not bugs): `NO ENC` badge and literal
  `__IP__:__PORT__` — the real server replaces `__IP__/__PORT__` and loads
  CryptoJS (AES-256). Not verified on a physical phone yet.

## 2026-07-12 - Tunnel HTTPS-origin fix + install-first onboarding

### Problem

- With the Cloudflare tunnel enabled, the public URL returned 502. Root cause: the
  dashboard serves HTTPS (a self-signed cert exists under `config/certs/`), but
  `core/remote_tunnel.py` hard-coded an `http://localhost:PORT` origin, so
  cloudflared could not reach the TLS origin.
- The phone flow did not guide PWA installation; the user wanted "scan → install →
  connect", and iOS cannot auto-install a PWA.

### Fix

- `core/remote_tunnel.py`: `CloudflareTunnel` now takes `origin_https` and, when
  set, points cloudflared at `https://localhost:PORT --no-tls-verify` (Cloudflare
  still terminates real TLS at the edge; only the localhost origin cert is skipped).
  `main.py` passes `origin_https=self._dashboard._ssl_enabled()`.
- `dashboard/static/login.html`: added an install-first onboarding banner shown only
  when not already running as an installed PWA (`display-mode: standalone` /
  `navigator.standalone`), with platform-aware Add-to-Home-Screen steps and a
  "continue in browser" escape. Honest about the iOS manual-install requirement.

### Verification

- Standalone cloudflared with the corrected `https + --no-tls-verify` origin: public
  `https://*.trycloudflare.com/login` → 200 (JARVIS page), `/manifest.webmanifest`
  and `/static/icon-192.png` → 200. The prior `http` origin returned 502.
- Jarvis's own tunnel now runs the corrected command
  (`--url https://localhost:8000 --no-tls-verify`).
- `/login` serves the install banner markers. Full suite: 157 passed, 44 subtests.

## 2026-07-12 - Durable Qt fix applied: venv moved outside iCloud + live run

### Problem

- The diagnosed root cause (macOS re-applying `UF_HIDDEN` to the PyQt6 Qt plugin
  tree because `.venv` lived under the iCloud-synced `~/Desktop`) kept recurring:
  a one-time `chflags` repair passed the Cocoa smoke test, but a fresh
  `main.py` seconds later still crashed with `Could not find the Qt platform
  plugin "cocoa"` — proving the flags returned within seconds.

### Fix

- With Akbar's approval, moved the virtual environment out of the synced folder:
  copied `.venv` to `~/Library/Application Support/JARVIS/venv` (fresh files, no
  hidden flags), stripped any flags with `chflags -R nohidden`, backed up the
  original as `.venv.icloud-backup`, and replaced `.venv` with a symlink to the
  external venv. The base Python (`/opt/homebrew/opt/python@3.12`) is absolute, so
  the relocated venv resolves correctly.
- Fixed `scripts/launch_jarvis.command`: it hard-coded
  `~/Desktop/Mark-XLVIII-AkbarCustom`; it now derives `PROJECT_DIR` from the
  script's own location so it works regardless of the project folder name.
- Updated `.gitignore` to ignore the `.venv` symlink and `.venv.icloud-backup/`.

### Verification

- `scripts/check_qt_runtime.py` full GUI Cocoa smoke test: OK on the external venv.
- `main.py` launched for real: process alive, dashboard listening on TCP 8000/8001,
  Gemini connected (no Cocoa crash). Mobile web app served (HTTP 200 for `/login`,
  `/manifest.webmanifest`, `/sw.js`, `/static/icon-192.png`); served `/` contains
  the quick-command bar, PWA manifest/apple-touch-icon, and auto-reconnect markers.
- Durability re-check 1+ minute later: preflight OK and zero hidden plugin flags on
  the external venv (macOS no longer re-hides them outside the synced Desktop).

### Constraints kept

- No package versions changed; no `QT_PLUGIN_PATH` / `QT_QPA_PLATFORM_PLUGIN_PATH`
  overrides; PyQt6 not downgraded. Original venv preserved as a gitignored backup.

## 2026-07-12 - Qt Cocoa hidden-flag diagnosis and startup guard

### Problem

- `QApplication` again failed with `Could not find the Qt platform plugin "cocoa"`
  even though the Qt wheel, hashes, architecture, signature, metadata, permissions,
  and direct `QPluginLoader` load were valid.
- The exact cause was macOS `UF_HIDDEN` on the PyQt6 `Qt6/plugins` directories and
  `.dylib` files. Qt's default `QDir` scan therefore returned an empty plugin list.
  Reinstalling the same Qt wheel preserved the hidden parent state and was not a
  durable repair by itself.
- Repeated shell-only observation showed hidden flags returning on random plugin
  entries within seconds, without a Python process. A one-time `chflags` repair is
  therefore not durable while the venv remains under the current Desktop `.venv`.
- `actions/screen_processor.py` also imported OpenCV before the desktop
  `QApplication` existed, keeping an avoidable Qt/OpenCV startup interaction.

### Fix

- Cleared the incorrect local `UF_HIDDEN` flags without changing package versions.
- Added `scripts/check_qt_runtime.py` and wired it into
  `scripts/launch_jarvis.command` before `main.py`. The preflight:
  - resolves symlink aliases to the canonical project venv;
  - detects macOS `UF_HIDDEN`; the launcher's default path is read-only;
  - offers an explicit `--repair-hidden-flags` mode that refuses paths outside the
    venv and clears only `UF_HIDDEN` after user approval;
  - validates the PyQt/Qt/plugin paths and macOS/Windows/Linux platform filename;
  - constructs a real `QApplication` and blocks startup on failure;
  - never adds `QT_PLUGIN_PATH` / `QT_QPA_PLATFORM_PLUGIN_PATH`, installs packages,
    downgrades PyQt6, or reports a false success.
- Changed `actions/screen_processor.py` to lazy-load `cv2` behind a thread-safe
  lock only when camera capture is actually requested, after the desktop Qt
  application has initialized. The missing-OpenCV error is localized EN+RU.
- Added focused tests for symlink paths, moved/external venv rejection, missing and
  hidden platform plugins/directories, real macOS flag normalization, non-macOS
  no-op behavior, and serialized lazy OpenCV import.

### Verification

- Same versions retained: PyQt6 6.11.0, Qt runtime 6.11.1, PyQt6-sip 13.11.1,
  `opencv-python-headless` 5.0.0.93; `pip check` passed.
- Qt Cocoa binary matched its wheel RECORD hash, was arm64, codesign-valid, and
  directly loadable. After clearing `UF_HIDDEN`, default `QDir` again listed
  `libqcocoa.dylib`, `libqminimal.dylib`, and `libqoffscreen.dylib`.
- Controlled recurrence test: Cocoa was temporarily marked hidden, disappeared
  from Qt's default plugin listing, then explicit preflight repair cleared the
  plugin-tree flags and restored discovery inside that process. Subsequent
  shell-only checks observed metadata re-applying flags, proving relocation is
  still required for a durable runtime.
- `py_compile`: passed for `main.py` and all changed Python files.
- `python -m unittest discover -s tests -v`: **157 tests passed**.
- Focused preflight/lazy-import tests: **13 tests passed**, including the repair
  and concurrency paths.
- `bash -n scripts/launch_jarvis.command` and `git diff --check`: passed.
- `pytest` was unavailable in the venv (`No module named pytest`), so the existing
  `unittest` runner was used; no new dependency was installed.
- Fresh live Cocoa launch, dashboard HTTP checks, QR/PIN pairing, and iPhone Home
  Screen installation remain unverified because the final GUI `open` action was
  blocked by Codex's external usage limit and the current venv remains unstable.
  No success is claimed for those steps.

### Constraints kept

- No secret/private config was read or changed. No Qt path override, PyQt downgrade,
  dependency-version change, commit, or push was made. Launcher messages added by
  this change are bilingual English + Russian.
- Durable next step (not performed): after explicit approval, rebuild the venv in
  a non-hidden runtime directory outside Desktop, symlink project `.venv` to it,
  then repeat GUI/dashboard/iPhone acceptance tests.

## 2026-07-11 - Mobile Remote Control: anywhere access, sleep resilience, macros, settings window

### Added

- **Control from anywhere (Cloudflare Tunnel).** `core/remote_tunnel.py` wraps
  `cloudflared` (quick or named mode), parses the public
  `https://…trycloudflare.com` URL, restarts on failure, and reports an honest
  `not_installed` when cloudflared is absent — never a fake URL. Enabled via
  `config/settings.json` → `remote_tunnel` and wired in `main.py`. The dashboard
  now exposes the public URL in QR/PIN pairing (`set_public_url`,
  `get_lan_url`) and rate-limits `/login` for public exposure.
- **Sleep resilience.** The phone app auto-reconnects with backoff (device-token
  refresh on process restart) instead of dropping to login. While a phone is
  connected JARVIS keeps the computer awake cross-platform
  (`core/power_manager.py` + `prevent_sleep`/`release_sleep` adapters: macOS
  `caffeinate`, Windows `SetThreadExecutionState`, Linux `systemd-inhibit`),
  released after a grace period on the last disconnect. Toggleable in settings.
- **Command automation / macros.** `core/capabilities.py` (JARVIS's abilities as
  pickable options) + `core/macros.py` (`config/macros.json`, gitignored). Build a
  one-tap command from capabilities — one command → several actions — in the phone
  app (capability picker sheet) or the desktop settings; shared via
  `/api/capabilities` and `/api/macros`.
- **Installable PWA.** `manifest.webmanifest`, `sw.js` (caches app shell only,
  never API/ws), and app icons; served by new dashboard routes.
- **Desktop settings window.** A corner **⚙** gear (`ui.py`) opens `SettingsOverlay`
  with remote on/off, Show QR/PIN, keep-awake, language RU/EN, paired-device
  revoke, connection status, and the macro builder — plus an animated native
  `ToggleSwitch`. Native PyQt6; Unlumen used only as animation reference per
  `AI_RULES.md`. Phone app gains Unlumen-inspired CSS animations.
- New EN+RU strings in `core/i18n.py` (`tunnel.*`, `keepawake.*`, `settings.*`);
  safe `remote_tunnel` / `keep_awake_enabled` settings via `core/app_settings.py`.

### Verification

- `py_compile` on all changed runtime modules: passed.
- `python -m unittest discover -s tests`: 144 tests OK (124 existing + 20 new in
  `test_remote_tunnel`, `test_power_manager`, `test_capabilities_macros`).
- Live dashboard smoke test (curl, HTTPS): PWA login/app pages, manifest/sw/icons
  (200), PIN→token, `/api/capabilities` (10), `/api/macros` compose — all verified.

### Constraints kept

- Cross-platform parity (keep-awake per OS + honest unsupported; tunnel honest
  `not_installed`). Every new UI string bilingual EN+RU. No secrets in repo
  (cloudflared creds in `~/.cloudflared`; `config/macros.json` gitignored). No
  dependency changes; no new Gemini tool (main.py risk kept low). Truthful status
  reporting throughout.

## 2026-07-11 - Canonical Workflow Skill + Rule De-duplication

### Added

- New project-local skill as the **single source of truth** for the dev workflow:
  - `.claude/skills/mark-xlviii-workflow/SKILL.md` — the operational Before /
    During / After checklist (startup read order + `git status` + Uzbek state
    summary; cross-platform parity via `core/platform_adapters`; bilingual EN+RU UI
    in `core/i18n.py` `_MESSAGES`; truthful status vocabulary `Bajarildi.` /
    `Bajara olmadim.` / `Aniq tasdiqlay olmadim.` / `Tasdiqlaysizmi?`;
    protected-file list; Personal Briefing / Zerno route; `py_compile` /
    `pytest tests/` verification; Uzbek after-work report).
  - `.claude/skills/mark-xlviii-workflow/references/detailed-rules.md` — the
    exhaustive rules absorbed from `AI_RULES.md` / `AGENTS.md` (full cross-platform
    contract, Personal Briefing / Zerno semantics, git/memory discipline,
    high-risk-file list) so nothing is lost.

### Changed (universal + mandatory + de-duplicated)

- The skill triggers on this repo and appears in the skills list, so any Claude
  Code session auto-applies it. It is framed as MANDATORY for every AI agent
  (Claude, Codex, or any code bot).
- `AI_RULES.md`, `CLAUDE.md`, `AGENTS.md`: removed the duplicated rule bodies and
  slimmed each to a **mandatory pointer** to the skill, plus a short safety floor.
  Non-Claude agents are told to read the skill files by path in full (they don't
  auto-load Claude skills), which keeps the rules universal.
- `PROJECT_MEMORY.md`, `PROJECT_MAP.md`: content kept (they are architecture/state
  and the map, not duplicated rules); added a one-line pointer header to the skill.

### Why

- The same rules were restated across five root docs and could drift out of sync.
  Consolidating them into one triggered, mandatory skill gives every agent one
  operational checklist and one place to update.

### Verification

- `SKILL.md` YAML frontmatter parses (PyYAML); `name` matches the directory;
  body < 500 lines. `mark-xlviii-workflow` now appears in the available-skills
  list.
- No runtime code touched; no secrets added; nothing committed.

## 2026-07-11 - Personal Briefing Zerno-Backed Source Fallback

### Problem

A named statistics request such as `Instagram statistikasi` reaches
`personal_briefing` with `sources=["instagram"]`. The standalone Instagram adapter
is `not_configured`, so the request returned only `not_configured` even though the
connected Zerno hub already holds the available statistics. The same gap applied to
`telegram`, `messenger`, `channels`, `bots`, and `posts`.

### Fix

- `actions/personal_briefing.py`: added `_apply_zerno_fallback()` plus
  `_zerno_backed_source()`. When a named external source is `not_configured`, the
  briefing reuses the connected Zerno hub (collected at most once, reusing an
  already-requested Zerno report). Each source maps only to the Zerno metric groups
  that legitimately belong to it (`_ZERNO_FALLBACK_GROUPS`), so unrelated Zerno data
  (for example a generic `posts` group) never becomes fake Instagram statistics.
  - Zerno connected + platform metrics present → `connected` with `backing_source=zerno`
    and only the real, API-returned numbers.
  - Zerno connected + no platform-specific metrics → `not_available` with a clear
    "Zerno connected but no Instagram-specific metrics" message; no numbers invented.
  - Zerno `not_configured` → source stays `not_configured` (honest combined message).
  - A standalone adapter that is actually configured wins; only `not_configured`
    triggers the fallback.
- `core/briefing_routing.py`: extended `_SOURCE_ALIASES` with `channels`, `bots`, and
  `posts` so those named statistics phrases route to `personal_briefing` with the
  named source plus the Zerno hub.
- `tests/test_zerno_fallback.py`: instagram→connected-Zerno fallback, direct adapter
  wins when configured, no-Zerno stays `not_configured`, Zerno posts without platform
  metadata do not become fake Instagram stats, wrong-platform groups are not borrowed,
  every named source falls back, routing of named requests, and gitignore protection.

### Constraints kept

- No followers/reach/likes/revenue/engagement invented; only API-returned numbers show.
- API URL/token stay in the gitignored `config/briefing_sources.json` /
  `config/local_env.zsh`; nothing secret committed.
- `local_projects` and world-news routing unchanged.

### Verified

- `.venv/bin/python -m py_compile` on changed files and `main.py`: OK.
- Full suite: 122 tests, `OK`. `git diff --check`: clean.

## 2026-07-11 - PyQt6 Platform-Plugin Recovery (second, separate fix)

### Problem

After the OpenCV headless swap, the window still did not open. A second, independent
fault was found: PyQt6's Qt platform-plugin discovery was broken. `QApplication` failed
for **every** platform — `cocoa`, `offscreen`, and `minimal` — with
`Could not find the Qt platform plugin "..." in ""`, even though the plugin `.dylib`
files were present, arm64, ad-hoc signed, un-quarantined, and loadable directly
(`QPluginLoader.load()` returned `True`). Qt's factory loader scanned the correct
directory but registered zero plugins. This was reproducible with no cv2 imported and
with the sandbox disabled, so it was not the OpenCV conflict and not a GUI-session issue.

### Fix

- Force-reinstalled the Qt binary wheel **at the same version** in the `.venv`:
  `python -m pip install --force-reinstall --no-deps PyQt6-Qt6==6.11.1`. The previously
  installed plugin files were in a bad state (residue from earlier PyQt6 version churn).
  Reinstalling restored the plugin metadata so the factory loader registers `cocoa` /
  `offscreen` / `minimal` again. No version change, no downgrade, no `QT_PLUGIN_PATH`
  workaround — exactly the "reinstall the application" remedy Qt's own error suggests.

### Verified

- `import cv2` then `QApplication` now succeeds on **cocoa**, offscreen, and minimal.
- Real UI path builds and renders: `ui.MainWindow('face.png')` constructs, `.show()`,
  one `processEvents()` cycle, and `.close()` all succeed with cv2 imported (offscreen).
- Full test suite still green: 114 tests, `OK`.
- `requirements.txt` already pins `PyQt6-Qt6==6.11.1`, so a fresh install reproduces the
  healthy state; only the local `.venv` needed the in-place repair.

### If it recurs

If Qt again reports "Could not find the Qt platform plugin ... in ..." while the paths
are correct, re-run the same force-reinstall of `PyQt6-Qt6`. Do not add `QT_PLUGIN_PATH`
/ `QT_QPA_PLATFORM_PLUGIN_PATH` overrides and do not downgrade PyQt6.

## 2026-07-11 - OpenCV Headless / PyQt6 Cocoa Conflict Fix

### Changed

- Replaced `opencv-python` with `opencv-python-headless==5.0.0.93` in `requirements.txt` and in the local `.venv`. The GUI (`opencv-python`) build bundled its own Qt runtime under `cv2/qt/` and forced Qt's platform-plugin path, which masked PyQt6's bundled Cocoa plugin and crashed `QApplication` with `Could not find the Qt platform plugin "cocoa"`.

### Why This Is Safe

- Audited all OpenCV usage: only `VideoCapture`, `imencode`, `cvtColor`, and capture-backend constants (`CAP_AVFOUNDATION` / `CAP_DSHOW` / `CAP_ANY`) are used in `ui.py` and `actions/screen_processor.py`.
- No OpenCV GUI APIs are used anywhere (`imshow`, `waitKey`, `namedWindow`, `destroyAllWindows`, `createTrackbar`, `setMouseCallback`, `selectROI` — none present). Camera/screen frames are already displayed through the PyQt6 UI, so the headless build loses no functionality.
- No `QT_PLUGIN_PATH` / `QT_QPA_PLATFORM_PLUGIN_PATH` workarounds were added; PyQt6 was not downgraded. The launcher stays free of Qt path overrides.

### Verified

- `opencv-python-headless` no longer ships a `cv2/qt/` directory; `import cv2` does not mutate `QCoreApplication.libraryPaths()` and never sets `QT_QPA_PLATFORM_PLUGIN_PATH` (was the source of the conflict).
- Headless capture/encode APIs intact: `imencode`, `cvtColor`, `VideoCapture`, and macOS `CAP_AVFOUNDATION` all resolve.
- `py_compile` passes for `main.py`, `ui.py`, `actions/screen_processor.py`; `actions.screen_processor` imports cleanly.
- Full test suite green: 114 tests, `OK`.
- Note: the live Cocoa window cannot be opened inside this headless agent subprocess (a bare `QApplication` with no cv2 fails identically here because it is not a GUI login session); the Cocoa GUI must be re-confirmed by Akbar in a normal desktop session via `python main.py` and the launcher.

## 2026-07-11 - Spoken Reminder Delivery

### Added

- Added a private file-based reminder event bridge that lets an active, idle Gemini Live session speak scheduled reminders with the configured Charon voice.
- Added atomic event claiming so Gemini speech and the scheduler fallback do not both speak the same reminder.
- Added local speech fallback through macOS `say`, Windows System.Speech, or Linux `spd-say`/`espeak`, while retaining the existing OS notification.
- Added mechanical tool blocking for reminder-originated Gemini turns, local playback completion checks, renewable per-process claim leases, atomic stale-claim recovery, bounded idle waits, and bounded retries when both Live and system speech fail.

### Changed

- Reminder platform selection now uses DeviceProfile instead of reading OS data from the secret API configuration.
- Reminder scripts use unique IDs, private event files, bounded reminder text, absolute macOS command paths, argv-only subprocess calls without shell execution, quoted Linux `at` paths, and one-shot macOS LaunchAgent cleanup.
- Client-content turns are serialized around reminder delivery; pending microphone chunks are drained/gated before the reminder prompt, and fallback starts only after incomplete Live audio is stopped.

### Verified

- Added focused tests for event validation/claiming/retry, prompt-data isolation, runtime tool blocking, DeviceProfile scheduler routing, safe generated scripts, notification-plus-speech behavior, queued Live dispatch, playback failure, and system fallback.

## 2026-07-11 - Zerno Statistics Integration

### Added

- Added a configurable Zerno source adapter inside Personal Operations Briefing with bounded stdlib HTTP, Bearer authentication, flexible dict/list/nested JSON normalization, secret-field redaction, and explicit `connected` / `not_configured` / `failed` states.
- Added `config/briefing_sources.example.json`, interactive `scripts/setup_zerno_stats.sh`, safe `scripts/check_zerno_stats.py`, and `docs/ZERNO_SETUP.md`.
- Added normalized Zerno metric groups, latest updates, evidence-based `foyda` / `zarar`, next action, confidence, last-check time, and up to three priorities without inventing absent fields.

### Changed

- Personal Briefing now treats a valid Zerno JSON response as connected evidence and renders only metric groups actually returned by the API.
- Telegram/Instagram/Messenger statistics requests also inspect Zerno as the configured operations hub, while standalone source adapters remain honestly `not_configured`.
- Added deterministic Personal Briefing routing for `kanallarimni tekshir` and `botlarimni tekshir`; explicit world news remains on the existing news route.

### Security

- Added Git ignore protection for `config/briefing_sources.json` and `config/local_env.zsh`; real tokens remain environment-only and are never committed, logged, or stored in project memory.
- Setup writes local files atomically with restricted permissions, and the check CLI redacts the full token even in debug mode.

### Verified

- Passed the isolated staged-tree full test suite: 84 tests.
- Passed `py_compile` for `main.py`, Zerno/briefing runtime modules, and the check CLI; also passed setup-script syntax and `git diff --check`.
- Exercised setup with a dummy token in a temporary Git repository only; no real Zerno request was made because no local URL/token is configured.

## 2026-07-11 - PyQt6 Latest Compatible Upgrade

### Changed

- Upgraded the tested GUI runtime pin to PyQt6 6.11.0, PyQt6-Qt6 6.11.1, and PyQt6-sip 13.11.1 on the `test/pyqt-latest-compatible` investigation branch.
- Verified the latest PyQt6/Qt pair with a minimal macOS `QApplication`, `main.py` compile, the unit test suite, terminal startup, and `Jarvis.command` launcher startup.
- Updated project memory to clarify that the launcher must not reintroduce manual Qt platform path overrides unless a future `QT_DEBUG_PLUGINS=1` run proves they are required.

## 2026-07-11 - PyQt6 Cocoa Startup Fix

### Changed

- Pinned the GUI runtime to PyQt6 6.8.1 and PyQt6-Qt6 6.8.2 after the minimal macOS `QApplication` test verified this pair loads the Cocoa platform plugin successfully.
- Removed manual `QT_PLUGIN_PATH`, `QT_QPA_PLATFORM_PLUGIN_PATH`, and `QT_QPA_PLATFORM` exports from `scripts/launch_jarvis.command`; Qt now uses PyQt6's bundled plugin discovery instead of launcher-forced paths.
- Documented the verified PyQt6/Qt pair in project memory so future dependency updates do not accidentally reinstall the failing PyQt6 6.11 / Qt 6.11 pair.

## 2026-07-11 - AkbarCustom v0.5 Personal Operations Briefing

### Audited

- Added `COMMAND_ARCHITECTURE_AUDIT.md` with the real voice/text input, Gemini function routing, central dispatch, startup/news, prompt, SessionContext, DeviceProfile, truthfulness, and sounddevice warning flows.
- Confirmed normal intent detection is Gemini function calling, with local handlers only for UI language and DeviceProfile; there is no existing `men uydaman` command handler.
- Confirmed generic world news was hardcoded into the once-per-process startup phase in `main.py`.

### Added

- Added `core/briefing_routing.py`, a narrow policy inside the existing command path for Personal Briefing phrases, explicit world news, named external-statistics requests, and defensive wrong-tool correction.
- Added `actions/personal_briefing.py` with an allowlisted `local_projects` adapter and explicit offline Telegram, Instagram, Messenger, and Zerno `not_configured` adapters.
- Added real central-dispatch, startup, source safety, no-fake-statistics, local docs/Git, and warning-filter tests.
- Added `core/runtime_warnings.py` for the exact sounddevice NumPy 2.5 shape deprecation.

### Changed

- Automatic startup now collects Personal Operations Briefing locally and gives Gemini only the verified report for a short spoken summary. It no longer promises or requests world news.
- `men uydaman`, `uydaman`, `ishga qaytdim`, `loyihalarimni tekshir`, `statistikani ayt`, and `personal briefing` now route to the registered `personal_briefing` tool.
- Explicit `dunyo yangiliklari`, `world news`, and `latest news` remain on `web_search(mode="news")`; implicit generic world-news calls are guarded.
- Personal Briefing returns evidence-based operational `foyda`, `zarar`, and `next_action`. Missing external integrations return `not_configured` and `statistics=None` without network calls or placeholder numbers.
- The exact sounddevice warning filter is installed before all project sounddevice imports and reapplied immediately before the microphone stream; unrelated deprecations remain visible.
- Added English/Russian Personal Briefing content-title and runtime-log localization.

### Safety

- No API keys, private long-term memory, dependency versions, `.venv` files, third-party package code, or unrelated reconnect/audio logic were changed.
- Personal Briefing reads only its allowlisted docs and read-only Git metadata; paths from Git status are counted but not exposed.

## 2026-07-10 - AkbarCustom v0.4 Universal Device Intelligence

### Added

- Added `core/device_profile.py` for DeviceProfile schema defaults, privacy scrubbing, summary/query helpers, routing decisions, and permission gates.
- Added `core/environment_discovery.py` for first-run and refresh-time environment discovery.
- Added `core/platform_adapters/` with base, macOS, Windows, and Linux adapters.
- Added `config/device_profile.example.json` and gitignored local `config/device_profile.json`.
- Added `device_profile` tool plus direct local refresh/query handling for typed and dashboard commands.
- Added `tests/test_device_profile.py` for profile creation, schema validation, platform detection, browser/media/message routing, permission gating, privacy, and `.gitignore` protection.
- Added `UNIVERSAL_COMPATIBILITY_AUDIT.md`.

### Changed

- `main.py` now creates/loads DeviceProfile at startup and adds a concise DeviceProfile context to the model prompt.
- Tool dispatch now consults `SessionContext` first, then `DeviceProfile`, then tool verification for platform-sensitive actions.
- Browser routing no longer assumes Chrome/Safari; it uses explicit browser, recent session browser, preferred/default browser, installed browser, or asks.
- App, media, messaging, screen/camera, and UI automation paths now have DeviceProfile preflight checks.
- `actions/media_control.py` now uses the platform adapter for non-macOS media control instead of a Mac-only design.
- Updated `AI_RULES.md`, `AGENTS.md`, `CLAUDE.md`, `PROJECT_MEMORY.md`, `PROJECT_MAP.md`, `NEXT_STEPS.md`, and `core/prompt.txt` with DeviceProfile rules.

### Privacy

- DeviceProfile stores operational metadata only and must not store API keys, tokens, passwords, full conversations, screenshots, audio, or private message/contact contents.
- Local `config/device_profile.json` remains ignored by git; only the safe example schema is committed.

## 2026-07-10 - AkbarCustom v0.3.1 Context Routing, Media Control, Log Cleanup

### Added

- Added `resolve_followup_intent(user_text, session_context)` and strengthened `SessionContext.resolve_follow_up(...)` to return resolved intent, target context, confidence, and reason.
- Added `actions/media_control.py` for safe macOS/system media pause/play-pause handling.
- Added dispatch-level rerouting so high-confidence vague media follow-ups execute media pause instead of generic close/settings close.
- Added tests for media follow-up routing, ChatGPT Atlas target preservation, browser close routing, unknown-context clarification, message confirm-send routing, correction updates, still-playing fallback, and unverified media truthfulness.

### Changed

- Vague stop/pause/o'chir follow-ups now inspect the last 5 meaningful action records before selecting a tool.
- Browser/page `yop` follow-ups resolve to browser close only when recent context supports it.
- Message `yubor` follow-ups use recent message context but require confirmation/verification before any sent claim.
- User corrections such as `GPT Atlas'da`, `Chrome'da`, `Safari emas`, `hali ham o'ynayapti`, and `ishlamadi` update recent runtime action context.
- Unverified media stop/pause returns uncertainty instead of claiming stopped.
- Added a narrow startup warning filter for the repeated `sounddevice` NumPy 2.5 shape `DeprecationWarning` without changing dependencies or hiding unrelated warnings.
- Updated `core/prompt.txt`, AI rules, project memory, project map, next steps, and agent docs with the new routing/truthfulness/media-control rules.

### Rule

Jarvis must use SessionContext before generic routing for vague follow-ups, pause media before any app/browser close, and never claim stopped/sent/opened/closed/done unless the action result is successful and verified.

## 2026-07-10 - UI Language Settings Command

### Added

- Added `config/settings.json` with safe `ui_language` storage.
- Updated `core/i18n.py` to load UI language from `config/settings.json`, then `JARVIS_UI_LANG`, then default to Russian.
- Added strict `ru` / `en` validation for UI language changes.
- Added typed/remote command detection for English/Russian UI switching, including mixed Uzbek commands like `inglis qil` and `rus qil`.
- Added a `set_ui_language` tool so spoken Jarvis commands can change the UI language setting.

### Changed

- UI language changes now return a clear restart message in English or Russian.
- Documented the settings-file language rule in `AI_RULES.md` and `PROJECT_MEMORY.md`.

## 2026-07-10 - AkbarCustom v0.3 Session Context And Truthful Actions

### Added

- Added runtime-only `SessionContext` / action history in `core/session_context.py`.
- Session context stores the last 5 meaningful actions with summarized user text, assistant intent, tool name, parameter summary, target app/context, execution method, result status, verification flag, visible claim, and user correction.
- Added helper tests for last-5 action retention, vague browser follow-up resolution, opened-browser app normalization, uncertain action claims, and correction attachment.

### Changed

- Wired `JarvisLive` tool dispatch to record action context and return structured `result_status`, `verified`, `truthful_user_claim`, and recent action context in tool responses.
- Added vague follow-up handling so recent browser/message/file context can fill missing tool parameters before falling back to defaults.
- Replaced generic `Done.` / fabricated send/open fallbacks with uncertain result language when verification is missing.
- Changed message automation to avoid claiming messages were sent when contact/chat or delivery cannot be verified; default behavior now returns an uncertain draft/attempt result.
- Added truthful-action and recent-context rules to `core/prompt.txt`, `AI_RULES.md`, `AGENTS.md`, `CLAUDE.md`, and `PROJECT_MEMORY.md`.

### Rule

Jarvis must not create narrow one-off fixes when a reusable context layer is needed, must inspect recent action context before vague follow-up commands, and must never claim success unless the tool result is verified.

## 2026-07-10 - Russian UI Localization

### Added

- Added simple dictionary-based English/Russian UI localization in `core/i18n.py`.
- Localized main PyQt UI labels, buttons, HUD status text, file upload text, setup overlay, remote overlay, camera labels, footer text, and common UI log messages to Russian by default.
- Added English fallback support through `JARVIS_UI_LANG=en`.

### Changed

- Updated `setup.py` installer messages to use the localization dictionary.
- Added the bilingual UI rule to `AI_RULES.md`, `AGENTS.md`, `CLAUDE.md`, and `PROJECT_MEMORY.md`.
- Added prompt guidance so assistant-restated UI/system status does not introduce English-only UI labels.

### Rule

From now on, every new visible UI text must be added in both English and Russian. Do not add English-only UI labels. Do not add Russian-only UI labels unless user explicitly asks. Keep UI localization simple and maintainable.

## 2026-07-10 - Reconnect Session Isolation

### Changed

- Added explicit Gemini Live session startup and cleanup helpers in `main.py`.
- Added session generation guards for mic, phone audio, send, receive, playback, briefing, monitor, and proactive tasks.
- Ensured stale mic callbacks from an old session cannot enqueue audio into a new session queue.
- Ensured reconnect starts with fresh queues, reset transient flags, tracked session tasks, and a newly built Live audio config.

## 2026-07-10 - Gemini 1006 Reconnect Stability

### Changed

- Treated Gemini Live `1006` / keepalive disconnects as recoverable reconnect events instead of crash-style runtime errors.
- Replaced long reconnect tracebacks with short terminal status lines.
- Added capped reconnect backoff: 3s, 6s, then 12s.
- Cleaned mic/audio queues and session audio state between reconnect attempts.
- Made audio output stream stop/close cleanup tolerant of shutdown-time errors.

## 2026-07-10 - Audio Queue Overflow Guard

### Changed

- Added a guarded outgoing audio queue helper in `main.py`.
- When mic or phone audio fills the outgoing queue, stale queued audio is drained and the newest chunk is kept.
- Prevented `asyncio.QueueFull` from escaping through the mic callback and spamming logs or crashing the runtime.

## 2026-07-10 - GitHub Remote And Commit Rules

### Changed

- Added GitHub commit/push workflow rules to `AI_RULES.md` and `AGENTS.md`.
- Documented AkbarCustom GitHub remote in project memory and project map.
- Clarified that reliable verified changes should be committed and pushed, while broken, untested, secret-containing, or uncertain changes must not be pushed.

## 2026-07-10 - AkbarCustom Initial Context Foundation

Version: AkbarCustom initial context foundation

### Added

- Created AkbarCustom copy context foundation.
- Added project memory and AI instruction files.
- Added markdown-based project map / knowledge graph.
- Added resource guide for important files and risk levels.
- Added next-step tracker for Mac testing and future customization.
- Added AkbarCustom changelog.

### Current Setup Status

- Dependencies installed.
- PyQt6 installed.
- Setup completed.
- `main.py` runs on Mac.
- Gemini connects.
- Microphone/audio works.

### Rule

Future meaningful implementation changes must be logged here.
