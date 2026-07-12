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

## Build verification

- [ ] Foundation and full regression tests pass on Python 3.12.
- [ ] PyInstaller dependency/native-library report is reviewed.
- [ ] `JARVIS.app` launches without Terminal, Python or dependency installation.
- [ ] App reports the exact expected product/version/build mechanically.
- [ ] Gemini onboarding and secure local key storage work without a bundled secret file.
- [ ] Core voice, audio, dashboard and required macOS permission paths are manually tested.

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
- [ ] Full download length, digest, signature, source compatibility and monotonic build are verified before mutation.
- [ ] Old app and user data remain untouched through download and verification.
- [ ] Atomic replacement and bounded post-install health check pass.
- [ ] Injected install/health failures restore and re-verify the last-known-working app.
- [ ] A declined update leaves the purchased older version operating offline.

## Publication gate

- [ ] Admin-only private artifact storage and payment-evidence access are verified.
- [ ] Release is published only after all platform artifacts and evidence are attached.
- [ ] Download authorization and audit events are verified in production configuration.
- [ ] Rollback/support instructions and known limitations are documented.
- [ ] Final release approval explicitly records that all commercial gates are cleared.
