# Product Release End-to-End Validation

## Scope and truth boundary

The Stage 9 harness maps the required 30-step product scenario to fixed pytest
evidence groups and writes a machine-readable JSON report plus a reviewable
Markdown matrix. It is a deterministic **local validation harness**, not a
production certification tool.

Every scenario contains two independent fields:

- `local_evidence_status`: whether all mapped local automated tests passed;
- `status`: the honest scenario result after known implementation, production,
  external, and legal blockers are applied.

Therefore a synthetic macOS A-to-B update can have
`local_evidence_status=pass` while the final install scenario remains
`status=not_available`. A failed test always changes the final scenario to
`fail`, even when that scenario already has an availability blocker.

The harness never emits `production_ready=true` or
`production_verified=true`. The closed status vocabulary is:
`pass`, `fail`, `not_available`, and `not_run`.

Latest local run on 2026-07-17:

- evidence groups: **13/13 PASS** (`262` tests + `279` subtests);
- scenario matrix: **21 `pass`**, **9 `not_available`**, **0 `fail`**,
  **0 `not_run`**;
- external/legal gates: **7 `not_available`**;
- full repository suite after the harness changes: **813 tests + 527
  subtests PASS** (one pre-existing Starlette/httpx deprecation warning).

The nine unavailable scenario rows are 1, 7, 15, 23–27 and 30. Their mapped
local evidence passed, but exact browser notification delivery, the clean
frozen build, real Gemini runtime, production updater helper and unified audit
conditions did not become true.

## Run

From the repository root:

```bash
.venv/bin/python scripts/run_product_release_e2e.py
```

Reports are written atomically with private POSIX file modes to the ignored
directory:

```text
build/e2e-product-release/report.json
build/e2e-product-release/report.md
```

Useful bounded modes:

```bash
# Validate the catalog and create a not_run report without invoking pytest.
.venv/bin/python scripts/run_product_release_e2e.py --plan-only

# Execute only selected scenario evidence. Shared groups run once.
.venv/bin/python scripts/run_product_release_e2e.py \
  --scenario 6 --scenario 8 --scenario 10
```

Pytest is invoked with an argv list and `shell=False`. Scenario selection never
becomes a command fragment; it only selects fixed catalog entries. Each evidence
group has a bounded timeout. Credential-bearing environment variables and
pytest injection knobs are removed from the child environment. Captured
stdout/stderr is used only to classify the closed pytest summary and is never
persisted in the report. Any skip, expected failure or deselection makes the
group `not_available`; an unexpected pass or a successful run with no passing
evidence is a failure.

## Executable evidence groups

The catalog deliberately deduplicates test files across groups. When several
scenario steps share `initial_purchase`, for example, that group runs once.

| Group | Local evidence |
| --- | --- |
| `license_gate` | no-license gate, production override rejection, bilingual gate UI |
| `initial_purchase` | fresh purchase, valid PNG in private object storage, real TOTP enrollment, review, reject, resubmit, idempotent approval, signed polling |
| `payment_validation` | MIME/size/corruption limits, metadata removal, symlink and path boundaries |
| `mobile_admin` | isolated responsive Admin PWA, authenticated projections, polling contract, background cleanup |
| `admin_mfa` | RFC 6238, replay defence, recovery codes, session revoke, CSRF, rate limits, audit |
| `activation_offline` | signed activation, device/version mismatch, offline exact-version use |
| `desktop_runtime` | durable initial/update payment request, restart retry and explicit install boundary |
| `secure_credentials` | Gemini validation, secure-store CRUD, restart, migration, no prompt/no leak, macOS/Windows/Linux routing |
| `paid_update` | paid exact-version update, grant handling, download, retry and replay boundaries |
| `signature_security` | canonical signed manifests, pinned keys, hash/signature corruption |
| `updater_transaction` | development A-to-B install, nonce health proof, interruption recovery, rollback and attacks |
| `device_replacement` | atomic replacement, replay rejection, old-device future-operation denial |
| `audit_projection` | persistent payment decisions and bounded authenticated admin projections |

The upgraded fresh-purchase E2E uses a structurally valid PNG fixture and the
real POSIX `LocalPrivateObjectStore`; it no longer accepts arbitrary bytes
through an in-memory evidence stub. Admin approval occurs only after an actual
TOTP enrollment, logout, and password+TOTP login. This is still an in-process
HTTPS TestClient flow, not evidence of a public deployment. On a host where the
hardened local backend evidence adapter is unavailable, the integration test is
skipped and the harness reports any partially or wholly skipped group as
`not_available`, never as PASS.

## Thirty-scenario map

The generated `report.md` is the authoritative PASS/FAIL matrix for a specific
run. The fixed availability expectations below explain why some rows cannot be
reported as PASS even when their local tests are green.

| # | Scenario | Evidence group(s) | Availability boundary |
| ---: | --- | --- | --- |
| 1 | Fresh user opens Jarvis | `license_gate` | `not_available` until a frozen build is exercised on a clean customer Mac |
| 2 | No license is present | `license_gate` | local automated evidence |
| 3 | Purchase screen is shown | `license_gate`, `initial_purchase` | local automated evidence |
| 4 | Release notes and server price are shown | `license_gate`, `initial_purchase` | server fields plus desktop render contract |
| 5 | Payment instructions are shown | `license_gate`, `initial_purchase` | server fields plus desktop render contract |
| 6 | Screenshot uploads privately | `initial_purchase`, `payment_validation` | local automated evidence |
| 7 | Admin sees pending notification | `initial_purchase`, `mobile_admin` | `not_available`: source/polling contracts pass, but a new post-baseline payment is not delivered into a browser banner by this harness |
| 8 | Admin logs in with MFA | `initial_purchase`, `admin_mfa` | local TOTP evidence |
| 9 | Admin reviews evidence | `initial_purchase`, `admin_mfa` | local authenticated evidence |
| 10 | Admin approves | `initial_purchase` | local idempotency evidence |
| 11 | Exact-version entitlement | `initial_purchase`, `activation_offline` | local cryptographic evidence |
| 12 | Client observes approval | `initial_purchase` | local polling evidence |
| 13 | Activation | `activation_offline` | local signed-certificate evidence |
| 14 | Gemini secure storage | `secure_credentials` | adapter/contract tests; real host Keychain smoke is separate |
| 15 | Jarvis runtime starts | `license_gate`, `secure_credentials` | `not_available` without a real Gemini Live/operator credential run |
| 16 | Restart preserves state | `activation_offline`, `secure_credentials`, `desktop_runtime` | local persistence evidence |
| 17 | Offline old version works | `activation_offline` | local signed-cache evidence |
| 18 | New paid version exists | `activation_offline`, `paid_update` | local exact-version contract evidence |
| 19 | Old version remains usable | `activation_offline` | local no-kill-switch evidence |
| 20 | User buys update | `paid_update`, `desktop_runtime` | desktop submission + restart-idempotency evidence |
| 21 | Update downloads | `paid_update` | local private staged download evidence |
| 22 | Signature/hash verify | `paid_update`, `signature_security` | local cryptographic evidence |
| 23 | Install | `updater_transaction` | `not_available` in frozen production without signed/audited helper |
| 24 | Health check | `updater_transaction` | production helper execution unavailable |
| 25 | Update success | `updater_transaction` | development transaction only |
| 26 | Forced failure | `updater_transaction` | development transaction only |
| 27 | Rollback | `updater_transaction` | development transaction only |
| 28 | Device replacement | `device_replacement` | local backend evidence |
| 29 | Old device loses future server operations | `device_replacement`, `activation_offline` | local backend evidence; installed offline copy is deliberately not remotely killed |
| 30 | Complete audit log | `audit_projection`, `admin_mfa`, `device_replacement` | `not_available`: payment, MFA, and device events are not yet one unified view |

## Negative, interruption, and replay coverage

The mapped suites cover invalid MIME, oversized/corrupt images, duplicate
submission/approval, lost response and restart retry, network failure, invalid
or corrupt certificate, wrong device/version, TOTP replay, device-grant replay,
bad signature/hash, interrupted download/install, stale health nonce, symlink
and archive traversal, failed rollback, repeated install, and device replacement
replay.

These are deterministic in-process or filesystem fault injections. They do not
prove behavior during a real power loss, public-network outage, reverse-proxy
restart, or signed privileged-helper crash on a customer machine.

## Evidence that remains outside this harness

The generated report always lists the following as `not_available` rather than
PASS:

- upstream CC BY-NC commercial permission or a rights-clean replacement;
- a cleared PyQt6 distribution model;
- product-name, icon, copy, and bundled-asset rights;
- real Apple Developer ID signing, notarization, stapling, and Gatekeeper proof;
- final signed DMG validation on a clean Mac;
- an operator-owned production domain/TLS/server deployment;
- representative iOS/Android browser smoke over HTTPS.

Additional internal gaps remain explicit: the frozen macOS privileged helper is
disabled pending audit/signing, and product-wide audit events do not yet have a
single durable query surface. Native mobile clients and background push remain
`not_available`; the implemented mobile administration surface is a responsive
PWA with visible/online in-app polling.

## Local Chromium manual smoke — 2026-07-17

A real Playwright Chromium session exercised the assembled backend over
`https://localhost` with a self-signed development certificate and a `390×844`
viewport. Bootstrap credentials were read from an owner-only file by a temporary
helper; no password, TOTP secret, session cookie or CSRF value entered a command
argument or log. The helper completed real MFA enrollment and wrote only an
owner-only browser storage-state file, which was deleted with every temporary
key/cookie after the run.

Observed results:

- unauthenticated `/admin/` showed the login boundary; after the MFA session
  cookie was loaded, the UI restored an authenticated **read-only** session and
  correctly required a fresh login before issuing a new CSRF mutation token;
- English and Russian titles, navigation, status and queue copy rendered;
- the 390×844 layout had no horizontal document overflow;
- a real click on an injected external HTTPS link kept the page on the JARVIS
  origin and showed the Russian “external navigation blocked” status;
- browser offline emulation displayed the bilingual data-unavailable notice and
  did not manufacture write access;
- the final responsive screenshot was visually inspected from the top of the
  page; no overlap or clipped primary control was observed.

The console retained one expected error: Chromium refused to install the service
worker through a self-signed certificate. `ignoreHTTPSErrors` permits page
navigation but does not turn an untrusted development certificate into a valid
service-worker security origin. This is not counted as production PWA proof.
Real trusted TLS plus representative iOS and Android browsers remain an external
`not_available` gate.

## Manual follow-up

After local automated evidence is green, an operator still needs to record:

1. real Keychain save/restart smoke without Terminal prompts;
2. a frozen license-gate → purchase → Gemini onboarding run;
3. Admin PWA use in real iOS and Android browsers over trusted HTTPS;
4. a signed/notarized A-to-B update and forced rollback on a clean Mac;
5. backend restart and network interruption against the real deployment;
6. confirmation that legal/commercial rights gates are cleared.

None of those checks may be inferred from this harness output.

## Related readiness records

- [Product release contract and canonical status matrix](PRODUCT_RELEASE_CONTRACT.md)
- [Packaging readiness](PACKAGING.md)
- [Clean Mac test record](CLEAN_MAC_TEST.md)
- [Security policy](../SECURITY.md)
- [Threat model](../THREAT_MODEL.md)
