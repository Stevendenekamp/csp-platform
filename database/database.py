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
        # Configureerbaar crediteurnummer voor inkooporders
        "ALTER TABLE tenant_environments ADD COLUMN mkg_cred_num INTEGER",
        # Status-koppeling zaagplan aan inkooporder
        "ALTER TABLE cutting_plans ADD COLUMN purchase_order_id INTEGER REFERENCES purchase_orders(id)",
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
    # Voor PostgreSQL: enum type + nieuwe waarden idempotent toevoegen
    if "postgresql" in _db_url:
        # ALTER TYPE ADD VALUE mag NIET in een transactie (PostgreSQL beperking).
        # Gebruik een aparte autocommit-verbinding.
        raw_conn = engine.raw_connection()
        try:
            raw_conn.set_isolation_level(0)  # AUTOCOMMIT
            cur = raw_conn.cursor()

            # Maak het enum type aan als het nog niet bestaat
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_type WHERE typname = 'optimizationstatus'
                    ) THEN
                        CREATE TYPE optimizationstatus AS ENUM (
                            'pending', 'processing', 'completed', 'failed',
                            'geoptimaliseerd', 'inkooporder_aangemaakt'
                        );
                    END IF;
                END$$;
            """)

            # Voeg nieuwe waarden toe als ze nog niet bestaan
            for val in ("geoptimaliseerd", "inkooporder_aangemaakt", "completed", "failed", "pending", "processing"):
                cur.execute(f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_enum e
                            JOIN pg_type t ON t.oid = e.enumtypid
                            WHERE t.typname = 'optimizationstatus'
                              AND e.enumlabel = '{val}'
                        ) THEN
                            ALTER TYPE optimizationstatus ADD VALUE '{val}';
                        END IF;
                    END$$;
                """)
            cur.close()
        finally:
            raw_conn.close()

    # checkfirst=True: sla CREATE TABLE over als tabel al bestaat
    Base.metadata.create_all(bind=engine, checkfirst=True)
    _apply_migrations()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
