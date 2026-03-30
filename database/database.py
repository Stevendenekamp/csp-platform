from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import Base
from config import get_settings

settings = get_settings()

# Railway levert DATABASE_URL als "postgres://..." maar SQLAlchemy vereist "postgresql://..."
_db_url = settings.database_url
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    _db_url,
    connect_args={"check_same_thread": False} if "sqlite" in _db_url else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _apply_migrations():
    """Voeg ontbrekende kolommen toe (idempotent)."""
    migrations = [
        # Legacy material_orders columns
        "ALTER TABLE material_orders ADD COLUMN mkg_iofa_num VARCHAR",
        "ALTER TABLE material_orders ADD COLUMN mkg_document INTEGER",
        "ALTER TABLE material_orders ADD COLUMN mkg_rowkey VARCHAR",
        # Multi-tenant: link orders to users (nullable for backward compat)
        "ALTER TABLE material_orders ADD COLUMN user_id INTEGER REFERENCES users(id)",
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                # Column already exists → rollback and continue (required for PostgreSQL)
                conn.rollback()


def init_db():
    # Voor PostgreSQL: maak enum type atomisch aan met IF NOT EXISTS
    # zodat meerdere gunicorn workers niet met elkaar botsen
    if "postgresql" in _db_url:
        with engine.connect() as conn:
            conn.execute(text(
                "CREATE TYPE IF NOT EXISTS optimizationstatus AS ENUM "
                "('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')"
            ))
            conn.commit()

    Base.metadata.create_all(bind=engine)
    _apply_migrations()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
