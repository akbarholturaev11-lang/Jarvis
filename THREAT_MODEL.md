# JARVIS Product Threat Model

## Status and scope

This threat model covers the product-release system as implemented in the
repository on 2026-07-17: the frozen desktop gate, secure credential layer,
device identity, exact-version entitlement and payment flow, product backend,
web/mobile Admin PWA, release download, macOS development update transaction,
packaging pipeline, deployment recipes, and backup/restore tooling.

It is a pre-production model. Controls described as tested were exercised only
by local automated tests or recorded local smoke tests. **No component is
production-verified.** The status terms are defined in
[`SECURITY.md`](SECURITY.md).

Excluded from a claim of protection are a host already controlled by root or the
logged-in OS user, a stolen unlocked authenticator, maliciously modified source
before a trusted build, and infrastructure outside the repository. These remain
relevant operational risks; exclusion means the application alone cannot
contain them.

## Security objectives

1. Only a verified admin approval grants an entitlement for the exact semantic
   version purchased.
2. A device must prove possession of its generated private key; raw hardware
   identifiers are not identity.
3. Declining or losing network access for a newer release must not disable a
   legitimately purchased older version.
4. Payment evidence, credentials, private keys, sessions, grants and personal
   data must not become public URLs, logs, command arguments, repository files,
   or bundle resources.
5. Admin actions require the intended authenticated subject, MFA, CSRF
   protection, bounded sessions and auditable decisions.
6. Release metadata and bytes must pass pinned signature, identity, size and
   digest checks before installation authority exists.
7. Installation success requires exact target identity plus a fresh health proof;
   otherwise the previous version must be independently preserved or restored.
8. Unsupported platform or production operations return an honest
   `not_available`/failure status and never synthetic success.

## Assets

### Client assets

- Gemini API key in the OS secure store;
- generated device private key and its server-bound public identity;
- signed exact-version entitlement cache;
- encrypted durable payment retry data;
- personal memory, settings, logs and user files outside the app bundle;
- downloaded release artifact and update journal/backup.

### Server and operator assets

- entitlement-signing private key and pinned public-key sets;
- offline release-signing private key and Apple notarization identity;
- activation pepper, session secret and admin MFA encryption key;
- salted admin password hashes, encrypted TOTP state and recovery-code digests;
- accounts, licenses, devices, releases, payments, entitlements and audit rows;
- private payment evidence and private release artifacts;
- backend databases, backups, metrics and structured logs;
- production domain, TLS keys, proxy/VPN policy and deployment configuration.

## Threat actors

- unauthenticated internet attacker;
- authenticated customer attempting entitlement, device or grant replay;
- attacker controlling an untrusted network or spoofing proxy headers;
- malware or another local user attempting to read or replace private files;
- compromised administrator session or authenticator;
- malicious or mistaken operator with filesystem/deployment access;
- compromised dependency, CI runner, build host, artifact store or signing key;
- attacker providing a crafted image, archive, manifest, request ID, host header,
  redirect or path;
- physical attacker with an unlocked or fully compromised client/server host.

## Trust boundaries and data flow

1. **Desktop process ↔ OS secure store.** Secrets cross only the native secure
   storage adapter boundary. Legacy plaintext migration is a bounded,
   idempotent transition and the plaintext source is removed/quarantined only
   after verified storage.
2. **Desktop/PWA ↔ HTTPS edge.** Price, payment, activation and release requests
   cross an untrusted network. TLS, host validation and explicit proxy trust are
   deployment requirements.
3. **Reverse proxy ↔ ASGI process.** Forwarded client/scheme headers are trusted
   only from configured direct peers. The backend is intended to remain
   unpublished behind loopback or a private container network.
4. **ASGI process ↔ private state.** SQLite stores, evidence, artifacts and
   owner-only secret files are one host trust zone. The current design permits
   exactly one backend process.
5. **Admin browser ↔ admin API.** The PWA is a public shell; authentication and
   authorization remain server-side. Cookies are HttpOnly and mutations require
   an in-memory CSRF token.
6. **Build/signing zone ↔ online service.** Release signing is an offline trust
   boundary. The online backend receives approved artifacts and public release
   verification material, never the release-signing private key.
7. **Updater coordinator ↔ production helper.** Artifact verification and
   entitlement checks occur before the mutation boundary. The production helper
   protocol is not implemented, so frozen mutation remains `not_available`.
8. **Live data ↔ backup storage.** SHA-256 manifests detect corruption but do
   not authenticate origin. Backup storage is therefore a trusted operator
   boundary until an independent signature/MAC exists.

## Threat analysis

| Threat | Implemented mitigation | Current evidence | Residual risk / status |
| --- | --- | --- | --- |
| License file tampering or wrong-version use | Pinned Ed25519 authority, exact product/version/build/device checks, frozen gate before Gemini | Positive/negative and offline local tests | Clean frozen customer run not performed; not production-verified |
| Copied license on another device | Per-install key pair and challenge proof; certificate bound to public device identity | Wrong-device and challenge-replay tests | A fully compromised original host can use its own key; physical-host compromise is not contained |
| Remote kill of a purchased version | Indefinite exact-version local certificate with no routine server expiry | Offline and old-version regression tests | Device replacement intentionally blocks future server operations but cannot revoke an already offline copy |
| Payment submission replay or duplicate approval | One-time grants, durable idempotency key+sanitized bytes, transactional approval | Retry, restart, duplicate, replay and idempotent-approval tests | Real public-network retry and backend-restart drill outstanding |
| Malicious, oversized or metadata-bearing screenshot | MIME/size/pixel bounds, decoded-image validation, sanitized re-encoding, private opaque object key | Corrupt/type/size/path/symlink and metadata tests | Production object store and automated retention are not implemented |
| Public or unauthorized evidence access | Authenticated admin route, no public URL, `no-store`, short-lived Blob URL and audit | API authorization and Chromium background-cleanup smoke | Trusted-TLS iOS/Android/browser-cache behavior not production-verified |
| Admin password compromise | Password + TOTP/recovery second factor, bounded login/MFA attempts, optional CIDR/VPN boundary | Valid/invalid/replay/rate-limit tests | Phishing, stolen unlocked authenticator and operator endpoint compromise remain |
| TOTP/recovery theft or replay | AES-256-GCM sealed TOTP state, used-step tracking, keyed hashed one-time recovery codes | Enrollment, replay, expiry-window and single-use tests | Transactional MFA master-key rotation is `not_available`; key compromise can require re-enrollment |
| Session fixation, theft or CSRF | Session rotation, Secure/HttpOnly/SameSite=Strict cookie, in-memory CSRF, idle/absolute expiry, revoke/revoke-all, recent auth | Session, cookie, CSRF and revoke tests | Production browser/proxy configuration and incident revoke drill outstanding |
| XSS/PWA cache leaks admin data | Static same-origin shell, no auth in browser storage, API/service-worker cache bypass, background cleanup, minimal/no JS bridge | Source contract tests and local Chromium smoke | CSP and service worker require real trusted TLS; no native wrapper exists |
| Host/proxy/header spoofing or plaintext API use | HTTPS-only policy, HSTS, trusted hosts, direct-peer allowlist for forwarded headers | Missing-config, HTTP, host and spoof tests | Real domain/certificate/proxy not deployed; stock Caddy recipe needs external edge rate limiting |
| Brute force or request-flood denial | Bounded app authentication limits, request/body bounds, nginx edge limits | Local tests and recipe checks | In-memory counters are single-process and not DDoS defence; multi-instance shared limiting absent |
| Secret or grant leakage through logs/URLs/argv | Redacted structured logs, safe request IDs, grants in `X-Artifact-Grant`, secure-store redaction, argv-only subprocesses | Secret redaction, no-prompt/no-leak and API URL tests | Production proxy/log/crash-report pipeline not audited |
| Manifest/artifact substitution | Pinned release public key, signed canonical manifest, exact identity/size/SHA-256, private staged copy, redirects forbidden | Signature/hash/corruption/grant tests | Signing-key custody and production artifact-store controls are external; no signed customer artifact exists |
| Downgrade, archive traversal, symlink or ZIP-bomb update | Monotonic build checks, strict bounded one-app archive, collision/link/path rejection, pinned descriptor | Adversarial update integration tests | Tests use synthetic bundles and development adapter only |
| Interrupted install, false health or rollback tamper | Durable journal, private tree-digested backup, same-volume atomic swap, fresh nonce, startup recovery block | A→B, forced failure, stale nonce, interruption and rollback tests | Real power-loss and signed privileged-helper behavior not available |
| Malicious/unsafe production helper | Fixed path and ownership/link/mode/Team-ID/signature/staple assessment; frozen adapter disabled | Missing/unsafe/unsigned/replaced-helper tests | Helper request/shutdown/privilege protocol is an internal blocker; production install is `not_available` |
| Build or dependency compromise | Isolated build venv, explicit PyInstaller data allowlist, bundle secret scan, unsigned CI artifact marked non-distributable | Packaging tests and one recorded local unsigned build | Transitive hash lock, immutable base images, trusted build-host attestation and production signing executor absent |
| Signing-key compromise | Release private key excluded from online backend; public key IDs and overlap rotation plan | Config/recipe and rotation tests | Real HSM/Keychain custody and incident drill are external; entitlement private key remains high-value online material |
| Database or evidence corruption | SQLite integrity/schema verification, no-follow storage, bounded reads, stopped-service backup and fresh-target restore | Migration, backup/restore corruption and path-attack tests | Single-host SQLite, no authenticated manifest, no production restore drill |
| Audit deletion or fragmented incident trail | Append-only decision/MFA/device records and authenticated bounded reads | Local persistence/query tests | One unified product-wide audit projection and retention/archive enforcement are internal blockers |
| Operator misconfiguration | Fail-closed runtime schema, owner/mode checks, `ops.validate_config`, exact proxy trust, one-process recipes | Missing/invalid config and deployment-recipe tests | Human secret-store, DNS/TLS, backup immutability and monitoring configuration remain external |

## Privacy and data minimization

- Payment evidence is private review material, not a public asset. The database
  stores only an opaque key and bounded metadata rather than image bytes or a
  public URL.
- Device identity uses a generated key pair rather than raw hardware serial
  numbers. Replacement records future authority changes without remotely
  deleting an offline purchased copy.
- The updater must not upload personal projects, memory, API keys, tokens or
  unrelated settings. Writable user data remains outside the application
  bundle.
- The Admin PWA service worker caches only an explicit public shell. Offline
  administration and background push are not implemented.
- Automated payment-evidence deletion is not implemented. Production deployment
  requires a documented retention window and audited deletion/archive job.

## Abuse cases that must remain fail-closed

- Missing/invalid production configuration, MFA manager, signing authority,
  entitlement or release trust root.
- Wrong product, semantic version, build, device, signature, hash, size, MIME,
  host, proxy peer, CSRF value, TOTP step, one-time grant or recovery code.
- Symlink, hard link, special file, unsafe owner/mode, path traversal, Unicode or
  case collision, recursive archive, stale health nonce or downgrade.
- Production macOS signing/install without the audited executor/helper.
- Windows/Linux packaging or update installation before native implementations
  and host evidence exist.

In every case the operation must return an explicit invalid/failed/
`not_available` result and must not consume unrelated grants, overwrite known-good
state, expose a secret, or claim success.

## Verification evidence and limitations

The Stage 9 harness maps the 30-step flow to 13 fixed evidence groups. Its
2026-07-17 local run recorded all 13 groups passing (262 tests + 279 subtests),
with 21 scenarios `pass`, 9 `not_available`, and no production-ready or
production-verified claim. The full suite subsequently recorded 813 tests + 527
subtests passing. See
[`docs/E2E_PRODUCT_VALIDATION.md`](docs/E2E_PRODUCT_VALIDATION.md).

A local Chromium smoke used self-signed HTTPS and a mobile viewport. It checked
the MFA boundary, bilingual responsive layout, external-navigation denial,
offline notice and background data cleanup. Chromium rejected service-worker
installation under the untrusted development certificate, as expected; this is
not trusted-TLS mobile proof.

The local unsigned macOS build produced a self-contained app and DMG and reached
the license gate, but it was ad-hoc signed and rejected by Gatekeeper. Production
signing execution, notarization, stapling, final artifact custody, clean-Mac
installation, real Gemini onboarding, real backend deployment and production
update/rollback have not been performed.

## Release decision and required re-review

Current decision: **NO-GO for commercial or production distribution.** Re-run
this threat model whenever trust boundaries, entitlement rules, authentication,
storage, signing, updater privilege, deployment topology, or retention policy
changes. A production review must attach evidence for:

1. legal/license and branding clearance;
2. reviewed dependency lock and trusted build provenance;
3. Developer ID signing, accepted notarization, stapling and Gatekeeper results;
4. completed [`docs/CLEAN_MAC_TEST.md`](docs/CLEAN_MAC_TEST.md);
5. real domain/TLS/proxy/VPN deployment plus backup/restore and rotation drills;
6. representative secure-store and Admin PWA host/device tests;
7. an independent review of the privileged updater helper and product-wide
   audit/retention controls.

Until then, unavailable boundaries remain unavailable; local evidence cannot be
promoted to production verification.
