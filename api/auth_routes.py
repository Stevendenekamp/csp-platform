"""
Authentication and per-user environment configuration API routes.

Endpoints:
  POST /auth/register          - create a new account
  POST /auth/login             - obtain JWT token
  GET  /auth/me                - current user info
  GET  /auth/environment       - get own MKG environment settings
  PUT  /auth/environment       - save / update MKG environment settings
  GET  /auth/webhook-info      - show the personal webhook URL + token
  POST /auth/webhook-regen     - regenerate webhook token
"""
import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User, TenantEnvironment
from auth.security import hash_password, verify_password, create_access_token, encrypt_secret, decrypt_secret
from auth.dependencies import get_current_user
from config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    email: str
    username: str
    is_active: bool
    is_admin: bool

    class Config:
        from_attributes = True


class EnvironmentRequest(BaseModel):
    mkg_base_url: Optional[str] = None
    mkg_context_path: str = "/mkg"
    mkg_api_key: Optional[str] = None
    mkg_username: Optional[str] = None
    mkg_password: Optional[str] = None   # plain text → will be encrypted before storage
    use_mkg: bool = False
    default_stock_length: float = 6000.0


class EnvironmentResponse(BaseModel):
    mkg_base_url: Optional[str]
    mkg_context_path: str
    mkg_api_key: Optional[str]
    mkg_username: Optional[str]
    mkg_password_set: bool          # don't return the password, just whether it's configured
    use_mkg: bool
    default_stock_length: float
    webhook_token: str
    webhook_url: str

    class Config:
        from_attributes = True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _env_to_response(env: TenantEnvironment) -> EnvironmentResponse:
    settings = get_settings()
    webhook_url = f"{settings.app_base_url}/api/webhook/mkg/{env.webhook_token}"
    return EnvironmentResponse(
        mkg_base_url=env.mkg_base_url,
        mkg_context_path=env.mkg_context_path,
        mkg_api_key=env.mkg_api_key,
        mkg_username=env.mkg_username,
        mkg_password_set=bool(env.mkg_password_enc),
        use_mkg=env.use_mkg,
        default_stock_length=env.default_stock_length,
        webhook_token=env.webhook_token,
        webhook_url=webhook_url,
    )


def _get_or_create_env(user: User, db: Session) -> TenantEnvironment:
    env = db.query(TenantEnvironment).filter(TenantEnvironment.user_id == user.id).first()
    if not env:
        env = TenantEnvironment(user_id=user.id)
        db.add(env)
        db.commit()
        db.refresh(env)
    return env


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """Register a new user account and return a JWT token."""
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=body.email,
        username=body.username,
        hashed_password=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Auto-create an empty TenantEnvironment
    env = TenantEnvironment(user_id=user.id)
    db.add(env)
    db.commit()

    token = create_access_token(user.id)
    logger.info(f"New user registered: {user.email}")
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Authenticate and return a JWT token."""
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Account is disabled")

    token = create_access_token(user.id)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    """Return the current user's profile."""
    return current_user


@router.get("/environment", response_model=EnvironmentResponse)
def get_environment(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the current user's MKG environment configuration."""
    env = _get_or_create_env(current_user, db)
    return _env_to_response(env)


@router.put("/environment", response_model=EnvironmentResponse)
def update_environment(
    body: EnvironmentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the user's MKG environment configuration.
    Pass mkg_password=null/omit to keep the existing password unchanged.
    Pass mkg_password='' to clear it.
    """
    env = _get_or_create_env(current_user, db)

    env.mkg_base_url = body.mkg_base_url
    env.mkg_context_path = body.mkg_context_path or "/mkg"
    env.mkg_api_key = body.mkg_api_key
    env.mkg_username = body.mkg_username
    env.use_mkg = body.use_mkg
    env.default_stock_length = body.default_stock_length

    if body.mkg_password is not None:
        # Empty string = clear password; any other value = encrypt & store
        env.mkg_password_enc = encrypt_secret(body.mkg_password) if body.mkg_password else None

    db.commit()
    db.refresh(env)
    logger.info(f"Environment updated for user {current_user.email}")
    return _env_to_response(env)


@router.get("/webhook-info")
def webhook_info(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's personal webhook URL and token."""
    env = _get_or_create_env(current_user, db)
    settings = get_settings()
    return {
        "webhook_token": env.webhook_token,
        "webhook_url": f"{settings.app_base_url}/api/webhook/mkg/{env.webhook_token}",
        "instructions": (
            "Configure this URL in MKG as the webhook target. "
            "The token uniquely identifies your environment — keep it secret."
        ),
    }


@router.post("/webhook-regen")
def regen_webhook_token(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a new webhook token (invalidates the old URL)."""
    env = _get_or_create_env(current_user, db)
    env.webhook_token = str(uuid.uuid4())
    db.commit()
    db.refresh(env)
    settings = get_settings()
    new_url = f"{settings.app_base_url}/api/webhook/mkg/{env.webhook_token}"
    logger.info(f"Webhook token regenerated for user {current_user.email}")
    return {
        "webhook_token": env.webhook_token,
        "webhook_url": new_url,
        "warning": "Update this new URL in MKG — the old URL is now invalid.",
    }
