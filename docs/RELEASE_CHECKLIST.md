# JARVIS Release Checklist

No release is customer-ready unless every applicable item is checked with saved
evidence.  A local unsigned DMG does not satisfy this checklist.

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
- [ ] Gemini onboarding and secure local key storage work without a bundled secret file.
- [ ] Core voice, audio, dashboard and required macOS permission paths are manually tested.

## Mobile admin gate

- [ ] Ordinary remote-control users have no Admin Mode link; every admin record
      and evidence request still requires the separate MFA session boundary.
- [ ] The Admin PWA service worker cache contains public shell files only and no
      `/api/`, session, evidence, customer, release or audit response.
- [ ] Background privacy cleanup, logout/remote revoke, token expiry, offline
      denial, external-navigation blocking and 390/412px layouts pass on real
      target browsers over HTTPS.
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

## Install and clean-device verification

- [ ] A clean supported Mac downloads and opens the final DMG.
- [ ] Dragging `JARVIS.app` to Applications works.
- [ ] First launch passes Gatekeeper without bypass instructions.
- [ ] Onboarding asks only for Gemini key plus clear permission guidance.
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
- [ ] Final release approval explicitly records that all commercial gates are cleared.
