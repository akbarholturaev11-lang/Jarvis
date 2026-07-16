"""Deterministic evidence catalog and report helpers for product-release E2E.

This module deliberately separates *local automated evidence* from the final
scenario status.  A passing synthetic/development test cannot turn a missing
production helper, clean-host check, legal clearance, or real service check
into a production PASS.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Final, Literal

ReportStatus = Literal["pass", "fail", "not_available", "not_run"]

REPORT_SCHEMA: Final = "jarvis-product-release-e2e/v1"
ALLOWED_STATUSES: Final = frozenset(
    {"pass", "fail", "not_available", "not_run"}
)
_PYTEST_SUMMARY_COUNT = re.compile(
    r"(?<![A-Za-z0-9_])(?P<count>[0-9]+) "
    r"(?P<status>passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b"
)
_SAFE_ENV_KEYS: Final = frozenset(
    {
        "CI",
        "COMSPEC",
        "HOME",
        "LANG",
        "LANGUAGE",
        "PATH",
        "PATHEXT",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "VIRTUAL_ENV",
        "WINDIR",
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceGroup:
    group_id: str
    description: str
    node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: int
    title: str
    evidence_groups: tuple[str, ...]
    not_available_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    group_id: str
    status: ReportStatus
    argv: tuple[str, ...]
    returncode: int | None
    duration_ms: int
    output: str
    reason: str


@dataclass(frozen=True, slots=True)
class ExternalGate:
    gate_id: str
    classification: Literal["external", "legal"]
    reason: str


EVIDENCE_GROUPS: Final = (
    EvidenceGroup(
        "license_gate",
        "Frozen/source gate policy, no-license state, and bilingual gate UI.",
        (
            "tests/test_product_gate.py",
            "tests/test_product_gate_ui_contract.py",
        ),
    ),
    EvidenceGroup(
        "initial_purchase",
        "Fresh purchase, private PNG evidence, MFA review, reject/resubmit, "
        "approval, and polling.",
        ("tests/test_product_initial_purchase_e2e.py",),
    ),
    EvidenceGroup(
        "payment_validation",
        "Image MIME, size, corruption, metadata stripping, and private-path boundaries.",
        ("tests/test_payment_evidence.py",),
    ),
    EvidenceGroup(
        "mobile_admin",
        "Responsive Admin PWA isolation, private data access, polling, and background cleanup.",
        (
            "tests/test_mobile_admin_pwa.py",
            "tests/test_admin_web_static.py",
        ),
    ),
    EvidenceGroup(
        "admin_mfa",
        "RFC 6238, replay defence, recovery codes, CSRF, sessions, and audit controls.",
        (
            "tests/test_product_backend_totp.py",
            "tests/test_product_backend_mfa_sessions.py",
        ),
    ),
    EvidenceGroup(
        "activation_offline",
        "Signed activation, offline cache, wrong device/version, and paid-version boundary.",
        (
            "tests/test_product_activation.py",
            "tests/test_product_license_gate_integration.py",
        ),
    ),
    EvidenceGroup(
        "desktop_runtime",
        "Desktop initial/update purchase persistence, restart retry, and explicit install boundary.",
        ("tests/test_product_runtime.py",),
    ),
    EvidenceGroup(
        "secure_credentials",
        "Gemini validation plus secure-store CRUD, migration, persistence, and platform routing.",
        (
            "tests/test_secure_store.py",
            "tests/test_credential_service.py",
            "tests/test_gemini_credential_onboarding.py",
            "tests/test_gemini_credential_validator.py",
        ),
    ),
    EvidenceGroup(
        "paid_update",
        "Paid exact-version update authorization, download grant, retry, and replay boundaries.",
        (
            "tests/test_product_backend_api_mvp.py",
            "tests/test_product_updates.py",
        ),
    ),
    EvidenceGroup(
        "signature_security",
        "Pinned release signatures, canonical manifests, corruption, and key mismatch.",
        (
            "tests/test_release_manifest.py",
            "tests/test_product_backend_release_verifier.py",
        ),
    ),
    EvidenceGroup(
        "updater_transaction",
        "Development A-to-B install, health proof, interruption recovery, and verified rollback.",
        (
            "tests/test_macos_update.py",
            "tests/test_update_transaction.py",
        ),
    ),
    EvidenceGroup(
        "device_replacement",
        "Atomic device replacement, replay rejection, and old-device server denial.",
        ("tests/test_product_backend_device_replacement.py",),
    ),
    EvidenceGroup(
        "audit_projection",
        "Persistent commerce decisions and bounded authenticated admin projections.",
        (
            "tests/test_product_backend_commerce.py",
            "tests/test_product_backend_admin_queries.py",
        ),
    ),
)

EVIDENCE_BY_ID: Final = {group.group_id: group for group in EVIDENCE_GROUPS}


SCENARIOS: Final = (
    Scenario(
        1,
        "Fresh user opens Jarvis",
        ("license_gate",),
        "A frozen build has not been exercised on a clean customer Mac.",
    ),
    Scenario(2, "No license is present", ("license_gate",)),
    Scenario(
        3,
        "Purchase screen is shown",
        ("license_gate", "initial_purchase"),
    ),
    Scenario(
        4,
        "Release notes and server price are shown",
        ("license_gate", "initial_purchase"),
    ),
    Scenario(
        5,
        "Payment instructions are shown",
        ("license_gate", "initial_purchase"),
    ),
    Scenario(
        6,
        "Payment screenshot is uploaded privately",
        ("initial_purchase", "payment_validation"),
    ),
    Scenario(
        7,
        "Admin receives an in-app pending notification",
        ("initial_purchase", "mobile_admin"),
        "Polling and notification source contracts are tested, but a new "
        "post-baseline payment has not been delivered into the browser banner "
        "by this automated harness.",
    ),
    Scenario(8, "Admin logs in with MFA", ("initial_purchase", "admin_mfa")),
    Scenario(9, "Admin reviews private evidence", ("initial_purchase", "admin_mfa")),
    Scenario(10, "Admin approves payment idempotently", ("initial_purchase",)),
    Scenario(
        11,
        "Exact-version entitlement is granted",
        ("initial_purchase", "activation_offline"),
    ),
    Scenario(12, "Client polling observes approval", ("initial_purchase",)),
    Scenario(13, "Client activates with signed entitlement", ("activation_offline",)),
    Scenario(14, "Gemini key is stored securely", ("secure_credentials",)),
    Scenario(
        15,
        "Jarvis assistant runtime starts",
        ("license_gate", "secure_credentials"),
        "A real Gemini Live session with operator-owned credentials is not run by this harness.",
    ),
    Scenario(
        16,
        "App restart preserves activation and credentials",
        ("activation_offline", "secure_credentials", "desktop_runtime"),
    ),
    Scenario(17, "Purchased version works offline", ("activation_offline",)),
    Scenario(
        18,
        "A new paid semantic version is published",
        ("activation_offline", "paid_update"),
    ),
    Scenario(19, "Older purchased version remains usable", ("activation_offline",)),
    Scenario(20, "User purchases the update", ("paid_update", "desktop_runtime")),
    Scenario(21, "Authorized update downloads privately", ("paid_update",)),
    Scenario(
        22,
        "Manifest signature and artifact hash are verified",
        ("paid_update", "signature_security"),
    ),
    Scenario(
        23,
        "Update installs atomically",
        ("updater_transaction",),
        "Frozen production install is disabled until the fixed helper is "
        "signed, notarized, and audited.",
    ),
    Scenario(
        24,
        "Post-install health check succeeds",
        ("updater_transaction",),
        "Production helper health-check execution is not available in frozen builds.",
    ),
    Scenario(
        25,
        "Updated product reports success",
        ("updater_transaction",),
        "Only the development transaction has local evidence; production success is unavailable.",
    ),
    Scenario(
        26,
        "Forced update failure is detected",
        ("updater_transaction",),
        "Failure injection is verified only through the development updater, "
        "not a signed production helper.",
    ),
    Scenario(
        27,
        "Failed update rolls back to the verified prior app",
        ("updater_transaction",),
        "Rollback is locally verified only through the development updater.",
    ),
    Scenario(28, "Admin replaces the active device", ("device_replacement",)),
    Scenario(
        29,
        "Old device loses future server operations",
        ("device_replacement", "activation_offline"),
    ),
    Scenario(
        30,
        "Unified product audit log is complete",
        ("audit_projection", "admin_mfa", "device_replacement"),
        "Payment, MFA, and device events exist in separate stores; a unified "
        "product audit view is an internal gap.",
    ),
)


EXTERNAL_GATES: Final = (
    ExternalGate(
        "upstream_commercial_rights",
        "legal",
        "Upstream CC BY-NC commercial permission or a rights-clean replacement is required.",
    ),
    ExternalGate(
        "pyqt6_distribution_rights",
        "legal",
        "The lawful PyQt6 commercial/distribution model has not been documented as cleared.",
    ),
    ExternalGate(
        "branding_asset_rights",
        "legal",
        "Product name, icon, copy, and bundled asset rights have not been cleared.",
    ),
    ExternalGate(
        "developer_id_notarization",
        "external",
        "No real Apple Developer ID identity/notary credential or notarized "
        "final artifact is available.",
    ),
    ExternalGate(
        "clean_mac_validation",
        "external",
        "The final signed DMG has not been installed and exercised on a clean Mac.",
    ),
    ExternalGate(
        "production_https_host",
        "external",
        "No operator-owned production domain, TLS deployment, or server access "
        "is supplied to this harness.",
    ),
    ExternalGate(
        "representative_mobile_browsers",
        "external",
        "Real iOS/Android browser and LAN HTTPS smoke evidence is not produced "
        "by this local pytest harness.",
    ),
)


def validate_catalog(project_root: Path | None = None) -> tuple[str, ...]:
    """Return deterministic catalog errors; an empty tuple means valid."""

    errors: list[str] = []
    scenario_ids = [scenario.scenario_id for scenario in SCENARIOS]
    if scenario_ids != list(range(1, 31)):
        errors.append("scenario IDs must be exactly 1 through 30 in order")
    if len(set(scenario_ids)) != len(scenario_ids):
        errors.append("scenario IDs must be unique")

    group_ids = [group.group_id for group in EVIDENCE_GROUPS]
    if len(set(group_ids)) != len(group_ids):
        errors.append("evidence group IDs must be unique")

    seen_test_files: dict[str, str] = {}
    for group in EVIDENCE_GROUPS:
        if not group.node_ids:
            errors.append(f"evidence group {group.group_id} is empty")
        for node_id in group.node_ids:
            test_path = node_id.split("::", 1)[0]
            if not test_path.startswith("tests/test_") or not test_path.endswith(".py"):
                errors.append(f"unsafe pytest node ID in {group.group_id}: {node_id}")
            previous = seen_test_files.setdefault(test_path, group.group_id)
            if previous != group.group_id:
                errors.append(
                    f"test file {test_path} appears in both {previous} and {group.group_id}"
                )
            if project_root is not None and not (project_root / test_path).is_file():
                errors.append(f"pytest evidence path is missing: {test_path}")

    known_groups = set(group_ids)
    for scenario in SCENARIOS:
        if not scenario.evidence_groups:
            errors.append(f"scenario {scenario.scenario_id} has no evidence group")
        unknown = set(scenario.evidence_groups) - known_groups
        if unknown:
            errors.append(
                f"scenario {scenario.scenario_id} references unknown groups: {sorted(unknown)}"
            )
    return tuple(errors)


def select_scenarios(scenario_ids: Iterable[int] | None = None) -> tuple[Scenario, ...]:
    if scenario_ids is None:
        return SCENARIOS
    requested = tuple(scenario_ids)
    if len(set(requested)) != len(requested):
        raise ValueError("scenario selection contains duplicates")
    by_id = {scenario.scenario_id: scenario for scenario in SCENARIOS}
    unknown = sorted(set(requested) - set(by_id))
    if unknown:
        raise ValueError(f"unknown scenario IDs: {unknown}")
    requested_set = set(requested)
    return tuple(
        scenario for scenario in SCENARIOS if scenario.scenario_id in requested_set
    )


def evidence_groups_for(scenarios: Sequence[Scenario]) -> tuple[EvidenceGroup, ...]:
    """Return each referenced group exactly once, in catalog order."""

    requested = {
        group_id for scenario in scenarios for group_id in scenario.evidence_groups
    }
    return tuple(
        group for group in EVIDENCE_GROUPS if group.group_id in requested
    )


def _pytest_summary_counts(output: str) -> dict[str, int]:
    counts = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "deselected": 0,
    }
    for match in _PYTEST_SUMMARY_COUNT.finditer(output):
        status = match.group("status")
        if status == "error":
            status = "errors"
        counts[status] += int(match.group("count"))
    return counts


def _safe_subprocess_environment() -> dict[str, str]:
    """Pass only execution/locale keys; never inherit ambient credentials."""

    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _SAFE_ENV_KEYS or key.upper().startswith("LC_")
    }


class EvidenceRunner:
    """Run fixed pytest evidence via argv only; never invoke a shell."""

    def __init__(
        self,
        project_root: Path,
        *,
        python_executable: str = sys.executable,
        timeout_seconds: int = 900,
        executor: Callable[..., object] = subprocess.run,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.project_root = project_root.resolve()
        self.python_executable = python_executable
        self.timeout_seconds = timeout_seconds
        self._executor = executor

    def run_group(self, group: EvidenceGroup) -> EvidenceResult:
        argv = (
            self.python_executable,
            "-m",
            "pytest",
            "-q",
            "-ra",
            *group.node_ids,
        )
        started = time.monotonic()
        try:
            completed = self._executor(
                list(argv),
                cwd=self.project_root,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=_safe_subprocess_environment(),
            )
            returncode = int(getattr(completed, "returncode"))
            stdout = str(getattr(completed, "stdout", "") or "")
            stderr = str(getattr(completed, "stderr", "") or "")
            combined = stdout + "\n" + stderr
            counts = _pytest_summary_counts(combined)
            unavailable_count = (
                counts["skipped"]
                + counts["xfailed"]
                + counts["deselected"]
            )
            if returncode != 0:
                status = "fail"
                reason = f"pytest exited with status {returncode}."
            elif counts["xpassed"]:
                status = "fail"
                reason = "Pytest reported an unexpected pass; evidence expectations are stale."
            elif unavailable_count:
                status = "not_available"
                reason = (
                    "Required pytest evidence was skipped, expected-failed, "
                    "or deselected on this host."
                )
            elif counts["passed"] > 0:
                status = "pass"
                reason = "All pytest evidence passed."
            else:
                status = "fail"
                reason = "Pytest returned success without any passing evidence."
        except subprocess.TimeoutExpired:
            returncode = None
            status = "fail"
            reason = f"pytest exceeded the {self.timeout_seconds}-second limit."
        except (OSError, ValueError) as exc:
            returncode = None
            status = "fail"
            reason = f"pytest could not start ({type(exc).__name__})."
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        return EvidenceResult(
            group.group_id,
            status,
            argv,
            returncode,
            duration_ms,
            "",
            reason,
        )

    def run_groups(self, groups: Sequence[EvidenceGroup]) -> tuple[EvidenceResult, ...]:
        seen: set[str] = set()
        results: list[EvidenceResult] = []
        for group in groups:
            if group.group_id in seen:
                continue
            seen.add(group.group_id)
            results.append(self.run_group(group))
        return tuple(results)


def not_run_results(groups: Sequence[EvidenceGroup]) -> tuple[EvidenceResult, ...]:
    return tuple(
        EvidenceResult(
            group.group_id,
            "not_run",
            (sys.executable, "-m", "pytest", "-q", "-ra", *group.node_ids),
            None,
            0,
            "",
            "Evidence was planned but not executed.",
        )
        for group in groups
    )


def _scenario_result(
    scenario: Scenario,
    results_by_group: dict[str, EvidenceResult],
) -> dict[str, object]:
    results = [results_by_group.get(group_id) for group_id in scenario.evidence_groups]
    missing = [
        group_id
        for group_id, result in zip(scenario.evidence_groups, results, strict=True)
        if result is None
    ]
    evidence_status: ReportStatus
    if missing or any(result.status == "not_run" for result in results if result):
        evidence_status = "not_run"
    elif any(result.status == "fail" for result in results if result):
        evidence_status = "fail"
    elif all(result and result.status == "pass" for result in results):
        evidence_status = "pass"
    else:
        evidence_status = "not_available"

    if evidence_status == "fail":
        status: ReportStatus = "fail"
        failed = [result.group_id for result in results if result and result.status == "fail"]
        reason = f"Local evidence failed: {', '.join(failed)}."
    elif evidence_status == "not_run":
        status = "not_run"
        reason = (
            f"Evidence was not run: {', '.join(missing)}."
            if missing
            else "One or more required evidence groups were not run."
        )
    elif scenario.not_available_reason is not None:
        status = "not_available"
        reason = scenario.not_available_reason
    elif evidence_status == "pass":
        status = "pass"
        reason = "All mapped local automated evidence passed."
    else:
        status = "not_available"
        reason = "Required local evidence is not available."

    return {
        "id": scenario.scenario_id,
        "title": scenario.title,
        "status": status,
        "local_evidence_status": evidence_status,
        "evidence_groups": list(scenario.evidence_groups),
        "reason": reason,
        "production_verified": False,
    }


def build_report(
    scenarios: Sequence[Scenario],
    evidence_results: Sequence[EvidenceResult],
    *,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    results_by_group = {result.group_id: result for result in evidence_results}
    scenario_results = [
        _scenario_result(scenario, results_by_group) for scenario in scenarios
    ]
    summary = {
        status: sum(1 for item in scenario_results if item["status"] == status)
        for status in sorted(ALLOWED_STATUSES)
    }
    generated = generated_at or datetime.now(timezone.utc)
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": generated.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "production_ready": False,
        "production_verified": False,
        "summary": summary,
        "scenarios": scenario_results,
        "evidence_groups": [
            {
                "id": result.group_id,
                "status": result.status,
                "argv": list(result.argv),
                "returncode": result.returncode,
                "duration_ms": result.duration_ms,
                "reason": result.reason,
            }
            for result in evidence_results
        ],
        "external_gates": [
            {
                "id": gate.gate_id,
                "classification": gate.classification,
                "status": "not_available",
                "reason": gate.reason,
                "production_verified": False,
            }
            for gate in EXTERNAL_GATES
        ],
    }


def _markdown_escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, object]) -> str:
    scenarios = report["scenarios"]
    external_gates = report["external_gates"]
    evidence_groups = report["evidence_groups"]
    assert isinstance(scenarios, list)
    assert isinstance(external_gates, list)
    assert isinstance(evidence_groups, list)
    lines = [
        "# JARVIS Product Release E2E Validation",
        "",
        f"Generated: `{_markdown_escape(report['generated_at'])}`",
        "",
        "`production_ready=false` and `production_verified=false`. A local "
        "PASS is only automated local evidence.",
        "",
        "## Scenario matrix",
        "",
        "| # | Scenario | Status | Local evidence | Reason |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for item in scenarios:
        assert isinstance(item, dict)
        lines.append(
            "| {id} | {title} | `{status}` | `{local}` | {reason} |".format(
                id=item["id"],
                title=_markdown_escape(item["title"]),
                status=item["status"],
                local=item["local_evidence_status"],
                reason=_markdown_escape(item["reason"]),
            )
        )
    lines.extend(
        [
            "",
            "## Evidence groups",
            "",
            "| Group | Status | Duration (ms) | Reason |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for item in evidence_groups:
        assert isinstance(item, dict)
        lines.append(
            "| {id} | `{status}` | {duration} | {reason} |".format(
                id=_markdown_escape(item["id"]),
                status=item["status"],
                duration=item["duration_ms"],
                reason=_markdown_escape(item["reason"]),
            )
        )
    lines.extend(
        [
            "",
            "## External and legal gates",
            "",
            "| Gate | Class | Status | Reason |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in external_gates:
        assert isinstance(item, dict)
        lines.append(
            "| {id} | {classification} | `{status}` | {reason} |".format(
                id=_markdown_escape(item["id"]),
                classification=_markdown_escape(item["classification"]),
                status=item["status"],
                reason=_markdown_escape(item["reason"]),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _atomic_write_text(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def write_reports(report: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    if output_dir.exists() and output_dir.is_symlink():
        raise ValueError("report output directory must not be a symbolic link")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise ValueError("report output path is not a directory")
    json_path = output_dir / "report.json"
    markdown_path = output_dir / "report.md"
    _atomic_write_text(
        json_path,
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    _atomic_write_text(markdown_path, render_markdown(report))
    return json_path, markdown_path


def completed_process(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> SimpleNamespace:
    """Tiny dependency-free completed-process fixture for harness self-tests."""

    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


__all__ = [
    "ALLOWED_STATUSES",
    "EVIDENCE_BY_ID",
    "EVIDENCE_GROUPS",
    "EXTERNAL_GATES",
    "REPORT_SCHEMA",
    "SCENARIOS",
    "EvidenceGroup",
    "EvidenceResult",
    "EvidenceRunner",
    "ExternalGate",
    "Scenario",
    "build_report",
    "completed_process",
    "evidence_groups_for",
    "not_run_results",
    "render_markdown",
    "select_scenarios",
    "validate_catalog",
    "write_reports",
]
