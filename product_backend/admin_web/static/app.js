"use strict";

const state = {
  language: "en",
  session: null,
  payments: [],
  releases: [],
  sessionDrafts: [],
  audit: [],
  selectedRelease: null,
  evidencePayment: null,
  decision: null,
  provisioning: {
    account: null,
    license: null,
    device: null,
    replacement: null,
  },
  security: {
    mfa: null,
    sessions: [],
    enrolling: false,
  },
};

let translations = {};
let csrfToken = "";
let evidenceObjectUrl = "";
let mfaQrObjectUrl = "";
let toastTimer = 0;

class ApiError extends Error {
  constructor(status, key = "action_failed") {
    super(key);
    this.name = "ApiError";
    this.status = status;
    this.key = key;
  }
}

function element(id) {
  return document.getElementById(id);
}

function t(key, values = {}) {
  const catalog = translations[state.language] || translations.en || {};
  let value = catalog[key] || (translations.en || {})[key] || key;
  for (const [name, replacement] of Object.entries(values)) {
    value = value.replaceAll(`{${name}}`, String(replacement));
  }
  return value;
}

function node(tag, options = {}) {
  const created = document.createElement(tag);
  if (options.className) created.className = options.className;
  if (options.text !== undefined) created.textContent = String(options.text);
  if (options.type) created.type = options.type;
  if (options.title) created.title = options.title;
  return created;
}

function translateStaticPage() {
  document.documentElement.lang = state.language;
  document.title = t("document_title");
  document.querySelectorAll("[data-i18n]").forEach((item) => {
    item.textContent = t(item.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-aria]").forEach((item) => {
    item.setAttribute("aria-label", t(item.dataset.i18nAria));
  });
  document.querySelectorAll("[data-language]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.language === state.language));
  });
  updateSessionAction();
  renderPayments();
  renderReleases();
  renderAudit();
  renderProvisioning();
  renderSecurity();
  if (state.selectedRelease && element("release-dialog").open) renderReleaseDetail();
  if (state.evidencePayment && element("evidence-dialog").open) {
    element("evidence-meta").textContent = evidenceMeta(state.evidencePayment);
  }
  if (state.decision && element("decision-dialog").open) configureDecisionDialog();
}

function errorText(error, fallback = "action_failed") {
  if (!(error instanceof ApiError)) return t(fallback);
  if (error.key && error.key !== "action_failed") return t(error.key);
  const byStatus = {
    400: "request_invalid",
    401: "session_expired",
    403: "changes_locked",
    409: "operation_conflict",
    413: "request_invalid",
    422: "request_invalid",
    429: "too_many_attempts",
    503: "service_unavailable",
  };
  return t(byStatus[error.status] || fallback);
}

async function apiJson(path, options = {}) {
  const headers = { Accept: "application/json" };
  let body;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  if (options.mutate) {
    if (!csrfToken) throw new ApiError(403, "changes_locked");
    headers["X-CSRF-Token"] = csrfToken;
  }
  let response;
  try {
    response = await fetch(path, {
      method: options.method || "GET",
      headers,
      body,
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch (_error) {
    throw new ApiError(0, "service_unavailable");
  }
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === "string" ? payload.detail : "";
    } catch (_error) {
      detail = "";
    }
    const key = detail === "recent authentication is required"
      ? "reauth_required"
      : "action_failed";
    throw new ApiError(response.status, key);
  }
  if (response.status === 204) return null;
  try {
    return await response.json();
  } catch (_error) {
    throw new ApiError(502, "service_unavailable");
  }
}

function showToast(message, isError = false) {
  const toast = element("toast");
  window.clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.hidden = false;
  toastTimer = window.setTimeout(() => {
    toast.hidden = true;
  }, 4600);
}

function setBusy(control, busy) {
  control.disabled = busy;
  control.setAttribute("aria-busy", String(busy));
}

function showAuth({ allowCancel = false, message = "" } = {}) {
  element("app-shell").hidden = true;
  element("auth-shell").hidden = false;
  element("auth-cancel").hidden = !allowCancel;
  element("login-error").hidden = !message;
  element("login-error").textContent = message;
  window.setTimeout(() => element("subject").focus(), 0);
}

function showApp() {
  element("auth-shell").hidden = true;
  element("app-shell").hidden = false;
  element("session-subject").textContent = state.session?.subject || t("unknown");
  setWriteAccess(Boolean(csrfToken));
}

function setWriteAccess(allowed) {
  element("write-lock-banner").hidden = allowed;
  document.querySelectorAll(".requires-write").forEach((root) => {
    if (root.matches("button, input, select, textarea")) root.disabled = !allowed;
    root.setAttribute("aria-disabled", String(!allowed));
    root.querySelectorAll("button, input, select, textarea").forEach((control) => {
      control.disabled = !allowed;
    });
  });
  updateSessionAction();
}

function updateSessionAction() {
  const button = element("session-action");
  if (!button) return;
  button.textContent = csrfToken ? t("sign_out") : t("enable_changes");
}

async function restoreSession() {
  try {
    state.session = await apiJson("/api/admin/session");
    csrfToken = "";
    showApp();
    await loadAll();
  } catch (error) {
    state.session = null;
    csrfToken = "";
    showAuth({ message: error instanceof ApiError && error.status !== 401 ? errorText(error, "load_failed") : "" });
  }
}

async function handleLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const submit = element("login-submit");
  const subject = element("subject").value.trim();
  let password = element("password").value;
  element("password").value = "";
  const totp = element("totp").value.trim();
  let recovery = element("recovery-code").value.trim();
  element("recovery-code").value = "";
  const body = { subject, password };
  if (totp) body.totp = totp;
  else if (recovery) body.recovery_code = recovery;
  element("login-error").hidden = true;
  setBusy(submit, true);
  try {
    const issued = await apiJson("/api/admin/session", {
      method: "POST",
      body,
    });
    password = "";
    recovery = "";
    element("totp").value = "";
    state.session = { subject: issued.subject, expires_at: issued.expires_at };
    csrfToken = issued.csrf_token;
    showApp();
    if (issued.mfa_enrollment_required) {
      switchView("security");
      await loadSecurity();
      await beginEnrollment();
      showToast(t("mfa_enroll_required"));
    } else {
      await loadAll();
      showToast(t("signed_in"));
    }
  } catch (error) {
    password = "";
    element("login-error").textContent =
      error instanceof ApiError && error.status === 401
        ? t("invalid_login")
        : errorText(error);
    element("login-error").hidden = false;
  } finally {
    setBusy(submit, false);
  }
}

async function handleSessionAction() {
  if (!csrfToken) {
    showAuth({ allowCancel: true });
    return;
  }
  const button = element("session-action");
  setBusy(button, true);
  try {
    await apiJson("/api/admin/session", { method: "DELETE", mutate: true });
    csrfToken = "";
    state.session = null;
    state.payments = [];
    state.releases = [];
    state.audit = [];
    state.provisioning = {
      account: null,
      license: null,
      device: null,
      replacement: null,
    };
    state.security = { mfa: null, sessions: [], enrolling: false };
    revokeMfaQrUrl();
    renderProvisioning();
    showAuth();
    showToast(t("signed_out"));
  } catch (error) {
    showToast(errorText(error), true);
  } finally {
    setBusy(button, false);
  }
}

async function loadAll() {
  const results = await Promise.allSettled([loadPayments(), loadReleases(), loadAudit()]);
  if (results.some((result) => result.status === "rejected")) showToast(t("load_failed"), true);
}

async function loadPayments() {
  const payload = await apiJson("/api/admin/payments?limit=100");
  state.payments = Array.isArray(payload.payments) ? payload.payments : [];
  renderPayments();
}

async function loadReleases() {
  const payload = await apiJson("/api/releases?limit=100");
  state.releases = Array.isArray(payload.releases) ? payload.releases : [];
  renderReleases();
}

async function loadAudit() {
  const payload = await apiJson("/api/admin/audit?limit=100");
  state.audit = Array.isArray(payload.events) ? payload.events : [];
  renderAudit();
}

function showProvisionResult(id, text) {
  const result = element(id);
  result.textContent = text;
  result.hidden = false;
}

function renderProvisioning() {
  if (!element("account-result")) return;
  const { account, license, device, replacement } = state.provisioning;
  element("account-result").hidden = !account;
  element("license-result").hidden = !license;
  element("device-result").hidden = !device;
  element("device-replacement-result").hidden = !replacement;
  if (account) {
    showProvisionResult(
      "account-result",
      t("account_result", { id: account.account_id, subject: account.external_subject }),
    );
  }
  if (license) {
    showProvisionResult(
      "license-result",
      t("license_result", { id: license.license_id, plan: license.plan_code }),
    );
  }
  if (device) {
    showProvisionResult(
      "device-result",
      t("device_result", {
        id: device.device_binding_id,
        platform: device.platform,
        architecture: device.architecture,
      }),
    );
  }
  if (replacement) {
    showProvisionResult(
      "device-replacement-result",
      t("device_replacement_result", {
        id: replacement.device_binding_id,
        platform: replacement.platform,
        architecture: replacement.architecture,
      }),
    );
  }
}

function provisioningFormParts(form) {
  return {
    submit: form.querySelector("button[type='submit']"),
    error: form.querySelector("[data-form-error]"),
  };
}

async function handleCreateAccount(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!csrfToken || !form.reportValidity()) return;
  const { submit, error } = provisioningFormParts(form);
  error.hidden = true;
  setBusy(submit, true);
  try {
    const account = await apiJson("/api/admin/accounts", {
      method: "POST",
      mutate: true,
      body: { external_subject: form.elements.external_subject.value.trim() },
    });
    state.provisioning.account = account;
    form.reset();
    element("license-account-id").value = account.account_id;
    renderProvisioning();
    showToast(t("account_created"));
  } catch (caught) {
    error.textContent = errorText(caught);
    error.hidden = false;
  } finally {
    setBusy(submit, false);
  }
}

async function handleIssueLicense(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!csrfToken || !form.reportValidity()) return;
  const { submit, error } = provisioningFormParts(form);
  const accountId = form.elements.account_id.value.trim();
  error.hidden = true;
  setBusy(submit, true);
  try {
    const license = await apiJson(
      `/api/admin/accounts/${encodeURIComponent(accountId)}/licenses`,
      { method: "POST", mutate: true },
    );
    state.provisioning.license = license;
    document.querySelector("#device-bind-form [name='license_id']").value = license.license_id;
    document.querySelector("#device-replace-form [name='license_id']").value = license.license_id;
    document.querySelector("#activation-issue-form [name='license_id']").value = license.license_id;
    renderProvisioning();
    showToast(t("license_issued"));
  } catch (caught) {
    error.textContent = errorText(caught);
    error.hidden = false;
  } finally {
    setBusy(submit, false);
  }
}

async function handleBindDevice(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!csrfToken || !form.reportValidity()) return;
  const { submit, error } = provisioningFormParts(form);
  const data = new FormData(form);
  const licenseId = String(data.get("license_id") || "").trim();
  const deviceLabel = String(data.get("device_label") || "").trim();
  error.hidden = true;
  setBusy(submit, true);
  try {
    const device = await apiJson(
      `/api/admin/licenses/${encodeURIComponent(licenseId)}/devices`,
      {
        method: "POST",
        mutate: true,
        body: {
          device_key_fingerprint: String(data.get("device_key_fingerprint") || "").trim(),
          platform: data.get("platform"),
          architecture: data.get("architecture"),
          device_label: deviceLabel || null,
        },
      },
    );
    state.provisioning.device = device;
    document.querySelector("#device-replace-form [name='license_id']").value = device.license_id;
    document.querySelector("#device-replace-form [name='current_device_key_fingerprint']").value = device.device_key_fingerprint;
    document.querySelector("#activation-issue-form [name='license_id']").value = device.license_id;
    renderProvisioning();
    showToast(t("device_bound"));
  } catch (caught) {
    error.textContent = errorText(caught);
    error.hidden = false;
  } finally {
    setBusy(submit, false);
  }
}

async function handleReplaceDevice(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!csrfToken || !form.reportValidity()) return;
  const { submit, error } = provisioningFormParts(form);
  const data = new FormData(form);
  const licenseId = String(data.get("license_id") || "").trim();
  const currentFingerprint = String(
    data.get("current_device_key_fingerprint") || "",
  ).trim();
  const newFingerprint = String(
    data.get("new_device_key_fingerprint") || "",
  ).trim();
  const newDeviceLabel = String(data.get("new_device_label") || "").trim();
  const reason = String(data.get("replacement_reason") || "").trim();
  if (!reason) {
    error.textContent = t("required_replacement_reason");
    error.hidden = false;
    form.elements.replacement_reason.focus();
    return;
  }
  if (!window.confirm(t("replacement_confirm", {
    current: shortId(currentFingerprint),
    replacement: shortId(newFingerprint),
  }))) return;
  error.hidden = true;
  setBusy(submit, true);
  try {
    const replacement = await apiJson(
      `/api/admin/licenses/${encodeURIComponent(licenseId)}/devices/replace`,
      {
        method: "POST",
        mutate: true,
        body: {
          current_device_key_fingerprint: currentFingerprint,
          new_device_key_fingerprint: newFingerprint,
          new_platform: data.get("new_platform"),
          new_architecture: data.get("new_architecture"),
          new_device_label: newDeviceLabel || null,
          replacement_reason: reason,
        },
      },
    );
    state.provisioning.replacement = replacement;
    form.reset();
    form.elements.license_id.value = replacement.license_id;
    form.elements.current_device_key_fingerprint.value = (
      replacement.device_key_fingerprint
    );
    renderProvisioning();
    showToast(t("device_replaced"));
  } catch (caught) {
    error.textContent = errorText(caught);
    error.hidden = false;
  } finally {
    setBusy(submit, false);
  }
}

function showActivationCredential(issued) {
  if (
    !issued
    || typeof issued.license_key !== "string"
    || typeof issued.license_id !== "string"
    || typeof issued.version !== "string"
  ) {
    throw new ApiError(502, "service_unavailable");
  }
  element("activation-key-value").value = issued.license_key;
  element("activation-key-meta").textContent = t("activation_meta", {
    license: shortId(issued.license_id),
    version: issued.version,
    expires: formatDate(issued.expires_at),
  });
  element("activation-key-dialog").showModal();
}

function clearActivationCredential() {
  element("activation-key-value").value = "";
  element("activation-key-meta").textContent = "";
}

async function handleIssueActivation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!csrfToken || !form.reportValidity()) return;
  const { submit, error } = provisioningFormParts(form);
  const licenseId = form.elements.license_id.value.trim();
  const version = form.elements.version.value.trim();
  if (!window.confirm(t("activation_issue_confirm", { license: shortId(licenseId), version }))) return;
  error.hidden = true;
  setBusy(submit, true);
  try {
    const issued = await apiJson(
      `/api/admin/licenses/${encodeURIComponent(licenseId)}/versions/${encodeURIComponent(version)}/activation-credentials`,
      { method: "POST", mutate: true },
    );
    showActivationCredential(issued);
    form.elements.version.value = "";
    showToast(t("activation_key_issued"));
  } catch (caught) {
    error.textContent =
      caught instanceof ApiError && caught.status === 401
        ? t("activation_not_authorized")
        : errorText(caught);
    error.hidden = false;
  } finally {
    setBusy(submit, false);
  }
}

async function copyActivationCredential() {
  const field = element("activation-key-value");
  if (!field.value) return;
  try {
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
      throw new Error("clipboard unavailable");
    }
    await navigator.clipboard.writeText(field.value);
    showToast(t("key_copied"));
  } catch (_error) {
    field.focus();
    field.select();
    showToast(t("copy_unavailable"), true);
  }
}

function locale() {
  return state.language === "ru" ? "ru-RU" : "en-GB";
}

function formatDate(value) {
  if (!value) return t("not_recorded");
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return t("not_available");
  return new Intl.DateTimeFormat(locale(), { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function formatMoney(minor, currency) {
  const amount = Number(minor);
  if (!Number.isFinite(amount) || typeof currency !== "string") return t("not_available");
  try {
    const formatter = new Intl.NumberFormat(locale(), { style: "currency", currency });
    const digits = formatter.resolvedOptions().maximumFractionDigits;
    return formatter.format(amount / (10 ** digits));
  } catch (_error) {
    return `${amount} ${currency}`;
  }
}

function shortId(value) {
  if (typeof value !== "string") return t("unknown");
  return value.length > 20 ? `${value.slice(0, 9)}…${value.slice(-7)}` : value;
}

const statusKeys = {
  pending: "state_pending",
  under_review: "state_under_review",
  approved: "state_approved",
  rejected: "state_rejected",
  draft: "draft",
  published: "published_at",
};

function statusPill(status) {
  const known = Object.hasOwn(statusKeys, status) ? status : "unknown";
  return node("span", {
    className: `status-pill status-${known}`,
    text: t(statusKeys[status] || "unknown"),
  });
}

function tableCell(labelKey, content, className = "") {
  const cell = node("td", { className });
  cell.dataset.label = t(labelKey);
  if (content instanceof Node) cell.append(content);
  else cell.textContent = String(content ?? t("not_available"));
  return cell;
}

function actionButton(key, action, { style = "secondary", write = false } = {}) {
  const button = node("button", {
    className: `button button-${style} button-small`,
    text: t(key),
    type: "button",
  });
  if (write && !csrfToken) button.disabled = true;
  button.addEventListener("click", action);
  return button;
}

function renderPayments() {
  const body = element("payments-body");
  if (!body) return;
  body.replaceChildren();
  const filter = element("payment-filter")?.value || "";
  const visible = filter ? state.payments.filter((item) => item.state === filter) : state.payments;
  for (const payment of visible) {
    const row = node("tr");
    row.append(
      tableCell("version", payment.version || t("unknown"), "cell-mono"),
      tableCell("license", shortId(payment.license_id), "cell-mono"),
      tableCell("amount", formatMoney(payment.amount_minor, payment.currency)),
      tableCell("paid_at", formatDate(payment.paid_at)),
      tableCell("status", statusPill(payment.state)),
    );
    const evidenceActions = node("div", { className: "cell-actions" });
    evidenceActions.append(actionButton("view_evidence", () => openEvidence(payment)));
    row.append(tableCell("evidence", evidenceActions));
    const actions = node("div", { className: "cell-actions" });
    if (payment.state === "pending") {
      actions.append(actionButton("start_review", () => startReview(payment), { style: "primary", write: true }));
    } else if (payment.state === "under_review") {
      actions.append(
        actionButton("approve", () => openDecision("approve", payment), { style: "primary", write: true }),
        actionButton("reject", () => openDecision("reject", payment), { style: "quiet", write: true }),
      );
    } else {
      actions.append(node("span", { className: "cell-mono", text: t("not_available") }));
    }
    row.append(tableCell("actions", actions));
    body.append(row);
  }
  element("payments-empty").hidden = visible.length !== 0;
  const pending = state.payments.filter((item) => item.state === "pending").length;
  const review = state.payments.filter((item) => item.state === "under_review").length;
  element("pending-count").textContent = String(pending);
  element("queue-summary").textContent = t("queue_summary", { shown: visible.length, pending, review });
}

function replacePayment(updated, previous) {
  const normalized = { ...previous, ...updated, version: updated.version || previous.version };
  state.payments = state.payments.map((item) => item.id === previous.id ? normalized : item);
  renderPayments();
  return normalized;
}

async function startReview(payment) {
  try {
    const updated = await apiJson(`/api/admin/payments/${encodeURIComponent(payment.id)}/review`, {
      method: "POST",
      mutate: true,
    });
    replacePayment(updated, payment);
    showToast(t("review_started"));
  } catch (error) {
    showToast(errorText(error), true);
  }
}

function evidenceMeta(payment) {
  return t("evidence_meta", {
    version: payment.version || t("unknown"),
    license: shortId(payment.license_id),
    amount: formatMoney(payment.amount_minor, payment.currency),
  });
}

function revokeEvidenceUrl() {
  if (evidenceObjectUrl) URL.revokeObjectURL(evidenceObjectUrl);
  evidenceObjectUrl = "";
  element("evidence-image").removeAttribute("src");
}

async function openEvidence(payment) {
  state.evidencePayment = payment;
  revokeEvidenceUrl();
  element("evidence-meta").textContent = evidenceMeta(payment);
  element("evidence-loading").hidden = false;
  element("evidence-error").hidden = true;
  element("evidence-image").hidden = true;
  element("evidence-dialog").showModal();
  try {
    const response = await fetch(`/api/admin/payments/${encodeURIComponent(payment.id)}/evidence`, {
      headers: { Accept: "image/png, image/jpeg, image/webp" },
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!response.ok) throw new ApiError(response.status, "evidence_failed");
    const contentType = (response.headers.get("Content-Type") || "").split(";", 1)[0];
    if (!["image/png", "image/jpeg", "image/webp"].includes(contentType)) {
      throw new ApiError(502, "evidence_type_invalid");
    }
    const blob = await response.blob();
    evidenceObjectUrl = URL.createObjectURL(blob);
    element("evidence-image").src = evidenceObjectUrl;
    element("evidence-image").hidden = false;
  } catch (error) {
    element("evidence-error").textContent = errorText(error, "evidence_failed");
    element("evidence-error").hidden = false;
  } finally {
    element("evidence-loading").hidden = true;
  }
}

function openDecision(kind, payment) {
  if (!csrfToken) {
    showToast(t("changes_locked"), true);
    return;
  }
  state.decision = { kind, payment };
  configureDecisionDialog();
  element("decision-error").hidden = true;
  element("reject-reason").value = "";
  element("decision-dialog").showModal();
}

function configureDecisionDialog() {
  if (!state.decision) return;
  const { kind, payment } = state.decision;
  const approve = kind === "approve";
  element("decision-eyebrow").textContent = t(approve ? "decision_approve_eyebrow" : "decision_reject_eyebrow");
  element("decision-title").textContent = t(approve ? "decision_approve_title" : "decision_reject_title", { version: payment.version });
  element("decision-copy").textContent = t(approve ? "decision_approve_copy" : "decision_reject_copy", { version: payment.version });
  element("reject-reason-label").hidden = approve;
  element("reject-reason").required = !approve;
  const confirm = element("decision-confirm");
  confirm.textContent = t(approve ? "approve_and_grant" : "reject_payment");
  confirm.className = `button ${approve ? "button-primary" : "button-danger"}`;
}

async function handleDecision(event) {
  event.preventDefault();
  if (!state.decision) return;
  const { kind, payment } = state.decision;
  const reason = element("reject-reason").value.trim();
  if (kind === "reject" && !reason) {
    element("decision-error").textContent = t("required_reason");
    element("decision-error").hidden = false;
    element("reject-reason").focus();
    return;
  }
  const confirm = element("decision-confirm");
  setBusy(confirm, true);
  try {
    const path = `/api/admin/payments/${encodeURIComponent(payment.id)}/${kind}`;
    const result = await apiJson(path, {
      method: "POST",
      mutate: true,
      body: kind === "reject" ? { reason } : undefined,
    });
    replacePayment(kind === "approve" ? result.payment : result, payment);
    element("decision-dialog").close();
    state.decision = null;
    void loadAudit();
    showToast(t(kind === "approve" ? "payment_approved" : "payment_rejected"));
  } catch (error) {
    element("decision-error").textContent = errorText(error);
    element("decision-error").hidden = false;
  } finally {
    setBusy(confirm, false);
  }
}

function combinedReleases() {
  const publishedIds = new Set(state.releases.map((item) => item.id));
  return [...state.sessionDrafts.filter((item) => !publishedIds.has(item.id)), ...state.releases];
}

function renderReleases() {
  const grid = element("release-grid");
  if (!grid) return;
  grid.replaceChildren();
  const releases = combinedReleases();
  for (const release of releases) {
    const card = node("article", { className: "release-card" });
    const top = node("div");
    top.append(statusPill(release.state), node("h2", { text: release.version }));
    top.append(node("p", { className: "release-card-meta", text: shortId(release.id) }));
    const footer = node("div", { className: "release-card-footer" });
    const price = node("div");
    price.append(
      node("span", { className: "eyebrow", text: t("release_price") }),
      node("strong", { className: "release-price", text: formatMoney(release.price_minor, release.currency) }),
    );
    footer.append(price, actionButton("details", () => openRelease(release.id)));
    card.append(top, footer);
    grid.append(card);
  }
  element("releases-empty").hidden = releases.length !== 0;
}

async function openRelease(releaseId) {
  try {
    state.selectedRelease = await apiJson(`/api/admin/releases/${encodeURIComponent(releaseId)}`);
    renderReleaseDetail();
    element("release-dialog").showModal();
  } catch (error) {
    showToast(errorText(error, "load_failed"), true);
  }
}

function detailField(label, value) {
  const wrapper = node("div");
  wrapper.append(node("span", { text: label }), node("strong", { text: value }));
  return wrapper;
}

function renderReleaseDetail() {
  const release = state.selectedRelease;
  if (!release) return;
  element("release-dialog-title").textContent = release.version;
  element("release-detail-summary").replaceChildren(
    detailField(t("status"), t(statusKeys[release.state] || "unknown")),
    detailField(t("release_price"), formatMoney(release.price_minor, release.currency)),
    detailField(t("created_at"), formatDate(release.created_at)),
    detailField(
      t("whats_new"),
      state.language === "ru" ? (release.features_ru || t("not_provided")) : (release.features_en || t("not_provided")),
    ),
    detailField(
      t("fixes"),
      state.language === "ru" ? (release.fixes_ru || t("not_provided")) : (release.fixes_en || t("not_provided")),
    ),
  );
  const publish = element("publish-release-button");
  publish.hidden = release.state !== "draft";
  publish.disabled = !csrfToken;
  const artifacts = Array.isArray(release.artifacts) ? release.artifacts : [];
  element("artifact-count").textContent = String(artifacts.length);
  const list = element("artifact-list");
  list.replaceChildren();
  if (!artifacts.length) list.append(node("p", { className: "modal-meta", text: t("no_artifacts") }));
  for (const artifact of artifacts) {
    const record = node("article", { className: "artifact-record" });
    record.append(
      detailField(t("platform"), `${artifact.platform} / ${artifact.architecture}`),
      detailField(t("build_number"), String(artifact.build)),
      detailField(t("artifact_kind"), t(artifact.artifact_kind)),
      detailField(t("verified_at"), formatDate(artifact.signature_verified_at)),
    );
    list.append(record);
  }
  const create = element("artifact-create-details");
  create.hidden = release.state !== "draft";
  create.setAttribute("aria-disabled", String(!csrfToken));
  setWriteAccess(Boolean(csrfToken));
}

async function handleCreateRelease(event) {
  event.preventDefault();
  if (!csrfToken || !event.currentTarget.reportValidity()) return;
  const form = event.currentTarget;
  const data = new FormData(form);
  const submit = form.querySelector("button[type='submit']");
  const error = form.querySelector("[data-form-error]");
  error.hidden = true;
  setBusy(submit, true);
  try {
    const release = await apiJson("/api/admin/releases", {
      method: "POST",
      mutate: true,
      body: {
        version: String(data.get("version") || "").trim(),
        price_minor: Number(data.get("price_minor")),
        currency: String(data.get("currency") || "").trim().toUpperCase(),
        features_en: String(data.get("features_en") || "").trim(),
        features_ru: String(data.get("features_ru") || "").trim(),
        fixes_en: String(data.get("fixes_en") || "").trim(),
        fixes_ru: String(data.get("fixes_ru") || "").trim(),
      },
    });
    const draft = { ...release, artifacts: [] };
    state.sessionDrafts.unshift(draft);
    form.reset();
    form.elements.currency.value = "UZS";
    element("release-create-card").hidden = true;
    renderReleases();
    showToast(t("draft_created"));
    await openRelease(release.id);
  } catch (caught) {
    error.textContent = errorText(caught);
    error.hidden = false;
  } finally {
    setBusy(submit, false);
  }
}

async function handleAddArtifact(event) {
  event.preventDefault();
  if (!csrfToken || !state.selectedRelease || !event.currentTarget.reportValidity()) return;
  const form = event.currentTarget;
  const data = new FormData(form);
  const submit = form.querySelector("button[type='submit']");
  const error = form.querySelector("[data-form-error]");
  error.hidden = true;
  setBusy(submit, true);
  try {
    const compatible = String(data.get("compatible_source_versions") || "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    const artifact = await apiJson(`/api/admin/releases/${encodeURIComponent(state.selectedRelease.id)}/artifacts`, {
      method: "POST",
      mutate: true,
      body: {
        platform: data.get("platform"),
        architecture: data.get("architecture"),
        artifact_kind: data.get("artifact_kind"),
        build: Number(data.get("build")),
        sha256: String(data.get("sha256") || "").trim(),
        byte_size: Number(data.get("byte_size")),
        storage_key: String(data.get("storage_key") || "").trim(),
        signature: String(data.get("signature") || "").trim(),
        signing_key_id: String(data.get("signing_key_id") || "").trim(),
        compatible_source_versions: compatible,
      },
    });
    state.selectedRelease.artifacts = [...(state.selectedRelease.artifacts || []), artifact];
    form.reset();
    element("artifact-create-details").open = false;
    renderReleaseDetail();
    showToast(t("artifact_added"));
  } catch (caught) {
    error.textContent = errorText(caught);
    error.hidden = false;
  } finally {
    setBusy(submit, false);
  }
}

async function publishSelectedRelease() {
  const release = state.selectedRelease;
  if (!release || !csrfToken || !window.confirm(t("publish_confirm", { version: release.version }))) return;
  const button = element("publish-release-button");
  setBusy(button, true);
  try {
    const published = await apiJson(`/api/admin/releases/${encodeURIComponent(release.id)}/publish`, {
      method: "POST",
      mutate: true,
    });
    state.selectedRelease = { ...release, ...published };
    state.sessionDrafts = state.sessionDrafts.filter((item) => item.id !== release.id);
    await loadReleases();
    renderReleaseDetail();
    showToast(t("release_published"));
  } catch (error) {
    showToast(errorText(error), true);
  } finally {
    setBusy(button, false);
  }
}

function renderAudit() {
  const body = element("audit-body");
  if (!body) return;
  body.replaceChildren();
  for (const event of state.audit) {
    const row = node("tr");
    row.append(
      tableCell("decision", statusPill(event.decision)),
      tableCell("payment", shortId(event.payment_id), "cell-mono"),
      tableCell("operator", event.actor_admin_subject || t("unknown"), "cell-mono"),
      tableCell("reason", event.reason || t("not_recorded")),
      tableCell("recorded_at", formatDate(event.occurred_at)),
    );
    body.append(row);
  }
  element("audit-empty").hidden = state.audit.length !== 0;
}

let recoveryCodes = [];

function renderSecurity() {
  renderMfaStatus();
  renderSessions();
}

async function loadSecurity() {
  const results = await Promise.allSettled([loadMfaStatus(), loadSessions()]);
  if (results.some((result) => result.status === "rejected")) {
    showToast(t("load_failed"), true);
  }
}

async function loadMfaStatus() {
  state.security.mfa = await apiJson("/api/admin/mfa");
  renderMfaStatus();
}

async function loadSessions() {
  const payload = await apiJson("/api/admin/sessions");
  state.security.sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
  renderSessions();
}

const mfaStateKeys = {
  not_enrolled: "mfa_state_not_enrolled",
  enrolling: "mfa_state_enrolling",
  active: "mfa_state_active",
  disabled: "mfa_state_disabled",
};

function renderMfaStatus() {
  const pill = element("mfa-state-pill");
  if (!pill) return;
  const mfa = state.security.mfa;
  const stateName = mfa?.state || "not_enrolled";
  pill.textContent = t(mfaStateKeys[stateName] || "unknown");
  pill.className = `status-pill status-${stateName === "active" ? "approved" : stateName === "disabled" ? "rejected" : "pending"}`;
  const active = stateName === "active";
  const enrolling = stateName === "enrolling";
  const summary = element("mfa-status-summary");
  if (active) {
    summary.textContent = t("mfa_summary_active", {
      count: mfa?.recovery_codes_remaining ?? 0,
    });
  } else if (mfa?.mandatory) {
    summary.textContent = t("mfa_summary_mandatory");
  } else {
    summary.textContent = t("mfa_summary_optional");
  }
  element("mfa-enroll-button").hidden = active || enrolling || !csrfToken;
  element("mfa-regenerate-button").hidden = !active || !csrfToken;
  element("mfa-disable-button").hidden = !active || !csrfToken;
  element("mfa-enroll-card").hidden = !enrolling;
}

function assuranceLabel(assurance) {
  return t(assurance === "mfa_satisfied" ? "assurance_full" : "assurance_pending");
}

function renderSessions() {
  const body = element("sessions-body");
  if (!body) return;
  body.replaceChildren();
  for (const session of state.security.sessions) {
    const row = node("tr");
    row.append(
      tableCell("session_created", formatDate(session.created_at)),
      tableCell("session_last_seen", formatDate(session.last_seen_at)),
      tableCell("session_expires", formatDate(session.expires_at)),
      tableCell("assurance", assuranceLabel(session.assurance)),
    );
    const actions = node("div", { className: "cell-actions" });
    if (session.current) {
      actions.append(node("span", { className: "cell-mono", text: t("this_device") }));
    }
    actions.append(
      actionButton("revoke_session", () => revokeSession(session), { style: "quiet", write: true }),
    );
    row.append(tableCell("actions", actions));
    body.append(row);
  }
  element("sessions-empty").hidden = state.security.sessions.length !== 0;
}

function revokeMfaQrUrl() {
  if (mfaQrObjectUrl) URL.revokeObjectURL(mfaQrObjectUrl);
  mfaQrObjectUrl = "";
  element("mfa-qr-image").removeAttribute("src");
}

async function handleReauth(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const { submit, error } = provisioningFormParts(form);
  const totpInput = element("reauth-totp");
  const recoveryInput = element("reauth-recovery");
  const totp = totpInput.value.trim();
  const recovery = recoveryInput.value.trim();
  if (!csrfToken || (!totp && !recovery) || !form.reportValidity()) {
    error.textContent = t("reauth_code_required");
    error.hidden = false;
    return;
  }
  const body = totp ? { totp } : { recovery_code: recovery };
  totpInput.value = "";
  recoveryInput.value = "";
  error.hidden = true;
  setBusy(submit, true);
  try {
    const issued = await apiJson("/api/admin/session/reauth", {
      method: "POST",
      mutate: true,
      body,
    });
    csrfToken = issued.csrf_token;
    state.session = { subject: issued.subject, expires_at: issued.expires_at };
    setWriteAccess(true);
    await loadSessions();
    showToast(t("reauth_complete"));
  } catch (caught) {
    error.textContent = caught instanceof ApiError && caught.status === 401
      ? t("mfa_code_invalid")
      : errorText(caught);
    error.hidden = false;
  } finally {
    body.totp = "";
    body.recovery_code = "";
    setBusy(submit, false);
  }
}

async function handlePasswordChange(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const { submit, error } = provisioningFormParts(form);
  if (!csrfToken || !form.reportValidity()) return;
  const currentInput = element("current-password");
  const newInput = element("new-password");
  const confirmInput = element("confirm-new-password");
  if (newInput.value !== confirmInput.value) {
    error.textContent = t("passwords_do_not_match");
    error.hidden = false;
    return;
  }
  const body = {
    current_password: currentInput.value,
    new_password: newInput.value,
  };
  currentInput.value = "";
  newInput.value = "";
  confirmInput.value = "";
  error.hidden = true;
  setBusy(submit, true);
  try {
    await apiJson("/api/admin/password", {
      method: "POST",
      mutate: true,
      body,
    });
    csrfToken = "";
    state.session = null;
    state.security = { mfa: null, sessions: [], enrolling: false };
    showAuth({ message: t("password_changed_sign_in") });
    showToast(t("password_changed_sign_in"));
  } catch (caught) {
    error.textContent = caught instanceof ApiError && caught.status === 401
      ? t("current_password_invalid")
      : errorText(caught);
    error.hidden = false;
  } finally {
    body.current_password = "";
    body.new_password = "";
    setBusy(submit, false);
  }
}

async function beginEnrollment() {
  if (!csrfToken) {
    showToast(t("changes_locked"), true);
    return;
  }
  try {
    const start = await apiJson("/api/admin/mfa/enrollment", {
      method: "POST",
      mutate: true,
    });
    state.security.enrolling = true;
    element("mfa-secret").value = start.secret_base32;
    element("mfa-activate-code").value = "";
    await loadMfaStatus();
    element("mfa-enroll-card").hidden = false;
    await loadEnrollmentQr();
    showToast(t("enrollment_started_toast"));
  } catch (error) {
    element("mfa-status-error").textContent = errorText(error);
    element("mfa-status-error").hidden = false;
  }
}

async function loadEnrollmentQr() {
  revokeMfaQrUrl();
  element("mfa-qr-error").hidden = true;
  element("mfa-qr-image").hidden = true;
  try {
    const response = await fetch("/api/admin/mfa/enrollment/qr", {
      headers: { Accept: "image/png" },
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!response.ok) throw new ApiError(response.status, "qr_failed");
    const blob = await response.blob();
    mfaQrObjectUrl = URL.createObjectURL(blob);
    element("mfa-qr-image").src = mfaQrObjectUrl;
    element("mfa-qr-image").hidden = false;
  } catch (error) {
    element("mfa-qr-error").textContent = errorText(error, "qr_failed");
    element("mfa-qr-error").hidden = false;
  }
}

function cancelEnrollment() {
  revokeMfaQrUrl();
  state.security.enrolling = false;
  element("mfa-enroll-card").hidden = true;
  element("mfa-secret").value = "";
  void loadMfaStatus();
}

async function handleActivateMfa(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!csrfToken || !form.reportValidity()) return;
  const { submit, error } = provisioningFormParts(form);
  error.hidden = true;
  setBusy(submit, true);
  try {
    const result = await apiJson("/api/admin/mfa/enrollment/activate", {
      method: "POST",
      mutate: true,
      body: { totp: element("mfa-activate-code").value.trim() },
    });
    csrfToken = result.csrf_token;
    revokeMfaQrUrl();
    state.security.enrolling = false;
    element("mfa-secret").value = "";
    element("mfa-activate-code").value = "";
    showRecoveryCodes(result.recovery_codes);
    await loadSecurity();
    setWriteAccess(Boolean(csrfToken));
    showToast(t("mfa_enabled_toast"));
  } catch (caught) {
    error.textContent =
      caught instanceof ApiError && caught.status === 401
        ? t("mfa_code_invalid")
        : errorText(caught);
    error.hidden = false;
  } finally {
    setBusy(submit, false);
  }
}

async function regenerateRecovery() {
  if (!csrfToken || !window.confirm(t("regenerate_confirm"))) return;
  try {
    const result = await apiJson("/api/admin/mfa/recovery/regenerate", {
      method: "POST",
      mutate: true,
    });
    showRecoveryCodes(result.recovery_codes);
    await loadMfaStatus();
    showToast(t("recovery_regenerated_toast"));
  } catch (error) {
    showToast(errorText(error), true);
  }
}

async function disableMfa() {
  if (!csrfToken || !window.confirm(t("disable_mfa_confirm"))) return;
  try {
    await apiJson("/api/admin/mfa/disable", {
      method: "POST",
      mutate: true,
      body: { reset: false },
    });
    csrfToken = "";
    state.session = null;
    state.security = { mfa: null, sessions: [], enrolling: false };
    showAuth({ message: t("mfa_disabled_toast") });
    showToast(t("mfa_disabled_toast"));
  } catch (error) {
    showToast(errorText(error), true);
  }
}

async function revokeSession(session) {
  if (!csrfToken || !window.confirm(t("revoke_session_confirm"))) return;
  try {
    await apiJson(`/api/admin/sessions/${encodeURIComponent(session.session_id)}`, {
      method: "DELETE",
      mutate: true,
    });
    if (session.current) {
      csrfToken = "";
      state.session = null;
      showAuth({ message: t("session_revoked_toast") });
    } else {
      await loadSessions();
    }
    showToast(t("session_revoked_toast"));
  } catch (error) {
    showToast(errorText(error), true);
  }
}

async function revokeAllSessions() {
  if (!csrfToken || !window.confirm(t("revoke_all_confirm"))) return;
  try {
    await apiJson("/api/admin/sessions/revoke-all", {
      method: "POST",
      mutate: true,
    });
    csrfToken = "";
    state.session = null;
    showAuth({ message: t("sessions_revoked_toast") });
    showToast(t("sessions_revoked_toast"));
  } catch (error) {
    showToast(errorText(error), true);
  }
}

function showRecoveryCodes(codes) {
  recoveryCodes = Array.isArray(codes) ? codes.slice() : [];
  const list = element("recovery-code-list");
  list.replaceChildren();
  for (const code of recoveryCodes) {
    list.append(node("li", { className: "recovery-code-item", text: code }));
  }
  element("recovery-dialog").showModal();
}

function clearRecoveryCodes() {
  recoveryCodes = [];
  element("recovery-code-list").replaceChildren();
}

function downloadRecovery() {
  if (!recoveryCodes.length) return;
  const text = `JARVIS admin recovery codes\n\n${recoveryCodes.join("\n")}\n`;
  const blob = new Blob([text], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const link = node("a");
  link.href = url;
  link.download = "jarvis-recovery-codes.txt";
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  showToast(t("codes_downloaded"));
}

function printRecovery() {
  if (!recoveryCodes.length) return;
  window.print();
}

function switchView(view) {
  if (view === "security" && csrfToken) void loadSecurity();
  document.querySelectorAll("[data-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== view;
  });
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  element("workspace").focus();
}

async function refreshSection(name, button) {
  setBusy(button, true);
  try {
    if (name === "payments") await loadPayments();
    if (name === "releases") await loadReleases();
    if (name === "audit") await loadAudit();
  } catch (error) {
    showToast(errorText(error, "load_failed"), true);
  } finally {
    setBusy(button, false);
  }
}

function closeDialog(dialog) {
  if (dialog.open) dialog.close();
}

function attachEvents() {
  document.querySelectorAll("[data-language]").forEach((button) => {
    button.addEventListener("click", () => {
      state.language = button.dataset.language;
      translateStaticPage();
    });
  });
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  document.querySelectorAll("[data-refresh]").forEach((button) => {
    button.addEventListener("click", () => refreshSection(button.dataset.refresh, button));
  });
  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => closeDialog(element(button.dataset.closeDialog)));
  });
  element("login-form").addEventListener("submit", handleLogin);
  element("auth-cancel").addEventListener("click", showApp);
  element("session-action").addEventListener("click", handleSessionAction);
  element("unlock-button").addEventListener("click", () => showAuth({ allowCancel: true }));
  element("payment-filter").addEventListener("change", renderPayments);
  element("decision-form").addEventListener("submit", handleDecision);
  element("release-create-form").addEventListener("submit", handleCreateRelease);
  element("artifact-create-form").addEventListener("submit", handleAddArtifact);
  element("account-create-form").addEventListener("submit", handleCreateAccount);
  element("license-issue-form").addEventListener("submit", handleIssueLicense);
  element("device-bind-form").addEventListener("submit", handleBindDevice);
  element("device-replace-form").addEventListener("submit", handleReplaceDevice);
  element("activation-issue-form").addEventListener("submit", handleIssueActivation);
  element("copy-activation-key").addEventListener("click", copyActivationCredential);
  element("publish-release-button").addEventListener("click", publishSelectedRelease);
  element("new-release-button").addEventListener("click", () => {
    element("release-create-card").hidden = false;
    element("release-create-form").elements.version.focus();
  });
  element("cancel-release-create").addEventListener("click", () => {
    element("release-create-card").hidden = true;
  });
  element("evidence-dialog").addEventListener("close", () => {
    revokeEvidenceUrl();
    state.evidencePayment = null;
  });
  element("decision-dialog").addEventListener("close", () => {
    state.decision = null;
  });
  element("activation-key-dialog").addEventListener("close", clearActivationCredential);
  element("mfa-enroll-button").addEventListener("click", beginEnrollment);
  element("mfa-enroll-cancel").addEventListener("click", cancelEnrollment);
  element("mfa-activate-form").addEventListener("submit", handleActivateMfa);
  element("reauth-form").addEventListener("submit", handleReauth);
  element("password-change-form").addEventListener("submit", handlePasswordChange);
  element("mfa-regenerate-button").addEventListener("click", regenerateRecovery);
  element("mfa-disable-button").addEventListener("click", disableMfa);
  element("revoke-all-sessions").addEventListener("click", revokeAllSessions);
  element("download-recovery").addEventListener("click", downloadRecovery);
  element("print-recovery").addEventListener("click", printRecovery);
  element("recovery-dialog").addEventListener("close", clearRecoveryCodes);
  element("artifact-create-details").addEventListener("toggle", (event) => {
    if (event.currentTarget.open && !csrfToken) {
      event.currentTarget.open = false;
      showToast(t("changes_locked"), true);
    }
  });
}

async function initialize() {
  try {
    const response = await fetch(new URL("i18n.json", document.baseURI), {
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error("translation request failed");
    translations = await response.json();
    if (!translations.en || !translations.ru) throw new Error("translation catalog invalid");
  } catch (_error) {
    element("login-error").textContent = "Language resources unavailable / Языковые ресурсы недоступны";
    element("login-error").hidden = false;
    return;
  }
  state.language = navigator.language.toLowerCase().startsWith("ru") ? "ru" : "en";
  attachEvents();
  translateStaticPage();
  await restoreSession();
}

void initialize();
