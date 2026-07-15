# JARVIS macOS Update and Rollback

## Status

The repository implements and tests the complete transaction boundary and a
real development-only macOS `.app` replacement adapter. Production mutation is
still fail-closed `not_available`: it requires a separately installed,
Developer-ID-signed and notarized helper plus an independently audited
shutdown/privilege protocol. No source or frozen build can silently select the
development adapter.

| Area | Status |
| --- | --- |
| Signed manifest, artifact hash/size and exact source/target verification | Implemented and tested |
| Exact-version entitlement recheck immediately before installation | Enforced and tested |
| Private staged download and pinned file-descriptor handoff | Enforced and tested |
| Strict `jarvis_macos_app_zip_v1` extraction | Implemented and adversarially tested |
| Private persisted backup and tree digest | Implemented and tested locally |
| Same-volume atomic `.app` replacement | Implemented and tested locally through the explicit development adapter |
| Fresh-nonce exact-identity health check | Implemented and tested locally |
| Durable journal, interrupted recovery and verified rollback | Implemented and tested locally |
| Production signed helper assessment | Implemented and fail-closed |
| Real `/Applications` install, safe process shutdown and privileged helper | `not_available` pending signed/notarized helper |
| Clean-Mac Gatekeeper update/rollback | Not production-verified |
| Windows/Linux installation | Honest `not_available` |

## Security invariants

1. A release manifest signature, artifact SHA-256 and byte size must all verify
   before a `VerifiedStagedUpdate` exists.
2. Installation re-reads the signed local entitlement for the current license,
   active device and exact target semantic version. Missing, corrupt or wrong
   authority never reaches the mutation adapter.
3. The coordinator opens the staged artifact without following links, verifies
   it and pins that file descriptor through the install call. An adapter must
   copy and re-hash the bytes into its own private destination.
4. The macOS archive has one `.app` root and bounded file count, path length,
   compressed size and expanded size. Absolute paths, traversal, backslashes,
   NULs, duplicate/case-colliding entries, special files, symlink parents,
   dangling links, cycles and links escaping the bundle are rejected. Safe
   relative in-bundle framework links are preserved.
5. `Info.plist` must report the exact bundle ID, semantic version and monotonic
   build expected by the signed release manifest. Downgrades are rejected.
6. A private backup and its deterministic tree digest exist before the durable
   rollback checkpoint is written. The checkpoint is cleared only after exact
   identity plus a fresh-nonce health response proves the target or restored
   source healthy.
7. A crash or power interruption leaves the checkpoint. On the next launch,
   recovery performs a fresh locked journal read before license onboarding,
   Gemini onboarding, payment resume or assistant construction. Unverified
   recovery blocks JARVIS startup.
8. The production helper path is fixed. Assessment rejects missing, linked,
   multiply linked, wrong-owner or writable files/parents and requires
   `codesign`, expected Team ID/designated requirement, `spctl` and stapler
   validation. Passing assessment still does not enable mutation until the
   production protocol exists.

## Transaction results

- `installed`: target identity and health were verified; never inferred from a
  successful copy command alone.
- `preserved`: mutation did not complete and the previous app was independently
  re-verified.
- `rolled_back`: the backup was restored and the previous app was independently
  re-verified.
- `rollback_required`: safety is unresolved; retry and JARVIS startup are
  blocked.
- `not_available` / `unsupported`: the platform/helper contract cannot mutate;
  no installation success is claimed.
- `invalid` / `failed`: staged authority or integrity failed, or safety could
  not be proven.

## Local verification

The integration suite builds synthetic version-A and version-B `.app` bundles
inside private temporary directories, creates the strict update ZIP, and uses
an explicit `MacOSDevelopmentUpdaterAdapter`. It verifies:

- A to B installation and target health;
- forced target health failure and rollback to A;
- interrupted transaction recovery and a checkpoint written after coordinator
  construction;
- corrupt hash, wrong identity, downgrade and repeated install rejection;
- traversal, symlink escape/cycle/dangling-link and ZIP-bomb rejection;
- backup tamper, missing backup and rollback failure;
- missing/unsafe/unsigned/replaced production helper rejection;
- development-adapter rejection in any frozen runtime.

This is a real local filesystem transaction, not evidence that a production
signed app can update itself while running. Final verification requires a clean
supported Mac, final signed artifacts, an installed notarized helper and saved
`codesign`, `spctl`, stapler, interruption and rollback evidence.

## External completion steps

1. Implement and independently audit the fixed privileged-helper request and
   safe-shutdown protocol. Do not pass secrets or user-controlled paths in argv.
2. Sign helper and app with the final Developer ID Team, notarize and staple
   both, then pin the Team ID and designated requirement in production build
   metadata.
3. Run version A to B, forced failure, power interruption and rollback tests on
   a clean supported Mac using the final `/Applications/JARVIS.app` bundle.
4. Retain verification reports and update the release checklist. Until all four
   steps pass, production installation remains `not_available`.
