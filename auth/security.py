"""
Security utilities: password hashing, JWT tokens, Fernet encryption for stored secrets.
"""
from datetime import datetime, timedelta
from typing import Optional
import hashlib
import base64

import bcrypt
from jose import JWTError, jwt
from cryptography.fernet import Fernet

from config import get_settings


def _get_fernet() -> Fernet:
    """Derive a Fernet key from the application's secret_key."""
    settings = get_settings()
    raw = hashlib.sha256(settings.secret_key.encode()).digest()
    key = base64.urlsafe_b64encode(raw)
    return Fernet(key)


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ── Symmetric encryption for stored API credentials ───────────────────────────

def encrypt_secret(value: str) -> str:
    """Encrypt a sensitive string for storage in the database."""
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    """Decrypt a stored secret."""
    return _get_fernet().decrypt(value.encode()).decode()


# ── JWT helpers ───────────────────────────────────────────────────────────────

ALGORITHM = "HS256"


def create_access_token(user_id: int, expire_minutes: Optional[int] = None) -> str:
    settings = get_settings()
    minutes = expire_minutes if expire_minutes is not None else settings.access_token_expire_minutes
    expire = datetime.utcnow() + timedelta(minutes=minutes)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        return None
