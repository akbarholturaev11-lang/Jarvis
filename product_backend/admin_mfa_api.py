"""HTTP surface for admin MFA enrollment, step-up, and session management.

These routes are registered onto the product backend app after the core admin
routes and before the static console mount.  They depend only on the injected
session manager, MFA manager, and rate limiters, and they never place a TOTP
secret, provisioning URI, or recovery code into a log line.  The single-step
login integration lives in :func:`complete_admin_login`, which the core login
endpoint calls after a verified password.
"""

from __future__ import annotations

import io
from typing import Annotated, Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .admin_mfa import (
    LoginFactorResult,
    MfaAuditEvent,
    MfaState,
    MfaStateError,
    SQLiteAdminMfaManager,
)
from .api_auth import (
    AdminSessionManager,
    AdminSessionRecord,
    BoundedAttemptLimiter,
    IssuedAdminSession,
    SessionAssurance,
)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MfaActivateBody(_StrictBody):
    totp: str = Field(pattern=r"^[0-9]{6}$")


class MfaDisableBody(_StrictBody):
    reset: bool = False


class MfaStepUpBody(_StrictBody):
    totp: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    recovery_code: str | None = Field(default=None, min_length=8, max_length=32)


def render_qr_png(data: str) -> bytes:
    """Render an otpauth URI as a PNG QR image for same-origin blob display."""

    import qrcode

    code = qrcode.QRCode(border=2, box_size=6)
    code.add_data(data)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _set_session_cookie(
    response: Response,
    issued: IssuedAdminSession,
    *,
    cookie_name: str,
    secure_cookie: bool,
    max_age: int,
) -> None:
    response.set_cookie(
        cookie_name,
        issued.session_token,
        max_age=max_age,
        httponly=True,
        secure=secure_cookie,
        samesite="strict",
        path="/api/admin",
    )


def complete_admin_login(
    *,
    sessions: AdminSessionManager,
    mfa: SQLiteAdminMfaManager | None,
    subject: str,
    totp: str | None,
    recovery_code: str | None,
    response: Response,
    cookie_name: str,
    secure_cookie: bool,
    session_ttl_seconds: int,
    factor_limiter: BoundedAttemptLimiter,
    factor_key: str,
) -> dict[str, Any]:
    """Finish a login after a verified password, enforcing the second factor.

    ``subject`` is already password-verified.  With MFA active, a valid TOTP or
    single-use recovery code is mandatory in the same request.  Missing or wrong
    factors raise ``401`` without revealing which factor failed.  An operator who
    is not yet enrolled under a mandatory policy receives a restricted
    ``mfa_pending`` session that may only reach enrollment.
    """

    def issue(assurance: SessionAssurance, extra: dict[str, Any]) -> dict[str, Any]:
        issued = sessions.issue_session(subject, assurance=assurance)
        _set_session_cookie(
            response,
            issued,
            cookie_name=cookie_name,
            secure_cookie=secure_cookie,
            max_age=session_ttl_seconds,
        )
        return {
            "subject": issued.subject,
            "csrf_token": issued.csrf_token,
            "expires_at": issued.expires_at,
            "assurance": issued.assurance.value,
            **extra,
        }

    if mfa is None:
        return issue(SessionAssurance.MFA_SATISFIED, {})

    state = mfa.state(subject)
    if state is MfaState.ACTIVE:
        if not factor_limiter.consume(factor_key):
            raise HTTPException(
                status_code=429, detail="too many authentication attempts"
            )
        if totp:
            result = mfa.verify_login_totp(subject, totp)
        elif recovery_code:
            result = mfa.verify_recovery_code(subject, recovery_code)
        else:
            result = LoginFactorResult.INVALID
        if result is not LoginFactorResult.ACCEPTED:
            mfa.record_event(subject, MfaAuditEvent.LOGIN_FAILURE)
            raise HTTPException(status_code=401, detail="invalid admin credentials")
        factor_limiter.clear(factor_key)
        mfa.record_event(subject, MfaAuditEvent.LOGIN_SUCCESS)
        return issue(SessionAssurance.MFA_SATISFIED, {})

    if mfa.settings.allow_password_only and not mfa.settings.mandatory:
        mfa.record_event(subject, MfaAuditEvent.LOGIN_SUCCESS, detail="password only")
        return issue(SessionAssurance.MFA_SATISFIED, {})

    # Mandatory policy, second factor not yet enrolled: restricted session.
    mfa.record_event(
        subject, MfaAuditEvent.LOGIN_SUCCESS, detail="enrollment pending"
    )
    return issue(SessionAssurance.MFA_PENDING, {"mfa_enrollment_required": True})


def register_admin_security_routes(
    app: FastAPI,
    *,
    sessions: AdminSessionManager,
    mfa: SQLiteAdminMfaManager,
    cookie_name: str,
    secure_cookie: bool,
    session_ttl_seconds: int,
    require_admin_any: Callable[..., AdminSessionRecord],
    require_admin: Callable[..., AdminSessionRecord],
    require_admin_csrf: Callable[..., AdminSessionRecord],
    require_admin_any_csrf: Callable[..., AdminSessionRecord],
    attempt_key: Callable[[Request, str], str],
    enrollment_limiter: BoundedAttemptLimiter,
    stepup_limiter: BoundedAttemptLimiter,
) -> None:
    """Register MFA and session-management routes bound to injected managers."""

    def _current_token(request: Request) -> str | None:
        return request.cookies.get(cookie_name)

    def require_recent_admin(
        request: Request,
        record: AdminSessionRecord = Depends(require_admin_csrf),
    ) -> AdminSessionRecord:
        if sessions.requires_reauth(record):
            raise HTTPException(
                status_code=403,
                detail="recent authentication is required",
            )
        return record

    def _clear_cookie(response: Response) -> None:
        response.delete_cookie(
            cookie_name,
            path="/api/admin",
            secure=secure_cookie,
            httponly=True,
            samesite="strict",
        )

    @app.get("/api/admin/mfa")
    def mfa_status(
        record: AdminSessionRecord = Depends(require_admin_any),
    ) -> dict[str, Any]:
        status = mfa.status(record.subject)
        return {
            "state": status.state.value,
            "activated_at": status.activated_at,
            "recovery_codes_remaining": status.recovery_codes_remaining,
            "mandatory": mfa.settings.mandatory,
            "enrollment_required": (
                record.assurance is SessionAssurance.MFA_PENDING
                or (mfa.settings.mandatory and status.state is not MfaState.ACTIVE)
            ),
        }

    @app.post("/api/admin/mfa/enrollment", status_code=201)
    def mfa_begin_enrollment(
        request: Request,
        record: AdminSessionRecord = Depends(require_admin_any_csrf),
    ) -> dict[str, Any]:
        if not enrollment_limiter.consume(
            attempt_key(request, f"mfa-enroll|{record.subject}")
        ):
            raise HTTPException(
                status_code=429, detail="too many enrollment attempts"
            )
        try:
            start = mfa.begin_enrollment(record.subject)
        except MfaStateError:
            raise HTTPException(
                status_code=409, detail="MFA is already active"
            ) from None
        return {
            "subject": start.subject,
            "secret_base32": start.secret_base32,
            "provisioning_uri": start.provisioning_uri,
            "qr_path": "/api/admin/mfa/enrollment/qr",
            "digits": 6,
            "period": 30,
        }

    @app.get("/api/admin/mfa/enrollment/qr")
    def mfa_enrollment_qr(
        record: AdminSessionRecord = Depends(require_admin_any),
    ) -> Response:
        uri = mfa.pending_provisioning_uri(record.subject)
        if uri is None:
            raise HTTPException(status_code=404, detail="no pending enrollment")
        return Response(
            render_qr_png(uri),
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/admin/mfa/enrollment/activate")
    def mfa_activate_enrollment(
        request: Request,
        body: MfaActivateBody,
        response: Response,
        record: AdminSessionRecord = Depends(require_admin_any_csrf),
    ) -> dict[str, Any]:
        activate_key = attempt_key(request, f"mfa-activate|{record.subject}")
        if not stepup_limiter.consume(activate_key):
            raise HTTPException(status_code=429, detail="too many attempts")
        try:
            batch = mfa.activate_enrollment(record.subject, body.totp)
        except MfaStateError:
            raise HTTPException(
                status_code=409, detail="no pending enrollment"
            ) from None
        if batch is None:
            raise HTTPException(status_code=401, detail="invalid verification code")
        stepup_limiter.clear(activate_key)
        issued = sessions.rotate(
            _current_token(request),
            assurance=SessionAssurance.MFA_SATISFIED,
        )
        if issued is None:
            raise HTTPException(status_code=401, detail="session is no longer valid")
        _set_session_cookie(
            response,
            issued,
            cookie_name=cookie_name,
            secure_cookie=secure_cookie,
            max_age=session_ttl_seconds,
        )
        return {
            "subject": issued.subject,
            "csrf_token": issued.csrf_token,
            "expires_at": issued.expires_at,
            "assurance": issued.assurance.value,
            "recovery_codes": list(batch.codes),
        }

    @app.post("/api/admin/mfa/recovery/regenerate")
    def mfa_regenerate_recovery(
        record: AdminSessionRecord = Depends(require_recent_admin),
    ) -> dict[str, Any]:
        try:
            batch = mfa.regenerate_recovery_codes(record.subject)
        except MfaStateError:
            raise HTTPException(
                status_code=409, detail="MFA is not active"
            ) from None
        return {"recovery_codes": list(batch.codes)}

    @app.post("/api/admin/mfa/disable")
    def mfa_disable(
        body: MfaDisableBody,
        response: Response,
        record: AdminSessionRecord = Depends(require_recent_admin),
    ) -> dict[str, Any]:
        try:
            mfa.disable(record.subject, reset=body.reset)
        except MfaStateError:
            raise HTTPException(
                status_code=409, detail="no MFA record"
            ) from None
        # Dropping the second factor lowers assurance: end every live session.
        sessions.revoke_all_for_subject(record.subject)
        _clear_cookie(response)
        return {"state": mfa.state(record.subject).value, "sessions_revoked": True}

    @app.get("/api/admin/mfa/audit")
    def mfa_audit(
        record: AdminSessionRecord = Depends(require_admin),
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        return {
            "events": [
                {
                    "id": entry.id,
                    "subject": entry.subject,
                    "event": entry.event.value,
                    "detail": entry.detail,
                    "occurred_at": entry.occurred_at,
                }
                for entry in mfa.list_audit(subject=record.subject, limit=limit)
            ]
        }

    @app.get("/api/admin/sessions")
    def list_sessions(
        request: Request,
        record: AdminSessionRecord = Depends(require_admin),
    ) -> dict[str, Any]:
        summaries = sessions.list_sessions_for_subject(
            record.subject,
            current_token=_current_token(request),
        )
        return {
            "sessions": [
                {
                    "session_id": item.session_id,
                    "created_at": item.created_at,
                    "expires_at": item.expires_at,
                    "last_seen_at": item.last_seen_at,
                    "assurance": item.assurance.value,
                    "current": item.current,
                }
                for item in summaries
            ]
        }

    @app.delete("/api/admin/sessions/{session_id}")
    def revoke_session(
        session_id: str,
        request: Request,
        response: Response,
        record: AdminSessionRecord = Depends(require_admin_csrf),
    ) -> dict[str, Any]:
        if not sessions.revoke_session_id(record.subject, session_id):
            raise HTTPException(status_code=404, detail="session not found")
        mfa.record_event(record.subject, MfaAuditEvent.SESSION_REVOKED)
        if record.session_id == session_id:
            _clear_cookie(response)
        return {"status": "revoked", "session_id": session_id}

    @app.post("/api/admin/sessions/revoke-all")
    def revoke_all_sessions(
        response: Response,
        record: AdminSessionRecord = Depends(require_recent_admin),
    ) -> dict[str, Any]:
        revoked = sessions.revoke_all_for_subject(record.subject)
        mfa.record_event(record.subject, MfaAuditEvent.SESSIONS_REVOKED_ALL)
        _clear_cookie(response)
        return {"status": "revoked", "revoked": revoked}

    @app.post("/api/admin/session/reauth")
    def reauth(
        request: Request,
        body: MfaStepUpBody,
        response: Response,
        record: AdminSessionRecord = Depends(require_admin_csrf),
    ) -> dict[str, Any]:
        if mfa.state(record.subject) is not MfaState.ACTIVE:
            raise HTTPException(status_code=409, detail="MFA is not active")
        stepup_key = attempt_key(request, f"mfa-stepup|{record.subject}")
        if not stepup_limiter.consume(stepup_key):
            raise HTTPException(status_code=429, detail="too many attempts")
        if body.totp:
            result = mfa.verify_login_totp(record.subject, body.totp)
        elif body.recovery_code:
            result = mfa.verify_recovery_code(record.subject, body.recovery_code)
        else:
            result = LoginFactorResult.INVALID
        if result is not LoginFactorResult.ACCEPTED:
            raise HTTPException(status_code=401, detail="invalid verification code")
        stepup_limiter.clear(stepup_key)
        issued = sessions.rotate(
            _current_token(request),
            assurance=SessionAssurance.MFA_SATISFIED,
        )
        if issued is None:
            raise HTTPException(status_code=401, detail="session is no longer valid")
        _set_session_cookie(
            response,
            issued,
            cookie_name=cookie_name,
            secure_cookie=secure_cookie,
            max_age=session_ttl_seconds,
        )
        return {
            "subject": issued.subject,
            "csrf_token": issued.csrf_token,
            "expires_at": issued.expires_at,
            "assurance": issued.assurance.value,
        }


__all__ = [
    "MfaActivateBody",
    "MfaDisableBody",
    "MfaStepUpBody",
    "complete_admin_login",
    "register_admin_security_routes",
    "render_qr_png",
]
