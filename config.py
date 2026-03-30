from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Optional


class Settings(BaseSettings):
    # Database
    database_url: str = "sqlite:///./cutting_platform.db"

    # Application
    secret_key: str = "change-me-in-production"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    app_base_url: str = "http://localhost:8000"

    # JWT
    access_token_expire_minutes: int = 60 * 24  # 24 hours

    # Optimizer defaults
    default_optimizer_algorithm: str = "ffd"  # ffd, bfd, nf
    default_saw_kerf: float = 3.0  # mm

    # ── Global / fallback MKG settings (optional) ────────────────────────────
    # These are only used when a user has no personal environment configured.
    # In multi-tenant mode, credentials come from TenantEnvironment per user.
    mkg_base_url: Optional[str] = None
    mkg_context_path: str = "/mkg"
    mkg_api_key: Optional[str] = None
    mkg_username: Optional[str] = None
    mkg_password: Optional[str] = None
    use_mkg: bool = False

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings():
    return Settings()
