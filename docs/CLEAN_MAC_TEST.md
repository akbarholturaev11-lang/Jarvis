# JARVIS Clean Mac Release Test

## Overall status

**NOT RUN**

No clean-Mac artifact evidence has been collected. No checklist item below is a
PASS, and this document must not be used to infer signing, notarization,
Gatekeeper, install, activation, offline, update, rollback, permission, or
uninstall success.

Execution is currently blocked because the final Developer-ID-signed,
notarized, stapled app/DMG and the audited production updater helper do not
exist. See [`PACKAGING.md`](PACKAGING.md) and
[`UPDATE_ROLLBACK.md`](UPDATE_ROLLBACK.md).

## Candidate identity

| Field | Evidence |
| --- | --- |
| Test date/time (UTC) | |
| Operator | |
| Clean Mac model/architecture | |
| macOS version/build | |
| Fresh device or fresh standard-user account | |
| Source commit | |
| Product semantic version/build | |
| Bundle identifier | |
| DMG filename | |
| DMG byte size | |
| DMG SHA-256 | |
| App tree/bundle digest | |
| Developer ID identity/Team ID | |
| Apple notarization request ID | |
| Backend origin/config version | |
| Evidence directory/archive | |

## Preconditions

- [ ] Legal/license and branding gates are documented as cleared for this exact
      candidate.
- [ ] Candidate was built from the recorded commit in a controlled isolated
      Python 3.12 build environment.
- [ ] Complete dependency lock/provenance and build logs are retained without
      secrets.
- [ ] App nested code and outer bundle carry the expected Developer ID identity.
- [ ] DMG was rebuilt from the signed app and separately signed.
- [ ] Apple notarization returned an explicitly parsed accepted result.
- [ ] Staple validation and final `codesign`/`spctl` verification passed before
      transfer to the clean Mac.
- [ ] Production backend uses the recorded trusted HTTPS origin and synthetic
      test customer/payment data.
- [ ] Production updater helper passed independent review and is signed,
      notarized, stapled and pinned to the expected Team ID/requirement.
- [ ] Test secrets are isolated, revocable and absent from argv, screenshots and
      logs.

## Artifact custody and first launch

- [ ] Transfer the exact candidate through the intended release channel.
- [ ] Recompute and match DMG size and SHA-256 before mounting.
- [ ] Confirm `hdiutil verify` succeeds.
- [ ] Record `codesign --verify --deep --strict --verbose=2` for the app.
- [ ] Record `spctl --assess --type execute --verbose=4` for the app.
- [ ] Record `spctl --assess --type install --verbose=4` for the DMG.
- [ ] Record `xcrun stapler validate` for the app and DMG.
- [ ] Mount the DMG and drag the app to `/Applications` as a standard user.
- [ ] Confirm no repository checkout, Python, pip, Terminal or project `.venv`
      is required.
- [ ] Confirm first launch occurs through Finder and Gatekeeper shows no bypass
      or unidentified-developer warning.
- [ ] Confirm product/version/build and bundle identifier match the candidate.
- [ ] Confirm the application bundle remains unchanged and all writable data
      goes to Application Support/Caches/Logs locations.
- [ ] Confirm no API key, personal memory, private config, signing material,
      database or payment evidence is present in the bundle.

## License, purchase and credential onboarding

- [ ] With no entitlement, confirm the frozen license gate appears before
      Gemini onboarding or assistant runtime construction.
- [ ] Confirm release notes, server-controlled minor-unit price/currency and
      payment instructions render in English and Russian.
- [ ] Upload a synthetic valid payment image; confirm pending state and private
      evidence access only.
- [ ] Reject once with a reason, resubmit, approve once, and confirm duplicate
      approval does not issue duplicate authority.
- [ ] Confirm the client receives only the exact purchased version entitlement.
- [ ] Enter a valid Gemini key once and confirm macOS Keychain save produces no
      Terminal or interactive password prompt.
- [ ] Confirm invalid Gemini key reports validation failure separately from
      secure-storage failure.
- [ ] Quit and relaunch; confirm license and Gemini onboarding do not reappear.
- [ ] Disconnect the network and confirm the purchased exact version continues
      to reach its core local runtime.
- [ ] Publish a differently priced newer semantic version and confirm the older
      purchased version remains usable without receiving the new entitlement.

## Permissions and runtime

- [ ] Exercise microphone permission and verified audio input/output.
- [ ] Exercise camera permission only after an explicit user action.
- [ ] Exercise Screen Recording and Accessibility paths with honest denied/not
      available results before permission is granted.
- [ ] Confirm no action reports success without its required verification.
- [ ] Exercise app restart, Mac restart and offline restart.
- [ ] Review Console/application logs for secrets, cookies, grants, API keys,
      TOTP/recovery material, payment evidence paths and private URLs.
- [ ] Confirm ordinary operation does not launch Terminal or depend on a system
      Python installation.

## Signed update and rollback

- [ ] Start from an entitled signed/notarized version A in `/Applications`.
- [ ] Purchase and approve exact signed/notarized version B.
- [ ] Confirm B download grant is not present in a URL or log.
- [ ] Confirm signed metadata, product, source/target, size, hash and signature
      verification complete before mutation.
- [ ] Confirm the production helper performs a bounded safe shutdown and creates
      a private verified backup/journal before replacement.
- [ ] Confirm A→B atomic replacement, fresh-nonce B health proof and final
      installed identity.
- [ ] Repeat with forced B health failure; confirm verified rollback to A.
- [ ] Interrupt download; confirm A is unchanged and retry is bounded.
- [ ] Interrupt install/helper execution; confirm next launch recovers or blocks
      startup with `rollback_required`, never false success.
- [ ] Exercise a controlled power-loss scenario and retain the recovery journal
      evidence.
- [ ] Attempt downgrade, repeated install, bad hash/signature, wrong entitlement,
      traversal/symlink archive and replaced/unsigned helper; confirm fail-closed
      behavior.
- [ ] Confirm user data, Keychain items, settings, logs and personal memory are
      preserved through success and rollback.

## Admin PWA and backend dependency

- [ ] Use the production trusted HTTPS origin; confirm no certificate bypass is
      enabled.
- [ ] Complete admin password+TOTP login, evidence review, approve/reject,
      customer/license/device reads and remote session revoke.
- [ ] Confirm Secure/HttpOnly/SameSite=Strict cookie, CSRF enforcement and
      external-navigation blocking in the real browser.
- [ ] Confirm offline/background Admin PWA exposes no previously loaded private
      evidence or customer records.
- [ ] Restart the backend during a bounded client retry and confirm no duplicate
      payment, entitlement or grant consumption.
- [ ] Verify audit records for payment, MFA, password/session and device
      replacement; record the known lack of a unified audit projection until it
      is implemented.

## Uninstall and residue review

- [ ] Uninstall the application without deleting user data by default.
- [ ] Verify the documented optional user-data removal procedure separately.
- [ ] Confirm no privileged helper, launch item, mount, temporary update tree or
      stale private backup remains unexpectedly.
- [ ] Reinstall the same signed version and confirm the documented credential
      and entitlement behavior.
- [ ] Revoke/destroy all synthetic test credentials, sessions, grants and
      payment evidence after evidence retention is complete.

## Evidence record

| Check | Result | Evidence path or identifier | Reviewer |
| --- | --- | --- | --- |
| Artifact hash/signature/notarization | | | |
| Gatekeeper install and first launch | | | |
| No Python/Terminal/repository dependency | | | |
| License/purchase/exact-version gate | | | |
| Keychain no-prompt restart persistence | | | |
| Offline and permissions | | | |
| Signed A→B update and health proof | | | |
| Forced failure and verified rollback | | | |
| Interruption/power-loss recovery | | | |
| Admin/backend dependency | | | |
| Secret/log/bundle inspection | | | |
| Uninstall/residue review | | | |

## Final decision

| Field | Value |
| --- | --- |
| Overall result (`PASS` / `FAIL` / `NOT RUN`) | `NOT RUN` |
| Production-verified | `false` |
| Distribution approved | `false` |
| Blocking finding IDs | |
| Reviewer sign-off | |
| Sign-off date/time (UTC) | |

Do not change the final result to `PASS` unless every applicable checklist item
has a retained evidence reference, all Critical/High findings are resolved, the
exact candidate is unchanged, and legal/release approval is recorded separately.
