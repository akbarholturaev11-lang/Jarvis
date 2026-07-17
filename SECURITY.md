# JARVIS Security Policy

## Current security status

JARVIS is a pre-release productization project. Security controls are
implemented and locally tested across the desktop client, product backend,
admin PWA, payment flow, exact-version entitlement flow, and development-only
update transaction. **No product component is production-verified.** There is
no signed/notarized customer artifact and no public production deployment.

The commercial and distribution decision is **NO-GO** until every external and
legal gate in
[`docs/PRODUCT_RELEASE_CONTRACT.md`](docs/PRODUCT_RELEASE_CONTRACT.md) is cleared.
Local test success must never be presented as customer-release approval.

This policy uses the following terms:

- **implemented** — the code or interface exists;
- **enforced** — the relevant runtime path fails closed when its invariant is
  violated;
- **tested locally** — automated tests or a recorded local smoke test exercised
  the behavior;
- **production-verified** — final deployment/artifact evidence exists under real
  production conditions;
- **`not_available`** — the operation is deliberately unavailable and must not
  report success;
- **internal blocker** — repository work remains;
- **external blocker** — operator infrastructure, credentials, hardware, or an
  external service is required;
- **legal/license blocker** — rights or licensing approval is required.

## Supported versions

There is currently no supported customer release and no security-support SLA.
Only the current maintained repository state is evaluated. Historical local
builds, ad-hoc-signed artifacts, and development deployment recipes must not be
treated as supported binaries or production services.

When a release is eventually approved, this section must be replaced with an
explicit supported-version table, security-update policy, and end-of-support
dates before distribution.

## Reporting a vulnerability

Report security issues privately to the repository owner through an already
trusted private channel associated with this repository. Do not include exploit
details, credentials, payment evidence, private URLs, device identifiers, or
customer data in a public issue. If no private channel is available, publish
only a request for a private contact path, without technical details.

A useful private report includes:

- affected commit, local build, endpoint, or platform;
- concise impact and preconditions;
- minimal reproduction steps using synthetic data;
- whether a secret or private object may have been exposed;
- suggested mitigation, if known.

Do not test against systems or data you do not own or have explicit permission
to assess. This personal pre-release project has no bug-bounty program and makes
no response-time promise. The owner should preserve evidence, rotate affected
material, revoke sessions/grants, and avoid publishing a fix before exposed
secrets and artifacts are contained.

## Security boundaries and implemented controls

| Area | Current control | Status |
| --- | --- | --- |
| Product authority | Ed25519-signed, exact-semantic-version entitlement certificates bound to a generated device identity | Implemented, enforced, tested locally; not production-verified |
| Gemini credential | OS secure-store authority, read-back verification, redacted errors, idempotent legacy plaintext migration | Implemented and tested locally; real host smoke remains required on each supported OS |
| License gate | Frozen runtime fails closed before Gemini onboarding/runtime without a valid exact-version entitlement | Implemented, enforced, tested locally; clean frozen customer run not performed |
| Payment evidence | Bounded decoded image, sanitized re-encoding, private opaque object key, authenticated admin reads, no public evidence URL | Implemented, enforced, tested locally; production storage/retention not verified |
| Payment replay | Durable client idempotency, one-time device grants, duplicate submission/approval controls | Implemented, enforced, tested locally |
| Admin authentication | Password + RFC 6238 TOTP, encrypted TOTP secret, single-use hashed recovery codes, replay defence and bounded attempts | Implemented, enforced, tested locally; real deployment enrollment smoke outstanding |
| Admin session | Secure/HttpOnly/SameSite=Strict cookie, CSRF token, idle/absolute expiry, revoke/revoke-all, recent re-authentication | Implemented, enforced, tested locally; no production browser evidence |
| Mobile admin | Separate same-origin PWA boundary, no browser-stored auth material, API cache bypass, private Blob URL cleanup, external-navigation block | Implemented and tested locally in Chromium; trusted-TLS iOS/Android smoke not performed |
| Backend edge | HTTPS-only policy, HSTS, trusted hosts, explicit proxy trust, optional admin CIDR allowlist, app and nginx rate limits | Implemented, enforced, tested locally; real domain/TLS/server not deployed |
| Observability | Structured request logs, bounded request IDs, secret redaction, optional private metrics endpoint | Implemented and tested locally; production log pipeline not audited |
| Release download | Signed metadata, pinned public keys, exact identity/size/hash checks, private staging, header-carried single-use grant | Implemented, enforced, tested locally |
| Update transaction | Strict archive extraction, downgrade defence, private backup, durable journal, fresh-nonce health proof, verified development rollback | Implemented and tested locally; production helper/install is `not_available` |
| Packaging | Self-contained unsigned macOS app/DMG build and secret-exclusion checks | Implemented and tested locally; signing execution, notarization and clean-Mac proof are `not_available`/not run |
| Deployment backup | Stopped-service snapshot, schema/integrity/hash verification, fresh-target atomic restore | Implemented and tested locally on POSIX; manifest is not authenticated and native Windows mutation is `not_available` |

Detailed readiness and residual-risk records are maintained in:

- [`THREAT_MODEL.md`](THREAT_MODEL.md)
- [`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md) (release-readiness matrix)
- [`docs/E2E_PRODUCT_VALIDATION.md`](docs/E2E_PRODUCT_VALIDATION.md)
- [`docs/ADMIN_MFA.md`](docs/ADMIN_MFA.md)
- [`docs/PACKAGING.md`](docs/PACKAGING.md)
- [`docs/UPDATE_ROLLBACK.md`](docs/UPDATE_ROLLBACK.md)
- [`docs/PRODUCTION_DEPLOYMENT.md`](docs/PRODUCTION_DEPLOYMENT.md)

## Secret and private-data handling

Never commit, print, attach to an issue, or place in a subprocess argument:

- API keys, passwords, session cookies, CSRF tokens, recovery codes or TOTP
  secrets;
- entitlement/release private keys, activation peppers, MFA master keys or
  notarization credentials;
- real product/payment configuration, database values, private object links or
  customer payment evidence;
- `config/api_keys.json`, `memory/long_term.json`, local device/Zerno config, or
  production environment files.

Production material belongs in an operator-controlled secret store and is
referenced through owner-only files or OS-native stores. Release-signing private
material is an offline boundary and must not be mounted into the online backend.
Artifact grants belong in the `X-Artifact-Grant` header and must not be logged or
placed in URLs. If secret exposure is suspected, stop distribution/deployment,
preserve a sanitized incident timeline, revoke relevant sessions/grants, rotate
the affected key using the documented overlap constraints, and re-issue any
affected one-time credentials.

## Mandatory security verification before a commit or release

For changes within scope, run Python compilation, targeted positive and negative
tests, the full repository suite, `git diff --check`, and a secret-oriented diff
review. Cross-platform code must test macOS, Windows and Linux routing, including
truthful `not_available` behavior. High-risk release candidates additionally
require the Stage 9 harness and the evidence checklist in
[`docs/CLEAN_MAC_TEST.md`](docs/CLEAN_MAC_TEST.md).

The latest recorded local Stage 9 evidence on 2026-07-17 is 13/13 evidence
groups passing (262 tests + 279 subtests), followed by 813 tests + 527 subtests
passing in the full repository suite. The scenario result remained 21 `pass` and
9 `not_available`; it did not set `production_ready` or `production_verified`.
See [`docs/E2E_PRODUCT_VALIDATION.md`](docs/E2E_PRODUCT_VALIDATION.md) for the
exact evidence map and limitations.

## Open blockers

### Internal blockers

- Implement and independently audit the production macOS helper protocol,
  safe-shutdown flow, final app-before-DMG signing sequence, notarization-result
  parsing, and final verification.
- Provide Windows and Linux distributables and atomic update/rollback helpers;
  both remain `not_available`.
- Add an authenticated backup manifest or enforce equivalent independently
  managed immutable/authenticated backup storage.
- Implement automated payment-evidence retention/deletion and a unified,
  durable product-wide audit projection.
- Implement transactional MFA master-key rotation; the current operation is
  honestly `not_available`.
- Hash-lock transitive backend/build dependencies and pin container base-image
  digests before supply-chain sign-off.
- Add shared PostgreSQL/session/grant/rate-limit/object-store implementations
  before running more than one backend process.

### External blockers

- Real Apple Developer ID certificate, Team ID and notarization credentials.
- Final signed/notarized/stapled artifact and clean-Mac Gatekeeper evidence.
- Registered production domain, valid TLS certificate, provisioned server/VPN,
  managed production secrets and private storage.
- Representative trusted-TLS iOS and Android browser smoke tests.
- Real-host secure-store smoke tests on supported macOS, Windows and Linux
  environments.

### Legal/license blockers

- Commercial permission or a rights-clean replacement for upstream CC BY-NC
  material.
- A documented lawful PyQt6 distribution model.
- Cleared JARVIS/product naming, icon, copy and bundled-asset rights.
- A final product identity and bundle identifier supported by those rights.

Until the internal, external, and legal/license blockers applicable to a target
are cleared with retained evidence, that target remains pre-release and must not
be described as secure for production or ready for sale.
