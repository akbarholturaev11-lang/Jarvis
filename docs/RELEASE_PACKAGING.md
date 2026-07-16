# JARVIS Release Packaging Architecture

## Current status

This document describes the implemented packaging **foundation**, not a customer
release.  The repository now has a platform-neutral release adapter contract and
a macOS-first unsigned local PyInstaller/DMG plan.  It does not yet have a signed,
notarized, commercially cleared, clean-device-verified DMG.

**Scope:** only the **macOS** packaging pipeline is implemented (and validated
locally as an unsigned/ad-hoc dev build).  This is **not** a cross-platform
packaging deliverable.  The JARVIS desktop client needs a separate distributable
per operating system, and only macOS exists today:

| Target | Required distributable | Packaging status | Update install |
| --- | --- | --- | --- |
| macOS | `.app` + DMG | `available` only when the local unsigned prerequisites pass | `not_available` |
| Windows | self-contained `.exe` + installer | `not_available` | `not_available` |
| Linux | AppImage and/or `.deb` | `not_available` | `not_available` |
| Unknown | — | `not_available` | `not_available` |

Windows and Linux packaging are explicit later stages (see the roadmap below);
their release adapters return `not_available` today and must never report a
macOS artifact as a Windows or Linux build.

No adapter reports update success.  A future updater may report success only
after artifact identity, size, digest and signature checks; atomic replacement;
launching the expected version/build; a bounded health check; and verified
preservation or rollback.

## Implemented layers

- `core/platform_adapters/release_base.py` — neutral request, plan, command and
  update-result contracts.
- `core/platform_adapters/release_macos.py` — read-only prerequisite assessment
  and argv-only unsigned local `.app` + DMG plan.
- `core/platform_adapters/release_windows.py` and `release_linux.py` — explicit
  honest placeholders.
- `core/platform_adapters/release_factory.py` — target routing with no silent
  macOS fallback.
- `packaging/macos/Jarvis.spec` — explicit non-secret PyInstaller resource list,
  hidden imports (Google GenAI SDK, PyQt6 plugins, cryptography, PIL, cv2, uvicorn)
  and the updater-helper modules, plus an optional dev-only `JARVIS_APP_ICON`.
- `packaging/macos/entitlements.plist` — hardened-runtime entitlements (JIT /
  unsigned-memory / library-validation for a frozen CPython + PyQt6 app, plus
  microphone, camera, Apple Events and network) with **no** App Sandbox.
- `core/platform_adapters/release_signing.py` — side-effect-free Developer ID
  signing + notarization planner: env-only public labels, nested-code-first
  ordering, `codesign --options runtime`/`spctl`/`notarytool`/`stapler`, and an
  honest unsigned-dev-build status when no credentials are configured.
- `core/release_build_manifest.py` — a non-secret local build manifest
  (product identity, SHA-256, byte size, `signed`/`notarized`/`distribution_ready`).
- `scripts/build_macos_release.py` — read-only planning by default and an explicit
  combined local-unsigned execution mode.
- `scripts/release_pipeline.py` + `packaging/macos/*.sh` — granular, argv-only
  steps (`clean`, `build_app`, `build_dmg`, `generate_manifest`, `verify_app`,
  `sign_artifact`, `smoke_launch`, `cleanup`, `build_all`), each a thin driver
  over the same `MacOSReleaseAdapter` plan, signing planner and manifest builder.
- `.github/workflows/macos-release.yml` — CI that runs the packaging unit tests,
  builds/verifies/uploads an unsigned artifact, and runs signing/notarization
  **only** when the protected Developer ID secrets are present (never logged).

The adapters resolve the source resource root through `AppPaths`.  The spec
places the committed prompt, safe settings, example configuration and dashboard
static assets in the bundle. The build script also generates a non-secret,
strict `product_build.json` from the requested SemVer/build so the frozen client
can prove its own installed identity. Writable data, secrets, logs, update staging and
personal memory belong outside the application bundle under the `AppPaths`
locations.

## Secret boundary

The spec deliberately does not include:

- local Gemini or other API keys;
- personal long-term memory;
- the real device profile;
- the real Zerno source configuration or environment helper;
- TLS/private certificate material;
- payment, signing, notarization or object-storage credentials.

Only explicit files are bundled.  Adding an entire `config/`, `memory/`, home or
project directory to PyInstaller data is prohibited.

## Local unsigned plan

The default command is read-only:

```bash
.venv/bin/python scripts/build_macos_release.py \
  --version 1.0.0 \
  --build 1 \
  --architecture arm64
```

Build tools are pinned separately from runtime dependencies:

```bash
python3.12 -m venv /secure/build-venv
/secure/build-venv/bin/python -m pip install -r requirements.txt -r requirements-build.txt
```

Do not install build tooling into a customer runtime environment. PyInstaller is
platform-native rather than a cross-compiler, so each target must be built and
verified on its own supported operating system.

It prints a machine-readable plan and exits with status `2` when PyInstaller,
`hdiutil`, the macOS host or a required safe resource is unavailable.  It does
not install dependencies.

Execution requires the explicit flag below:

```bash
.venv/bin/python scripts/build_macos_release.py \
  --version 1.0.0 \
  --build 1 \
  --architecture arm64 \
  --product-config /secure/build-input/product.json \
  --execute-local-unsigned
```

That mode may create `JARVIS.app`, a drag-to-Applications DMG, its SHA-256 and
byte size.  Its output always records:

- signing: not performed;
- notarization: not performed;
- distribution ready: false.

It must never be offered to customers as a trusted release.

## Granular local pipeline

The end-to-end local unsigned pipeline runs as discrete, reversible steps from a
double-clickable set of scripts (never the customer runtime; use an isolated
build venv via `JARVIS_BUILD_PYTHON`):

```bash
export JARVIS_BUILD_VERSION=1.0.0 JARVIS_BUILD_NUMBER=1 JARVIS_TARGET_ARCH=arm64
export JARVIS_BUILD_PYTHON=/secure/build-venv/bin/python
export JARVIS_PRODUCT_CONFIG=/secure/build-input/product.json
bash packaging/macos/build_all.sh   # clean → build_app → build_dmg → manifest →
                                    # verify_app → sign (plan) → smoke → cleanup
```

Each step refuses to fake success: `build_app` fails honestly when PyInstaller is
absent, `generate_manifest` fails until the DMG exists, and `verify_app` fails if
any secret file (`api_keys.json`, `long_term.json`, private configs, `*.key`,
`*.pem`, …) is found inside the bundle.

## Signing, notarization and the credential interface

`sign_artifact.sh` (and `scripts/release_pipeline.py sign`) read only **public**
labels from the environment; the private key and notary credentials stay in the
macOS keychain and are referenced by name:

- `JARVIS_MACOS_SIGN_IDENTITY` — e.g. `Developer ID Application: Name (TEAMID)`
- `JARVIS_MACOS_TEAM_ID` — 10-character Apple Team ID
- `JARVIS_MACOS_NOTARY_PROFILE` — `notarytool` keychain profile name

Without these, the planner returns an honest `not_available` unsigned-dev-build
result. With them, it plans nested-code-first `codesign --options runtime
--timestamp --entitlements …`, signs the outer bundle last, then
`codesign --verify`, `spctl --assess`, `notarytool submit --wait`,
`stapler staple`/`validate` and a final `spctl` install assessment.

## Remaining integration gates

The following are not solved by the packaging skeleton:

1. The pinned PyInstaller 6.21.0 build was run locally on 2026-07-16 in an
   isolated build venv and produced a working unsigned `JARVIS.app` (624 MB) and
   DMG (240 MB) that launches from its embedded interpreter to the license gate
   with the PyQt6 cocoa plugin. A full frozen-runtime audit of every optional
   path (audio, camera, Playwright browsers, remote-tunnel tools) on a clean Mac
   user account is still advisable before release.
2. A final product icon/name/bundle identifier requires cleared branding rights;
   `make_icns.sh` only produces a provisional dev icon from the committed PWA art.
3. The Developer ID signing / hardened-runtime / notarization / stapling /
   Gatekeeper pipeline is now **implemented as a planner + entitlements + CI job**,
   but it has never been run with a real Developer ID identity, notary profile or
   Apple notarization service; no signed, notarized, stapled artifact exists yet.
4. Update download/staging and a durable rollback contract exist, but the real
   signed atomic application-replacement helper is not implemented on any platform.
5. The final artifact has not been installed and exercised on a clean supported
   Mac user account/device.
6. The upstream CC BY-NC, PyQt6 distribution and other commercial gates in
   `docs/PRODUCT_RELEASE_CONTRACT.md` remain blockers.

## Windows and Linux packaging — roadmap and blockers (NOT implemented)

Windows and Linux use the same semantic-version/build/artifact and entitlement
contracts.  Account, license, payment approval and exact-version entitlement are
platform-neutral, but the **distributable and installer for each OS are separate
work that does not exist yet**.  `WindowsReleaseAdapter` and `LinuxReleaseAdapter`
return `plan_build(...).status = not_available` (empty commands) and
`install_update(...).verified = False` today; nothing here is done by the macOS
pipeline.

### Windows (next stage — `not_available`)

Required distributable: a **self-contained `.exe`** (frozen PyInstaller build) plus
an **installer** (e.g. Inno Setup or MSIX).  Blockers:

- A Windows build host and a `WindowsReleaseAdapter.plan_build` implementation
  (PyInstaller one-folder/one-file `.exe`, Windows hidden imports, PyQt6 plugins).
- Windows writable-path routing already exists in `core/app_paths.py`
  (`%APPDATA%` / `%LOCALAPPDATA%`); the installer must place data/logs/secure
  store there, never inside the program files bundle.
- Authenticode code-signing certificate + timestamping (the Windows equivalent of
  Developer ID); SmartScreen reputation.
- A Windows atomic-replacement + rollback updater helper.

### Linux (next stage — `not_available`)

Required distributable: an **AppImage** and/or a **`.deb` package**.  Blockers:

- A Linux build host and a `LinuxReleaseAdapter.plan_build` implementation
  (PyInstaller + `appimagetool`, or `dpkg-deb`/`.desktop` + icon), with XDG
  writable paths already handled in `core/app_paths.py`.
- Qt/xcb runtime dependencies (system libs, `libxcb*`, fontconfig) audited for a
  portable AppImage; `.deb` dependency declarations for the apt path.
- A signing/verification story (GPG-signed repo metadata for `.deb`; embedded
  signature or detached `.sig` for AppImage) and an atomic update/rollback helper.

Until each toolchain, build host and real install/rollback verification exist,
these adapters stay `not_available` and no macOS artifact may be presented as a
Windows or Linux build.
