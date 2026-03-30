from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import Base
from config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {}
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
    Base.metadata.create_all(bind=engine)
    _apply_migrations()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
