# JARVIS Release Checklist

No release is customer-ready unless every applicable item is checked with saved
evidence.  A local unsigned DMG does not satisfy this checklist.

An unchecked item is not implied by passing unit tests. Record each item as
**implemented**, **enforced**, **tested locally**, or **production-verified** with
its evidence. Record unavailable behavior as `not_available`, unfinished
repository work as **internal gap**, operator/platform dependencies as
**external blocker**, and rights/licensing as **legal blocker**. No current
component is production-verified. Use [`PACKAGING.md`](PACKAGING.md),
[`CLEAN_MAC_TEST.md`](CLEAN_MAC_TEST.md),
[`E2E_PRODUCT_VALIDATION.md`](E2E_PRODUCT_VALIDATION.md),
[`../SECURITY.md`](../SECURITY.md), and [`../THREAT_MODEL.md`](../THREAT_MODEL.md)
as the evidence index.

## Release-readiness matrix

This is the canonical per-function readiness matrix. Each cell is backed by
repository code and tests, not by intent. **No function is production-verified**
— that column is intentionally empty until real production infrastructure and
signed/notarized artifacts exist.

Legend: `✓` met · `◑` partial (see blocker) · `—` not met / not applicable.
Columns: **Impl** implemented · **Enf** enforced (fails closed) · **Unit**
unit-tested · **Integ** integration-tested · **E2E** covered by the Stage 9
`scripts/run_product_release_e2e.py` harness · **Man** recorded local manual
smoke · **Prod** production-verified · **Ext** an external/legal blocker gates
production.

| Function | Impl | Enf | Unit | Integ | E2E | Man | Prod | Ext | Primary blocker |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | --- |
| License gate | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | Frozen clean-Mac run needs Developer ID signing (scenario 1 `not_available`) |
| Payment + evidence | ✓ | ✓ | ✓ | ✓ | ✓ | ◑ | — | ✓ | Real payment/bank process; new-payment browser banner delivery `not_available` (scenario 7) |
| Entitlement (exact version) | ✓ | ✓ | ✓ | ✓ | ✓ | — | — | ✓ | Production entitlement private-key custody |
| Admin auth / MFA | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | Real deployment enrollment smoke; MFA master-key rotation is an internal gap |
| Mobile admin (PWA) | ✓ | ✓ | ✓ | ✓ | ◑ | ✓ | — | ✓ | Trusted-TLS iOS/Android smoke; native clients + push `not_available` |
| Secure storage | ✓ | ✓ | ✓ | ✓ | ✓ | — | — | ✓ | Real-host Keychain / Credential Manager / Secret Service smoke per OS |
| Updater | ✓ | ✓ | ✓ | ✓ | ◑ | — | — | ✓ | Production install helper `not_available` (scenarios 23–27); needs signed/notarized/audited helper |
| Packaging (macOS) | ✓ | — | ✓ | ◑ | — | ✓ | — | ✓ | Developer ID signing, notarization, stapling; Windows/Linux `not_available` |
| Deployment (backend) | ✓ | ✓ | ✓ | ✓ | — | — | — | ✓ | Real domain/TLS/server + backup-restore and key-rotation drills |
| Secrets hygiene | ✓ | ✓ | ✓ | ✓ | ◑ | ✓ | — | ✓ | Production key custody in a managed store (repository hygiene is verified) |
| Cross-platform contract | ✓ | ✓ | ✓ | ✓ | ◑ | — | — | ✓ | Windows/Linux real-host smoke; Windows/Linux packaging + updater `not_available` |
| Audit logs | ◑ | ✓ | ✓ | ✓ | ◑ | — | — | — | Unified product-wide audit view is an **internal gap**, not an external blocker |
| Rollback | ✓ | ✓ | ✓ | ✓ | ◑ | — | — | ✓ | Production signed helper + clean-Mac and power-loss drills |

Detailed per-function evidence: [`ADMIN_MFA.md`](ADMIN_MFA.md),
[`MOBILE_ADMIN.md`](MOBILE_ADMIN.md), [`UPDATE_ROLLBACK.md`](UPDATE_ROLLBACK.md),
[`PACKAGING.md`](PACKAGING.md), [`PRODUCTION_DEPLOYMENT.md`](PRODUCTION_DEPLOYMENT.md),
[`E2E_PRODUCT_VALIDATION.md`](E2E_PRODUCT_VALIDATION.md),
[`../SECURITY.md`](../SECURITY.md), and [`../THREAT_MODEL.md`](../THREAT_MODEL.md).
The consolidated blocker lists live in [`../SECURITY.md`](../SECURITY.md)
(internal / external / legal). The overall commercial decision is **NO-GO** until
every gate below is cleared with retained evidence.

## Commercial and identity gates

- [ ] Upstream CC BY-NC commercial permission/relicensing or rights-clean replacement documented.
- [ ] Lawful PyQt6 distribution model documented.
- [ ] Product name, icons, copy, bundled assets and reverse-DNS identifier cleared.
- [ ] Version is strict `MAJOR.MINOR.PATCH`; build is globally monotonic for its update stream.
- [ ] Admin price and release notes are final in English and Russian where visible.

## Secret and privacy gate

- [ ] No local API key, token, password, signing key or payment credential is in the source artifact.
- [ ] No personal memory, real device profile, real Zerno config, private certificate or payment screenshot is bundled.
- [ ] Secret scan covers source, PyInstaller analysis, `.app`, DMG and generated logs.
- [ ] Writable config/data/cache/log/update paths resolve outside `JARVIS.app` through `AppPaths`.

## Admin security gate

- [ ] Production starts fail-closed with owner-only MFA and session key material;
      the password-only development override is absent.
- [ ] Each operator enrolls RFC 6238 TOTP and stores one-time recovery codes
      offline; invalid, replayed and exhausted factors are rejected.
- [ ] Password rotation persists after restart, is audited and revokes every
      active session; current and new passwords never appear in logs/responses.
- [ ] CSRF, Secure/HttpOnly/SameSite=Strict cookies, idle/absolute expiry and
      recent step-up are verified on payment, release, license and device actions.
- [ ] Trusted proxies and optional admin/VPN CIDR allowlists match the deployed
      network; spoofed forwarding headers and excluded clients are rejected.

## Build verification

- [ ] Foundation and full regression tests pass on Python 3.12.
- [ ] PyInstaller dependency/native-library report is reviewed.
- [ ] `JARVIS.app` launches without Terminal, Python or dependency installation.
- [ ] App reports the exact expected product/version/build mechanically.
- [ ] Frozen startup enforces license/purchase/activation before Gemini
      onboarding; no-license and wrong-version states cannot construct the main
      assistant runtime.
- [ ] Gemini onboarding and secure local key storage work without a bundled
      secret file; any historical `config/api_keys.json` value is migrated once,
      verified in the native store, and removed from plaintext rather than used
      as an ongoing fallback.
- [ ] Core voice, audio, dashboard and required macOS permission paths are manually tested.

## Mobile admin gate

- [ ] Ordinary remote-control users have no Admin Mode link; every admin record
      and evidence request still requires the separate MFA session boundary.
- [ ] The Admin PWA service worker cache contains public shell files only and no
      `/api/`, session, evidence, customer, release or audit response.
- [ ] Background privacy cleanup, logout/remote revoke, token expiry, offline
      denial, external-navigation blocking and 390/412px layouts pass on real
      target browsers over HTTPS.
- [ ] A payment created after the visible queue baseline produces the expected
      in-browser pending banner; contract tests alone are not delivery evidence.
- [ ] Payment decisions, MFA security events and device replacement history are
      available through one authorized, durable product-wide audit view. This is
      currently an **internal gap**.
- [ ] Native iOS/Android and push status remain `not_available` until their own
      security and real-device verification gates pass.

## Artifact verification

- [ ] App and DMG byte sizes and SHA-256 digests are recorded.
- [ ] Release manifest identity matches product, version, build, platform and architecture.
- [ ] Artifact signature verifies with a pinned release public key.
- [ ] Developer ID signatures pass `codesign --verify --deep --strict` on the final artifact.
- [ ] Hardened runtime and required entitlements are reviewed.
- [ ] Apple notarization succeeds and the ticket is stapled.
- [ ] `spctl` and staple validation pass on the exact distributed DMG/app.
- [ ] The final app is signed before the final DMG is built/signed; an audited
      executor parses an `Accepted` notarization result and verifies the stapled
      final bytes. The repository currently has a non-executing planner only;
      `--execute` is `not_available`.

## Install and clean-device verification

- [ ] A clean supported Mac downloads and opens the final DMG.
- [ ] Dragging `JARVIS.app` to Applications works.
- [ ] First launch passes Gatekeeper without bypass instructions.
- [ ] First launch shows the license/purchase/activation gate before Gemini key
      onboarding, then clear permission guidance only after entitlement succeeds.
- [ ] Optional integrations remain optional in Settings.
- [ ] Reboot/relaunch preserves settings, secure secrets and purchased-version access.

## Update and rollback verification

- [ ] Pending/review/rejected payments cannot download or install an update.
- [ ] Approved exact-version entitlement authorizes only the bound active device and correct artifact target.
- [ ] Exact target entitlement is re-read locally immediately before mutation;
      missing/corrupt/wrong-device authority blocks the adapter call.
- [ ] Full download length, digest, signature, source compatibility and monotonic build are verified before mutation.
- [ ] Old app and user data remain untouched through download and verification.
- [ ] Strict app-ZIP extraction rejects traversal, case collisions, special files,
      archive bombs and escaping/cyclic/dangling symlinks while preserving only
      safe in-bundle framework links.
- [ ] Atomic replacement and bounded post-install health check pass.
- [ ] Injected install/health failures restore and re-verify the last-known-working app.
- [ ] Interrupted journal recovery completes before any license/Gemini/runtime
      startup; an unresolved recovery blocks the app fail-closed.
- [ ] The fixed production helper passes owner/mode/inode, Team ID, designated
      requirement, `codesign`, `spctl` and stapler checks on the final bytes.
- [ ] A declined update leaves the purchased older version operating offline.

## Publication gate

- [ ] Admin-only private artifact storage and payment-evidence access are verified.
- [ ] Release is published only after all platform artifacts and evidence are attached.
- [ ] Download authorization and audit events are verified in production configuration.
- [ ] Rollback/support instructions and known limitations are documented.
- [ ] Production HTTPS/domain/reverse-proxy policy, edge limiting, monitoring,
      owner-only secrets and a stopped-service backup/fresh-target restore drill
      are verified on the real deployment. Local recipes are not sufficient.
- [ ] Final release approval explicitly records that all commercial gates are cleared.
