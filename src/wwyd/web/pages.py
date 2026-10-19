# SPDX-License-Identifier: AGPL-3.0-or-later
"""The four pages: sign in & consent, account, users & invites, one-time-link landing.

All handlers are ``async`` so database access stays on the event loop thread; only argon2
hashing runs in a worker thread. Every POST form carries a CSRF token: the session's token
when signed in, otherwise a double-submit token from the ``wwyd_pre`` cookie.
"""

from __future__ import annotations

import hmac
import logging
import sqlite3
from importlib import resources
from typing import Any
from urllib.parse import quote, urlsplit

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from wwyd.app import AppState
from wwyd.auth import accounts, clock, tokens
from wwyd.auth.accounts import AccountError, WebSession
from wwyd.auth.mail import send_reset_link
from wwyd.auth.middleware import IP_KEY
from wwyd.auth.scopes import DESCRIPTIONS, KINDS, make_scope, pick_requested
from wwyd.auth.throttle import Throttled
from wwyd.errors import Invalid, NotFound
from wwyd.limits import USER_LIMIT_KEYS, get_limiter
from wwyd.limits.defaults import FLOAT_LIMIT_KEYS

log = logging.getLogger("wwyd.web")

SESSION_COOKIE = "wwyd_session"
PRE_COOKIE = "wwyd_pre"
templates = Jinja2Templates(directory=str(resources.files("wwyd.web") / "templates"))
router = APIRouter()

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",  # one-time tokens live in page URLs
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self';"
        " form-action 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none';"
        " base-uri 'none'"
    ),
}


class _Halt(Exception):
    def __init__(self, response: Response) -> None:
        self.response = response


# --- helpers ---------------------------------------------------------------------------


def _state(request: Request) -> AppState:
    return request.app.state.wwyd


def _conn(request: Request) -> sqlite3.Connection:
    return _state(request).conn


def _ip(request: Request) -> str:
    return request.scope.get(IP_KEY) or (request.client.host if request.client else "unknown")


def _session(request: Request) -> WebSession | None:
    if "wwyd_session" not in request.scope:
        request.scope["wwyd_session"] = accounts.get_session(
            _conn(request), request.cookies.get(SESSION_COOKIE)
        )
    return request.scope["wwyd_session"]


def _csrf(request: Request) -> str:
    sess = _session(request)
    if sess is not None:
        return sess.csrf_token
    token = request.cookies.get(PRE_COOKIE) or request.scope.get("wwyd_new_pre")
    if not token:
        token = clock.new_secret()
        request.scope["wwyd_new_pre"] = token
    return token


def _secure(request: Request) -> bool:
    return _state(request).config.secure_cookies


def _finish(request: Request, response: Response) -> Response:
    for k, v in _HEADERS.items():
        response.headers.setdefault(k, v)
    new_pre = request.scope.get("wwyd_new_pre")
    if new_pre:
        response.set_cookie(
            PRE_COOKIE, new_pre, httponly=True, samesite="lax", secure=_secure(request), path="/"
        )
    return response


def render(request: Request, template: str, status: int = 200, **ctx: Any) -> Response:
    sess = _session(request)
    base = {
        "csrf": _csrf(request),
        "me": sess.user if sess else None,
        "public_url": _state(request).config.public_url,
    }
    response = templates.TemplateResponse(request, template, {**base, **ctx}, status_code=status)
    return _finish(request, response)


def redirect(request: Request, url: str) -> Response:
    return _finish(request, RedirectResponse(url, status_code=303))


def message(request: Request, title: str, text: str, status: int = 200, **ctx: Any) -> Response:
    return render(request, "message.html", status, title=title, text=text, **ctx)


def too_many(request: Request, exc: Throttled) -> Response:
    response = message(
        request,
        "Too many attempts",
        "For your safety we paused this for a while. Please wait a few minutes and try again.",
        429,
    )
    response.headers["Retry-After"] = str(exc.retry_after)
    return response


async def _form(request: Request) -> dict[str, Any]:
    form = await request.form()
    expected = _csrf(request)
    got = form.get("csrf")
    if not isinstance(got, str) or not hmac.compare_digest(got, expected):
        raise _Halt(
            message(
                request,
                "This form expired",
                "Please go back, reload the page and try again.",
                403,
            )
        )
    out: dict[str, Any] = {}
    for key in form:
        values = [v for v in form.getlist(key) if isinstance(v, str)]
        out[key] = values if key.endswith("[]") else (values[0] if values else "")
    return out


def _require_user(request: Request) -> WebSession:
    sess = _session(request)
    if sess is None:
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise _Halt(redirect(request, "/login?next=" + quote(nxt, safe="")))
    return sess


def _require_admin(request: Request) -> WebSession:
    sess = _require_user(request)
    if not sess.user.is_admin:
        raise _Halt(
            message(request, "Not available", "Only an administrator can open this page.", 403)
        )
    return sess


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/account"


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(accounts.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=_secure(request),
        path="/",
    )


def _sign_in(request: Request, user_id: str, to: str) -> Response:
    token, _ = accounts.create_session(_conn(request), user_id)
    response = redirect(request, to)
    _set_session_cookie(request, response, token)
    response.delete_cookie(PRE_COOKIE, path="/")
    return response


def _audiences(conn: sqlite3.Connection, persona_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT name, description FROM audiences WHERE persona_id = ? ORDER BY name", (persona_id,)
    ).fetchall()


def _chosen_scope(conn: sqlite3.Connection, persona_id: str, form: dict[str, Any]):
    """Scope from a form with ``scope`` (kind) and ``audiences[]``; audiences must exist."""
    kind = form.get("scope", "")
    if kind not in KINDS:
        raise AccountError("Choose what this app may do.")
    names = sorted(set(form.get("audiences[]", [])))
    if kind == "respond":
        known = {r["name"] for r in _audiences(conn, persona_id)}
        if not names:
            raise AccountError("Tick at least one group this app may answer for.")
        if not set(names) <= known:
            raise AccountError("One of the selected groups no longer exists.")
    scope = make_scope(kind, names)
    if scope is None:
        raise AccountError("Choose what this app may do.")
    return scope


def _handle(fn):  # noqa: ANN001, ANN202 - decorator turning _Halt into its response
    import functools

    @functools.wraps(fn)
    async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Response:
        try:
            return await fn(request, *args, **kwargs)
        except _Halt as halt:
            return halt.response

    return wrapper


def _throttle(request: Request, *pairs) -> None:  # noqa: ANN002
    try:
        for throttle, key in pairs:
            throttle.check(key)
    except Throttled as exc:
        raise _Halt(too_many(request, exc)) from None


# --- / and sign in -----------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
@_handle
async def index(request: Request) -> Response:
    return redirect(request, "/account" if _session(request) else "/login")


@router.get("/login", response_class=HTMLResponse)
@_handle
async def login_page(request: Request, next: str | None = None) -> Response:  # noqa: A002
    if _session(request):
        return redirect(request, _safe_next(next))
    return render(
        request, "login.html", next=_safe_next(next), smtp=_state(request).config.smtp_enabled
    )


@router.post("/login", response_class=HTMLResponse)
@_handle
async def login_submit(request: Request) -> Response:
    form = await _form(request)
    login = str(form.get("login", "")).strip()
    password = str(form.get("password", ""))
    nxt = _safe_next(form.get("next"))
    t = _state(request).throttles
    ip, login_key = _ip(request), login.casefold()[:254]
    _throttle(request, (t.login_ip, ip), (t.login_user, login_key))
    user_id, pw_hash = accounts.password_hash_for(_conn(request), login)
    ok = await anyio.to_thread.run_sync(accounts.verify_password, pw_hash, password)
    if not ok or user_id is None:
        t.login_ip.hit(ip)
        t.login_user.hit(login_key)
        log.info("failed sign-in from %s", ip)
        return render(
            request,
            "login.html",
            401,
            next=nxt,
            login=login,
            error="That login and password do not match.",
            smtp=_state(request).config.smtp_enabled,
        )
    t.login_user.reset(login_key)
    return _sign_in(request, user_id, nxt)


@router.post("/logout")
@_handle
async def logout(request: Request) -> Response:
    await _form(request)
    sess = _session(request)
    if sess:
        accounts.end_session(_conn(request), sess.token_hash)
    response = redirect(request, "/login")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --- consent -----------------------------------------------------------------------------


async def _pending(request: Request, req: str | None):  # noqa: ANN202
    loaded = await _state(request).provider.load_pending(req or "")
    if loaded is None:
        raise _Halt(
            message(
                request,
                "This sign-in request expired",
                "Go back to the app you were connecting and start again.",
                400,
            )
        )
    return loaded


def _consent_ctx(request: Request, sess: WebSession, req: str, client, params) -> dict[str, Any]:  # noqa: ANN001
    from wwyd.auth.provider import client_display_name

    auds = _audiences(_conn(request), sess.user.persona_id)
    requested = pick_requested(params.scopes)
    known = {a["name"] for a in auds}
    return {
        "req": req,
        "client_name": client_display_name(client),
        "client_id_url": client.client_id if client.client_id.startswith("https://") else None,
        "redirect_host": urlsplit(str(params.redirect_uri)).netloc,
        "kinds": [(k, *DESCRIPTIONS[k]) for k in KINDS],
        "audiences": auds,
        "selected_kind": requested.kind if requested else None,
        "selected_audiences": [a for a in (requested.audiences if requested else ()) if a in known],
    }


@router.get("/consent", response_class=HTMLResponse)
@_handle
async def consent_page(request: Request, req: str | None = None) -> Response:
    sess = _require_user(request)
    client, params = await _pending(request, req)
    return render(request, "consent.html", **_consent_ctx(request, sess, req or "", client, params))


@router.post("/consent", response_class=HTMLResponse)
@_handle
async def consent_submit(request: Request) -> Response:
    sess = _require_user(request)
    form = await _form(request)
    req = str(form.get("req", ""))
    client, params = await _pending(request, req)
    provider = _state(request).provider
    if form.get("decision") != "allow":
        url = provider.deny(req, params)
    else:
        try:
            scope = _chosen_scope(_conn(request), sess.user.persona_id, form)
        except AccountError as exc:
            ctx = _consent_ctx(request, sess, req, client, params)
            ctx.update(
                error=str(exc),
                selected_kind=form.get("scope"),
                selected_audiences=form.get("audiences[]", []),
            )
            return render(request, "consent.html", 400, **ctx)
        url = provider.approve(req, client, params, sess.user.id, scope)
    if url is None:
        raise _Halt(message(request, "Already answered", "This request was already handled.", 400))
    return redirect(request, url)


# --- account -----------------------------------------------------------------------------


def _account(request: Request, sess: WebSession, status: int = 200, **ctx: Any) -> Response:
    conn = _conn(request)
    view = _limits_view(conn, sess.user.id, sess.user.persona_id)
    config = _state(request).config
    grants = [
        {**dict(g), "scope_label": _scope_label(g["scope"])}
        for g in tokens.list_grants(conn, sess.user.id)
    ]
    keys = [
        {**dict(k), "scope_label": _scope_label(k["scope"])}
        for k in tokens.list_api_keys(conn, sess.user.id)
    ]
    return render(
        request,
        "account.html",
        status,
        user=sess.user,
        grants=grants,
        keys=keys,
        mcp_url=config.mcp_url,
        kinds=[(k, *DESCRIPTIONS[k]) for k in KINDS],
        audiences=_audiences(conn, sess.user.persona_id),
        welcome=request.query_params.get("welcome") == "1",
        spend_cap_reached=view["spend"]["spend_capped"],
        limits=view,
        **ctx,
    )


def _scope_label(value: str) -> str:
    from wwyd.auth.scopes import parse_scope

    parsed = parse_scope(value)
    return parsed.label if parsed else value


@router.get("/account", response_class=HTMLResponse)
@_handle
async def account_page(request: Request) -> Response:
    return _account(request, _require_user(request))


@router.post("/account/password", response_class=HTMLResponse)
@_handle
async def change_password(request: Request) -> Response:
    sess = _require_user(request)
    form = await _form(request)
    t = _state(request).throttles
    _throttle(request, (t.password_change, sess.user.id))
    _, pw_hash = accounts.password_hash_for(_conn(request), sess.user.login)
    ok = await anyio.to_thread.run_sync(
        accounts.verify_password, pw_hash, str(form.get("current", ""))
    )
    if not ok:
        t.password_change.hit(sess.user.id)
        return _account(request, sess, 400, password_error="Your current password is not right.")
    try:
        new = accounts.check_new_password(str(form.get("new", "")))
    except AccountError as exc:
        return _account(request, sess, 400, password_error=str(exc))
    new_hash = await anyio.to_thread.run_sync(accounts.hash_password, new)
    accounts.set_password(_conn(request), sess.user.id, new_hash)
    ended = accounts.end_sessions(_conn(request), sess.user.id, except_hash=sess.token_hash)
    t.password_change.reset(sess.user.id)
    log.info("password changed for %s; %d other sessions ended", sess.user.id, ended)
    return _account(
        request,
        sess,
        password_ok="Password changed. You were signed out everywhere else; connected apps keep working.",
    )


@router.post("/account/keys", response_class=HTMLResponse)
@_handle
async def create_key(request: Request) -> Response:
    sess = _require_user(request)
    form = await _form(request)
    try:
        scope = _chosen_scope(_conn(request), sess.user.persona_id, form)
    except AccountError as exc:
        return _account(request, sess, 400, key_error=str(exc))
    _, raw = tokens.create_api_key(_conn(request), sess.user.id, str(form.get("name", "")), scope)
    return _account(request, sess, new_key=raw)


@router.post("/account/keys/{key_id}/revoke", response_class=HTMLResponse)
@_handle
async def revoke_key(request: Request, key_id: str) -> Response:
    sess = _require_user(request)
    await _form(request)
    tokens.revoke_api_key(_conn(request), key_id, sess.user.id)
    return redirect(request, "/account#apps")


@router.post("/account/grants/{grant_id}/revoke", response_class=HTMLResponse)
@_handle
async def revoke_grant(request: Request, grant_id: str) -> Response:
    sess = _require_user(request)
    await _form(request)
    tokens.revoke_grant(_conn(request), grant_id, sess.user.id)
    return redirect(request, "/account#apps")


@router.post("/account/limits", response_class=HTMLResponse)
@_handle
async def set_own_limits(request: Request) -> Response:
    sess = _require_user(request)
    form = await _form(request)
    try:
        get_limiter(_conn(request)).set_own_limits(sess.user.id, _limit_form(form))
    except Invalid as exc:
        return _account(request, sess, 400, limits_error=_limit_error(exc))
    return _account(request, sess, limits_ok="Your limits were saved.")


# --- limits (SPEC §11) -------------------------------------------------------------------

LIMIT_LABELS = {
    "rpm_subject": "Requests per minute, per app or key",
    "rpm_persona": "Requests per minute, all apps together",
    "predict_per_day": "Answers per day",
    "predict_per_audience_per_day": "Answers per day, per audience",
    "llm_tokens_per_day": "Language model tokens per day",
    "ingest_bytes_per_day": "Imported bytes per day",
    "batch_records": "Records per import call",
    "batch_bytes": "Bytes per import call",
    "concurrent_jobs": "Background jobs at a time",
    "spend_cap_usd": "Spending cap per month (USD)",
}


def _fmt(value: int | float | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(int(value))


def _limits_view(conn: sqlite3.Connection, user_id: str, persona_id: str) -> dict[str, Any]:
    """Limits, today's usage and the spend flag of one user, ready for a template.

    Only metric names and numbers: no persona content is read (SPEC §9.6).
    """
    limiter = get_limiter(conn)
    defaults = limiter.defaults.user_limits()
    ceilings, own = limiter.ceilings(user_id), limiter.own_limits(user_id)
    effective = limiter.effective_limits(user_id)
    rows = [
        {
            "key": k,
            "label": LIMIT_LABELS.get(k, k),
            "default": _fmt(defaults[k]) or "none",
            "ceiling": _fmt(ceilings.get(k)),
            "own": _fmt(own.get(k)),
            "effective": _fmt(effective[k]) or "none",
            "step": "0.01" if k in FLOAT_LIMIT_KEYS else "1",
        }
        for k in USER_LIMIT_KEYS
    ]
    usage = limiter.usage(user_id)
    calls = sorted(
        (m.removeprefix("calls:"), int(v)) for m, v in usage.items() if m.startswith("calls:")
    )
    spend = limiter.spend_state(user_id, persona_id)
    return {
        "rows": rows,
        "calls": calls,
        "tokens_in": int(usage.get("llm_tokens_in", 0)),
        "tokens_out": int(usage.get("llm_tokens_out", 0)),
        "cost_usd": f"{usage.get('cost_usd', 0.0):.4f}",
        "ingest_bytes": int(usage.get("ingest_bytes", 0)),
        "ingest_records": int(usage.get("ingest_records", 0)),
        "spend": spend,
        "spend_month_usd": f"{spend['spend_month_usd']:.4f}",
    }


def _limit_form(form: dict[str, Any]) -> dict[str, Any]:
    """``limit_<key>`` fields -> limit values; an empty field clears the value (null)."""
    out: dict[str, Any] = {}
    for key in USER_LIMIT_KEYS:
        name = f"limit_{key}"
        if name not in form:
            continue
        raw = str(form[name]).strip()
        if raw == "":
            out[key] = None
            continue
        try:
            out[key] = float(raw) if key in FLOAT_LIMIT_KEYS else int(raw)
        except ValueError:
            raise Invalid(f"{LIMIT_LABELS[key]} must be a number", field="limits") from None
    return out


def _limit_error(exc: Invalid) -> str:
    maximum = exc.details.get("maximum")
    if maximum is not None:
        for key in USER_LIMIT_KEYS:
            if f"limit {key} " in exc.message:
                return f"{LIMIT_LABELS[key]} may not be higher than {_fmt(maximum)}."
    for key in USER_LIMIT_KEYS:
        if f"limit {key} " in exc.message:
            kind = "a number" if key in FLOAT_LIMIT_KEYS else "a whole number"
            return f"{LIMIT_LABELS[key]} must be {kind} of 0 or more."
    return exc.message[:1].upper() + exc.message[1:] + "."


# --- admin -------------------------------------------------------------------------------


def _admin(request: Request, sess: WebSession, status: int = 200, **ctx: Any) -> Response:
    conn = _conn(request)
    users = accounts.list_users(conn)
    return render(
        request,
        "admin.html",
        status,
        users=users,
        user_limits={u.id: _limits_view(conn, u.id, u.persona_id) for u in users},
        **ctx,
    )


@router.get("/admin", response_class=HTMLResponse)
@_handle
async def admin_page(request: Request) -> Response:
    return _admin(request, _require_admin(request))


@router.post("/admin/invite", response_class=HTMLResponse)
@_handle
async def admin_invite(request: Request) -> Response:
    sess = _require_admin(request)
    await _form(request)
    token = accounts.create_link(_conn(request), "invite", created_by=sess.user.id)
    url = f"{_state(request).config.public_url}/invite?t={token}"
    return _admin(
        request, sess, link_url=url, link_text="Invite link (valid for 7 days, works once)"
    )


@router.post("/admin/users/{user_id}/reset", response_class=HTMLResponse)
@_handle
async def admin_reset(request: Request, user_id: str) -> Response:
    sess = _require_admin(request)
    await _form(request)
    target = accounts.user_by_id(_conn(request), user_id)
    if target is None:
        return _admin(request, sess, 404, error="No such user.")
    token = accounts.create_link(_conn(request), "reset", user_id=user_id, created_by=sess.user.id)
    url = f"{_state(request).config.public_url}/reset?t={token}"
    return _admin(
        request,
        sess,
        link_url=url,
        link_text=f"Password reset link for {target.login} (valid for 24 hours, works once)",
    )


@router.post("/admin/users/{user_id}/limits", response_class=HTMLResponse)
@_handle
async def admin_limits(request: Request, user_id: str) -> Response:
    sess = _require_admin(request)
    form = await _form(request)
    target = accounts.user_by_id(_conn(request), user_id)
    if target is None:
        return _admin(request, sess, 404, error="No such user.")
    try:
        get_limiter(_conn(request)).set_admin_ceilings(user_id, _limit_form(form))
    except NotFound:
        return _admin(request, sess, 404, error="No such user.")
    except Invalid as exc:
        return _admin(request, sess, 400, error=f"{target.login}: {_limit_error(exc)}")
    log.info("limit ceilings changed for %s by %s", user_id, sess.user.id)
    return redirect(request, "/admin")


@router.post("/admin/users/{user_id}/{action}", response_class=HTMLResponse)
@_handle
async def admin_disable(request: Request, user_id: str, action: str) -> Response:
    sess = _require_admin(request)
    await _form(request)
    if action not in ("disable", "enable"):
        return _admin(request, sess, 404, error="Unknown action.")
    if user_id == sess.user.id:
        return _admin(request, sess, 400, error="You cannot disable your own account.")
    if accounts.user_by_id(_conn(request), user_id) is None:
        return _admin(request, sess, 404, error="No such user.")
    accounts.set_disabled(_conn(request), user_id, action == "disable")
    log.info("user %s %sd by %s", user_id, action, sess.user.id)
    return redirect(request, "/admin")


# --- landing: setup, invite, reset ----------------------------------------------------


_LANDING_TITLES = {
    "setup": ("Create the admin account", "Create account"),
    "invite": ("You are invited", "Create account"),
    "reset": ("Choose a new password", "Save new password"),
}


def _link_or_halt(request: Request, kind: accounts.LinkKind, token: str | None) -> accounts.Link:
    t = _state(request).throttles
    ip = _ip(request)
    _throttle(request, (t.link_ip, ip))
    link = accounts.peek_link(_conn(request), kind, token)
    if link is None:
        t.link_ip.hit(ip)
        raise _Halt(
            message(
                request,
                "This link no longer works",
                "One-time links work once and expire after a while. Ask your administrator for a "
                "new one.",
                400,
            )
        )
    return link


def _landing(
    request: Request, kind: accounts.LinkKind, token: str, status: int = 200, **ctx: Any
) -> Response:
    title, button = _LANDING_TITLES[kind]
    fixed_login = None
    if kind == "reset":
        link = accounts.peek_link(_conn(request), kind, token)
        target = (
            accounts.user_by_id(_conn(request), link.user_id) if link and link.user_id else None
        )
        fixed_login = target.login if target else None
    return render(
        request,
        "landing.html",
        status,
        kind=kind,
        token=token,
        title=title,
        button=button,
        fixed_login=fixed_login,
        min_password=accounts.MIN_PASSWORD,
        **ctx,
    )


async def _landing_get(request: Request, kind: accounts.LinkKind) -> Response:
    token = request.query_params.get("t")
    _link_or_halt(request, kind, token)
    return _landing(request, kind, token or "")


async def _landing_post(request: Request, kind: accounts.LinkKind) -> Response:
    form = await _form(request)
    token = str(form.get("t", ""))
    link = _link_or_halt(request, kind, token)
    password = str(form.get("password", ""))
    if password != str(form.get("password2", "")):
        return _landing(
            request,
            kind,
            token,
            400,
            error="The two passwords are different.",
            login=form.get("login", ""),
        )
    try:
        accounts.check_new_password(password)
        login = None if kind == "reset" else accounts.check_login(str(form.get("login", "")))
    except AccountError as exc:
        return _landing(request, kind, token, 400, error=str(exc), login=form.get("login", ""))
    pw_hash = await anyio.to_thread.run_sync(accounts.hash_password, password)
    conn = _conn(request)
    try:
        from wwyd.db import transaction

        with transaction(conn):
            if not accounts.consume_link(conn, link):
                raise AccountError("This link was already used.")
            if kind == "reset":
                assert link.user_id is not None
                accounts.set_password(
                    conn, link.user_id, pw_hash, by_admin=link.created_by != "self"
                )
                accounts.end_sessions(conn, link.user_id)
                user_id = link.user_id
            else:
                if kind == "setup" and accounts.user_count(conn) > 0:
                    raise AccountError("The admin account already exists.")
                assert login is not None
                user_id = accounts.create_account(conn, login, pw_hash).id
    except AccountError as exc:
        return _landing(request, kind, token, 400, error=str(exc), login=form.get("login", ""))
    log.info("%s link used for user %s", kind, user_id)
    return _sign_in(request, user_id, "/account?welcome=1" if kind != "reset" else "/account")


@router.get("/setup", response_class=HTMLResponse)
@_handle
async def setup_page(request: Request) -> Response:
    return await _landing_get(request, "setup")


@router.post("/setup", response_class=HTMLResponse)
@_handle
async def setup_submit(request: Request) -> Response:
    return await _landing_post(request, "setup")


@router.get("/invite", response_class=HTMLResponse)
@_handle
async def invite_page(request: Request) -> Response:
    return await _landing_get(request, "invite")


@router.post("/invite", response_class=HTMLResponse)
@_handle
async def invite_submit(request: Request) -> Response:
    return await _landing_post(request, "invite")


@router.get("/reset", response_class=HTMLResponse)
@_handle
async def reset_page(request: Request) -> Response:
    if not request.query_params.get("t"):
        return render(request, "reset_request.html", smtp=_state(request).config.smtp_enabled)
    return await _landing_get(request, "reset")


@router.post("/reset", response_class=HTMLResponse)
@_handle
async def reset_submit(request: Request) -> Response:
    return await _landing_post(request, "reset")


@router.post("/reset/request", response_class=HTMLResponse)
@_handle
async def reset_request(request: Request) -> Response:
    state = _state(request)
    form = await _form(request)
    if not state.config.smtp_enabled:
        return render(request, "reset_request.html", 400, smtp=False)
    _throttle(request, (state.throttles.reset_request_ip, _ip(request)))
    state.throttles.reset_request_ip.hit(_ip(request))
    user = accounts.user_by_login(state.conn, str(form.get("login", "")))
    if user is not None and user.disabled_at is None:
        token = accounts.create_link(state.conn, "reset", user_id=user.id, created_by="self")
        url = f"{state.config.public_url}/reset?t={token}"
        try:
            await anyio.to_thread.run_sync(send_reset_link, state.config, user.login, url)
        except Exception:
            log.exception("sending a reset email failed")
    return message(
        request,
        "Check your inbox",
        "If that login exists, we sent it a link to choose a new password. It works once, "
        "for 24 hours.",
    )
