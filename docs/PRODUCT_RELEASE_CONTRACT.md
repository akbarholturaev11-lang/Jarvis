# JARVIS Product and Release Contract

## Status

This document records Akbar's approved product target for license, payment,
update, packaging, and release work. It is a contract for future implementation;
it does **not** claim that the commerce backend, admin panel, updater, signed app,
or DMG currently exists or that commercial distribution is legally cleared.

## Business model

- JARVIS has exactly one paid product plan.
- There is no subscription, recurring billing, or Lifetime Updates plan.
- A completed purchase grants indefinite use of the exact semantic version bought.
- Every newly published semantic version has its own price, set by an admin, and
  requires its own paid entitlement.
- A future version is never granted or installed automatically for free.
- If a customer does not buy a later version, the purchased older version keeps
  working. It must not be remotely blocked, expired, deleted, or disabled because
  an update was declined.

The entitlement boundary is the exact semantic version (`major.minor.patch`). A
new build, re-sign, or repackage that retains the same semantic version does not
create another paid version; platform/architecture packages for that version are
release artifacts under the same version entitlement.

## Entitlement and payment invariants

- Release visibility does not grant installation rights.
- A payment submission remains non-entitled while it is pending or under review.
- Only verified manual admin approval grants the entitlement for the exact version
  being purchased.
- Rejection grants no new entitlement and must leave the already purchased version
  unaffected.
- Update-server unavailability or temporary loss of internet must not disable the
  purchased version's core local functions.
- A client may report an update as installed only after the real installed version
  and post-install health have been verified.
- A failed update must preserve or restore the last known working version and must
  not erase user settings, local secrets, or personal data.

## Device and offline model

- The initial model permits one active server-side device binding per license.
- Device identity is platform-neutral and must be based on a generated per-install
  key pair or equivalent non-secret public identity; raw hardware serial numbers
  are not account identity and must not be exposed unnecessarily.
- Initial activation, a newly approved entitlement, and an update download require
  internet access. After activation, a locally cached, signed certificate for the
  exact purchased semantic version has no routine expiry, so temporary or extended
  loss of internet does not disable that purchased version's core local functions.
- A device replacement deactivates the old binding for future entitlement and
  artifact delivery, records an audit event, and activates the replacement. It
  must not use a remote kill switch against the already installed older copy.
  This is deliberate minimal copy protection: indefinite offline use and perfect
  remote revocation cannot both be guaranteed.
- Copying application files or a license response without possession of the bound
  device key must not grant update installation rights on another computer.

## Package and updater invariants

- Every artifact is identified by product, exact semantic version, platform,
  architecture, a build that increases monotonically within that target stream,
  byte size, SHA-256 digest, storage key, signing key ID, and cryptographic
  signature.
- Release metadata and packages are accepted only after asymmetric-signature
  verification with a pinned trusted public key, exact identity matching, declared
  source-version compatibility, full download length, and digest verification.
- Download and verification happen in a private staging location. The installed
  app is not changed before the complete candidate has passed all checks.
- Installation uses a platform adapter and an atomic replacement or equivalent
  last-known-working layout. User data, settings, logs, and secure-store items live
  outside the application bundle and are never replaced by the package.
- Any failed download, verification, install, or post-install health check must
  end by verifying that the previous version remains usable or by restoring it.
  A retry may not bypass this preservation/rollback checkpoint.
- Success is reported only after the launched application proves the expected
  installed product/version/build and passes a bounded health check.
- The updater must not upload personal project files, local memory, API keys,
  tokens, or unrelated settings.

## Payment evidence privacy

- Payment screenshots are private evidence objects, not public release assets.
- Only authenticated, authorized admins may read them; customers may access only
  their own submission status and sanitized rejection reason.
- The database stores an opaque private-object key and metadata, not public URLs or
  screenshot bytes. Upload type/size are bounded, access is audited, and retention
  must be documented before production deployment.

## Cross-platform contract

- Customer/license/version entitlements are platform-neutral; they must not be
  needlessly tied to macOS.
- Release packages are platform- and architecture-specific and are identified
  separately from the semantic-version entitlement.
- macOS may ship first as a DMG. Windows and Linux must use the same neutral
  contract and return explicit `not_available` or `unsupported` states until their
  installers and updater adapters exist.
- No platform-specific implementation may silently report success on another
  platform.

## Commercial-distribution gates

Commercial sale or public distribution is blocked until every applicable gate is
cleared and documented:

1. Obtain upstream `CC BY-NC` commercial permission/relicensing, or replace the
   affected upstream material with a rights-clean implementation.
2. Select and document a lawful PyQt6 distribution model, including a commercial
   license if the chosen proprietary distribution requires it.
3. Verify rights to the JARVIS/third-party names, branding, icons, copy, and every
   bundled asset.
4. Complete platform-appropriate signing. For macOS this includes Developer ID
   signing, hardened-runtime compatibility, notarization, stapling, and
   verification on the final artifact.
5. Confirm the final product name and reverse-DNS bundle identifier. The current
   foundation identifier is provisional and must not be treated as proof of
   branding/domain ownership.

Until those gates are cleared, local design, implementation, and testing may
continue, but agents must not describe JARVIS as commercially cleared, ready for
sale, or safely distributable to customers.

## Implementation guardrails

- Never add a subscription or Lifetime Updates path alongside this model without
  new explicit direction from Akbar.
- Never add remote revocation or forced expiry for a legitimately purchased older
  version.
- Never expose or embed API keys, tokens, signing credentials, payment credentials,
  private artifact links, or real database connection values.
- Every new visible fixed UI string remains bilingual English + Russian.
- License, payment, update, and release capabilities remain platform-neutral or
  return an explicit truthful status.
- Do not report any product flow as working until its code, tests, and real artifact
  checks verify it.
