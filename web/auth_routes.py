"""
Browser-facing auth routes: login, register, logout, environment settings.
Uses HttpOnly JWT cookie for session state.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User, TenantEnvironment
from auth.security import (
    hash_password, verify_password,
    create_access_token, encrypt_secret, decrypt_secret,
)
from auth.dependencies import get_current_user_optional
from config import get_settings
import uuid

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="web/templates")

_COOKIE = "access_token"
_COOKIE_MAX_AGE = 60 * 60 * 24  # 24 hours in seconds


def _set_cookie(response, token: str):
    response.set_cookie(
        key=_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=_COOKIE_MAX_AGE,
    )


def _clear_cookie(response):
    response.delete_cookie(key=_COOKIE)


def _get_or_create_env(user: User, db: Session) -> TenantEnvironment:
    env = db.query(TenantEnvironment).filter(TenantEnvironment.user_id == user.id).first()
    if not env:
        env = TenantEnvironment(user_id=user.id)
        db.add(env)
        db.commit()
        db.refresh(env)
    return env


# ── Login ─────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, current_user=Depends(get_current_user_optional)):
    if current_user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Ongeldig e-mailadres of wachtwoord"},
            status_code=401,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Dit account is uitgeschakeld"},
            status_code=403,
        )

    token = create_access_token(user.id)
    response = RedirectResponse("/", status_code=302)
    _set_cookie(response, token)
    return response


# ── Register ──────────────────────────────────────────────────────────────────

@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, current_user=Depends(get_current_user_optional)):
    if current_user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    email: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if password != password_confirm:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Wachtwoorden komen niet overeen"},
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Wachtwoord moet minimaal 8 tekens bevatten"},
        )
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Dit e-mailadres is al in gebruik"},
        )

    user = User(
        email=email,
        username=username,
        hashed_password=hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Auto-create environment
    env = TenantEnvironment(user_id=user.id)
    db.add(env)
    db.commit()

    token = create_access_token(user.id)
    response = RedirectResponse("/settings", status_code=302)
    _set_cookie(response, token)
    logger.info(f"New user registered via web: {user.email}")
    return response


# ── Logout ────────────────────────────────────────────────────────────────────

@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    _clear_cookie(response)
    return response


# ── Environment settings ───────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login", status_code=302)

    env = _get_or_create_env(current_user, db)
    app_settings = get_settings()
    webhook_url = f"{app_settings.app_base_url}/api/webhook/mkg/{env.webhook_token}"

    return templates.TemplateResponse("environment_settings.html", {
        "request": request,
        "current_user": current_user,
        "env": env,
        "webhook_url": webhook_url,
        "saved": request.query_params.get("saved") == "1",
        "error": None,
    })


@router.post("/settings", response_class=HTMLResponse)
def settings_submit(
    request: Request,
    mkg_base_url: str = Form(""),
    mkg_context_path: str = Form("/mkg"),
    mkg_api_key: str = Form(""),
    mkg_username: str = Form(""),
    mkg_password: str = Form(""),
    use_mkg: bool = Form(False),
    default_stock_length: float = Form(6000.0),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login", status_code=302)

    env = _get_or_create_env(current_user, db)

    env.mkg_base_url = mkg_base_url.strip() or None
    env.mkg_context_path = mkg_context_path.strip() or "/mkg"
    env.mkg_api_key = mkg_api_key.strip() or None
    env.mkg_username = mkg_username.strip() or None
    env.use_mkg = use_mkg
    env.default_stock_length = default_stock_length

    # Only update password if a new one was provided
    if mkg_password.strip():
        env.mkg_password_enc = encrypt_secret(mkg_password.strip())

    db.commit()
    logger.info(f"Environment settings saved for user {current_user.email}")
    return RedirectResponse("/settings?saved=1", status_code=302)


@router.post("/settings/regen-webhook")
def regen_webhook(
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login", status_code=302)

    env = _get_or_create_env(current_user, db)
    env.webhook_token = str(uuid.uuid4())
    db.commit()
    logger.info(f"Webhook token regenerated for user {current_user.email}")
    return RedirectResponse("/settings?saved=1", status_code=302)
