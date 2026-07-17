# JARVIS Mobile Admin Mode

## Current status

Phase 1 is implemented as the responsive, installable `/admin/` web console. It
uses the existing cobalt/paper release-control interface and the same protected
HTTPS API as the desktop browser. It is separate from the ordinary JARVIS remote
control PWA: the two applications have different scopes, service workers and
authentication boundaries.

Native iOS and Android applications are **`not_available`** in this repository.
The responsive PWA can be installed from a supported mobile browser, but that is
not evidence that a native wrapper, App Store build, Play Store build or mobile
push provider exists.

The same-origin PWA boundary is **implemented**, mechanically **enforced**, and
**tested locally** by unit/integration tests plus a 390×844 Playwright Chromium
smoke over self-signed local HTTPS. No mobile surface is
**production-verified**. Trusted public TLS and representative iOS/Android
browsers are **external blockers**; native wrappers/background push are
`not_available`; end-to-end notification delivery and a unified audit view have
the **internal gaps** described below. See
[`E2E_PRODUCT_VALIDATION.md`](E2E_PRODUCT_VALIDATION.md),
[`../SECURITY.md`](../SECURITY.md), and
[`../THREAT_MODEL.md`](../THREAT_MODEL.md).

## Enforced web boundary

- Admin authentication remains server-owned: subject, password and TOTP or a
  one-time recovery code establish a short-lived server session.
- The browser receives a Secure, HttpOnly, SameSite=Strict cookie. Mutations also
  require the in-memory CSRF value returned by the authenticated API. The UI does
  not put a password, session token, CSRF value, recovery code or activation key
  in browser storage.
- The console uses only same-origin relative API paths. External link navigation
  is blocked by the client and must also be blocked by any future native wrapper.
  There is no JavaScript bridge.
- Private payment evidence is fetched with `cache: no-store`, held only in a Blob
  URL and revoked when the dialog closes or the app moves to the background.
- Background/page-hide cleanup stops polling, revokes evidence and enrollment QR
  Blob URLs, hides one-time recovery and activation material, clears password and
  one-time-code fields, and covers the UI with a privacy shield.
- Logout and server-side session revocation remain authoritative. Returning from
  the background does not manufacture a session or bypass MFA.

## PWA and offline contract

`manifest.webmanifest` uses a relative `/admin/` scope and local artwork. The
service worker owns only a versioned `jarvis-admin-shell-*` cache and an explicit
allowlist of the public HTML/CSS/JavaScript/localization/manifest/icon files. It
does not intercept or cache `/api/`, session, payment evidence, customer data,
release data, audit data or any unknown path. It also leaves unrelated service
worker caches untouched.

Offline mode is intentionally read-only and data-empty. The public shell and
bilingual offline explanation may render, but sign-in, evidence, lists and
mutations require a live HTTPS connection. No cached admin record is shown as
current. This is an honest degraded state, not offline administration.

## Notifications

The implemented notification channel is **in-app only**. While an authenticated
admin view is online and visible, a single bounded timer refreshes the payment
queue every 30 seconds. New pending IDs produce a bilingual in-app banner; the
baseline load does not create a false “new” alert. Polling stops when the view is
hidden, offline or signed out.

A native APNs, FCM or Web Push provider is **`not_available`**. No UI text claims
that background push delivery exists. Adding one later requires an explicit
provider interface, permission UX, revocation, payload minimization and tests;
payment evidence or customer data must never be placed in a push payload.

The polling source, baseline suppression, visibility/offline stop conditions and
bilingual banner contract are tested locally. The Stage 9 harness did **not**
create a new post-baseline payment and observe its delivery into a live browser
banner, so that delivery remains an **internal verification gap** rather than a
PASS claim.

## Admin capabilities in Phase 1

- Payment queue, private screenshot review, approve/reject and rejection reason.
- Persisted release list, verified artifact metadata and explicit confirmation
  before release creation or publication.
- Persisted customer and license directories, exact-version entitlement summary,
  account/license creation, device binding/replacement and one-time activation.
- Read-only payment approval/rejection audit history. MFA events and device
  replacement history are persisted separately, but one product-wide audit query
  and UI do not yet exist; unified audit remains an **internal gap**.
- MFA enrollment, recovery-code lifecycle, recent re-authentication, password
  rotation and session revoke/revoke-all.

All fixed visible additions are present in English and Russian. The layout uses a
five-target touch navigation rail on narrow screens and retains the evidence-
custody sequence as the decision model.

## Future native wrapper contract

If a native iOS or Android shell is added, it must be a thin HTTPS client and must
meet every item below before its status can change from `not_available`:

1. Allow only the configured production HTTPS origin and exact `/admin/` scope;
   reject HTTP, certificate errors, `file:`, `content:`, custom schemes and
   external navigation. Local development exceptions must be explicit and absent
   from production builds.
2. Expose no general-purpose JavaScript bridge. Camera, contacts, filesystem,
   clipboard and external-app access stay disabled unless a separately reviewed,
   minimal capability requires them.
3. Keep server session cookies inside the OS-protected web data container, clear
   them on logout, remote revoke and account reset, and never copy them into app
   preferences, logs, crash reports or analytics.
4. Put no database, activation pepper, admin password material, MFA encryption
   key, entitlement private key or release-signing private key on the phone.
5. Apply platform privacy controls for app-switcher snapshots and backgrounding,
   while preserving the web cleanup contract above.
6. Verify remote session revoke, idle/absolute expiry, CSRF, MFA, screenshot
   authorization, offline denial and external-navigation denial on real devices.
7. Document iOS/Android/PWA support independently. A passing browser PWA test
   must never be recorded as a passing native test.

## Verification still required outside source tests

The local Chromium smoke verified the login boundary, restored read-only MFA
session, English/Russian rendering, 390×844 no-overflow layout, blocked external
navigation and offline data denial. Chromium refused service-worker installation
under the self-signed certificate; this is expected and means installability and
offline shell behavior were not production-verified.

- Install the PWA from production-like LAN HTTPS on representative iOS and
  Android browsers and confirm the manifest/icon/scope.
- Exercise MFA login, evidence viewing, approve/reject, customer/license reads,
  device replacement, audit and remote revoke at 390×844 and 412×915 viewports.
- Put the app in the background while evidence, a QR code, recovery codes and an
  activation key are open; confirm the app-switcher preview is shielded and the
  material is cleared on return.
- Disable networking and confirm only the public shell/offline notice is shown,
  no prior admin data is replayed, and reconnect resumes bounded polling.
- Confirm the deployment CSP permits only the same-origin manifest and worker;
  production HTTPS, proxy trust and admin IP/VPN policy remain deployment gates.
