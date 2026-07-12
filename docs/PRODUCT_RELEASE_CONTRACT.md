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
