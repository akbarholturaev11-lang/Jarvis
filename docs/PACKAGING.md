# JARVIS Packaging Readiness

## Canonical current status

This is the canonical readiness summary for packaging and distribution. The
implementation detail and roadmap live in
[`RELEASE_PACKAGING.md`](RELEASE_PACKAGING.md), updater guarantees live in
[`UPDATE_ROLLBACK.md`](UPDATE_ROLLBACK.md), and final-host evidence belongs in
[`CLEAN_MAC_TEST.md`](CLEAN_MAC_TEST.md).

Current release decision: **NO-GO**. A self-contained unsigned macOS development
artifact has been built and locally smoked, but no customer artifact is signed,
notarized, stapled, clean-Mac-verified, commercially cleared, or
production-verified.

| Capability | Implemented | Enforced | Tested locally | Production-verified | Current status |
| --- | --- | --- | --- | --- | --- |
| macOS self-contained `.app` freeze | Yes | Secret/resource and identity checks | Yes, one arm64 unsigned build | No | Development only |
| macOS drag-to-Applications DMG | Yes | Manifest/hash/size generation | Yes, one arm64 unsigned DMG | No | Development only |
| Bundle writable-path separation | Yes | App resources read-only by contract; user data routed outside bundle | Local build smoke | No | Pre-release |
| Bundle secret exclusion | Yes | Explicit resource allowlist and verify step | Automated and local artifact checks | No | Pre-release |
| Developer ID command planning | Yes | Strict identity/team/profile/artifact/tool inputs | Unit tested | No | Plan only |
| Production signing execution | No | `--execute` refuses before mutation | Negative tests | No | `not_available` |
| Notarization/stapling execution | No | Cannot claim signed/notarized/distribution-ready | Planner tests only | No | `not_available` |
| Gatekeeper validation | Verification command planned | Unsigned artifact remains rejected | Local unsigned rejection recorded | No | Signed run not performed |
| Production macOS update install | Transaction core only | Frozen mutation disabled | Synthetic development A→B/rollback | No | `not_available` |
| Clean-Mac install/update/rollback | Checklist exists | No result may be inferred | Not run | No | `NOT RUN` |
| Windows package/installer | No | Adapter returns honest status | Placeholder routing tests | No | `not_available` |
| Linux package/installer | No | Adapter returns honest status | Placeholder routing tests | No | `not_available` |

No row in this table is production-verified.

## What the repository currently provides

- `packaging/macos/Jarvis.spec` with an explicit resource/data allowlist and
  hidden imports for the current Python/PyQt6 runtime;
- `scripts/build_macos_release.py`, `scripts/release_pipeline.py`, and granular
  `packaging/macos/*.sh` drivers for clean, build, DMG, manifest, verification,
  structural smoke and cleanup;
- version/build injection through `product_build.json`;
- external writable paths through `core/app_paths.py`;
- a build manifest that records SHA-256, size, and explicit
  `signed=false`, `notarized=false`, `distribution_ready=false` state;
- a Developer ID/notary **planner** in
  `core/platform_adapters/release_signing.py`;
- an unsigned-only GitHub Actions workflow that runs tests, builds/verifies a
  development artifact, and uploads it with short retention;
- honest Windows/Linux adapter responses with no macOS fallback.

The current CI workflow does not accept or execute production signing secrets.
`packaging/macos/sign_artifact.sh --execute` and the equivalent Python command
return `not_available` before artifact mutation. A `SigningPlan` whose public
inputs are complete is only `plan_ready`; its `signed` property remains false.

The hardened-runtime entitlements intentionally omit JIT and DYLD-environment
exceptions. The remaining unsigned-executable-memory and disabled-library-
validation exceptions for frozen CPython/PyQt6 are provisional and require
final signed clean-Mac evidence and security review.

## Recorded local artifact evidence

On 2026-07-16, the pinned PyInstaller 6.21.0 path was run in an isolated build
environment on macOS arm64. The recorded result was:

- self-contained `JARVIS.app`: approximately 624 MB;
- `JARVIS-0.1.0-build1-macos-arm64.dmg`: approximately 240 MB;
- the embedded interpreter launched without a customer Python installation,
  project `.venv`, repository checkout, Terminal, or system-Python dependency;
- the frozen app reached the license gate and loaded the PyQt6 Cocoa plugin;
- the app wrote no test data inside its bundle and the verified bundle contained
  no protected secret files;
- the DMG mounted with an Applications shortcut and the copied app launched;
- the artifact had only an ad-hoc signature and `spctl` rejected it.

This is local unsigned development evidence, not a retained production signing
record. It does not prove optional audio, camera, browser automation, remote
tunnel, customer activation, update, or rollback behavior on a clean host.

The latest Stage 9 product harness on 2026-07-17 recorded the full repository at
813 tests + 527 subtests passing, but clean frozen launch and production update
rows remained `not_available`. See
[`E2E_PRODUCT_VALIDATION.md`](E2E_PRODUCT_VALIDATION.md).

## Build and verification boundary

For the local unsigned procedure, prerequisites, environment variables and
commands, use [`RELEASE_PACKAGING.md`](RELEASE_PACKAGING.md). Build tooling must
run in a disposable isolated Python 3.12 environment; it must not be installed
into a customer runtime. PyInstaller is not a cross-compiler, so each future
platform artifact must be built and verified on its native target host.

Before a production candidate can be evaluated, the pipeline must produce and
retain all of the following for the exact source commit/version/build:

1. dependency lock/provenance and build-host identity;
2. app and DMG SHA-256 plus byte size;
3. nested-code and outer-app Developer ID signing output;
4. a DMG rebuilt from the signed app, then signed itself;
5. parsed Apple notarization result with an accepted request identifier;
6. staple validation and final `codesign --verify` / `spctl` evidence;
7. a completed [`CLEAN_MAC_TEST.md`](CLEAN_MAC_TEST.md) record;
8. update/rollback evidence under [`UPDATE_ROLLBACK.md`](UPDATE_ROLLBACK.md);
9. legal/license/branding approval tied to the shipped resources.

None of those production evidence sets is complete today.

## Remaining blockers

### Internal blockers

- Implement and independently audit the final production sequence: sign nested
  code and app, rebuild DMG from the signed app, sign DMG, submit, parse accepted
  notarization result, staple, and re-verify both artifacts.
- Implement and independently audit the fixed production updater helper,
  safe-shutdown request protocol, least-privilege boundary, interruption and
  rollback execution.
- Hash-lock the complete build dependency graph and pin CI/base-image inputs for
  supply-chain review.
- Build native Windows `.exe`+installer and Linux AppImage/`.deb` paths with
  signing and atomic update/rollback; current adapters remain `not_available`.
- Decide whether the provisional hardened-runtime exceptions can be reduced
  after a real signed build.

### External blockers

- Apple Developer ID Application certificate/private key, Team ID, notarization
  account/profile and Apple service access.
- A controlled signed-build host and a clean supported Mac/device/account for
  Gatekeeper, install, permissions, offline, update and rollback testing.
- Production release key custody, artifact storage and operator evidence
  retention outside the repository.

### Legal/license blockers

- Upstream CC BY-NC commercial permission or rights-clean replacement.
- A lawful documented PyQt6 distribution model.
- Cleared product name, icon, copy and bundled-asset rights.
- Final bundle identifier/product identity supported by those rights.

Until all applicable blockers are cleared, every macOS artifact remains a local
development artifact and Windows/Linux remain `not_available`.
