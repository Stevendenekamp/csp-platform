from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, JSON, Enum, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
import uuid

Base = declarative_base()


# ── User & tenant models ──────────────────────────────────────────────────────

class User(Base):
    """Platform account. One user = one tenant."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, nullable=False)
    hashed_password = Column(String, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    environment = relationship("TenantEnvironment", back_populates="user", uselist=False)
    material_orders = relationship("MaterialOrder", back_populates="user")


class TenantEnvironment(Base):
    """Per-user MKG environment configuration."""
    __tablename__ = "tenant_environments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)

    # MKG connection settings
    mkg_base_url = Column(String, nullable=True)
    mkg_context_path = Column(String, default="/mkg", nullable=False)
    mkg_api_key = Column(String, nullable=True)          # stored as plain (not a secret per se)
    mkg_username = Column(String, nullable=True)
    mkg_password_enc = Column(Text, nullable=True)       # Fernet-encrypted

    use_mkg = Column(Boolean, default=False, nullable=False)
    default_stock_length = Column(Float, default=6000.0, nullable=False)
    mkg_cred_num = Column(Integer, nullable=True)        # crediteur nummer voor inkooporders

    # Unique token for this tenant's webhook URL: /api/webhook/mkg/{webhook_token}
    webhook_token = Column(String, unique=True, index=True,
                           default=lambda: str(uuid.uuid4()), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="environment")


# ── Optimisation status enum ──────────────────────────────────────────────────

class OptimizationStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"              # legacy — blijft voor bestaande records
    GEOPTIMALISEERD = "geoptimaliseerd"  # nieuw na optimalisatie
    INKOOPORDER_AANGEMAAKT = "inkooporder_aangemaakt"  # na inkopen
    FAILED = "failed"

class MaterialOrder(Base):
    __tablename__ = "material_orders"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    order_id = Column(String, index=True)
    article_code = Column(String, index=True)
    stock_length = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # MKG terugkoppeling identifiers
    mkg_iofa_num = Column(String, nullable=True)   # bijv. "7526000003"
    mkg_document = Column(Integer, nullable=True)  # bijv. 242
    mkg_rowkey   = Column(String, nullable=True)   # bijv. "0x0000000008a0f385"
    
    material_lines = relationship("MaterialLine", back_populates="order")
    cutting_plan = relationship("CuttingPlan", back_populates="order", uselist=False)
    user = relationship("User", back_populates="material_orders")

class MaterialLine(Base):
    __tablename__ = "material_lines"
    
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("material_orders.id"))
    line_number = Column(Integer)
    required_length = Column(Float)
    quantity = Column(Integer)
    description = Column(String, nullable=True)
    mkg_reference = Column(String, nullable=True)
    
    order = relationship("MaterialOrder", back_populates="material_lines")

class CuttingPlan(Base):
    __tablename__ = "cutting_plans"
    
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("material_orders.id"), unique=True)
    status = Column(
        Enum(OptimizationStatus, create_type=False,
             values_callable=lambda obj: [e.value for e in obj]),
        default=OptimizationStatus.PENDING,
    )
    total_stock_used = Column(Integer)
    total_waste = Column(Float)
    waste_percentage = Column(Float)
    optimization_data = Column(JSON)  # Detailed cutting instructions
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(String, nullable=True)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=True)
    
    order = relationship("MaterialOrder", back_populates="cutting_plan")


# ── Purchase order models ─────────────────────────────────────────────────────

class PurchaseOrder(Base):
    """Inkooporder aangemaakt vanuit geselecteerde zaagplannen."""
    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    iorh_num = Column(String, nullable=False)          # bijv. "7026000002"
    admi_num = Column(Integer, nullable=True)           # administratienummer uit MKG response
    iorh_rowkey = Column(String, nullable=True)         # RowKey van de iorh in MKG
    cred_num = Column(Integer, nullable=True)           # crediteur nummer (standaard 99999)
    cutting_plan_order_ids = Column(JSON, nullable=True)  # lijst van MaterialOrder.order_id strings
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    lines = relationship("PurchaseOrderLine", back_populates="purchase_order")


class PurchaseOrderLine(Base):
    """Inkooporderregel gekoppeld aan een PurchaseOrder."""
    __tablename__ = "purchase_order_lines"

    id = Column(Integer, primary_key=True, index=True)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False, index=True)
    iorr_num = Column(Integer, nullable=True)           # regelnummer in MKG iorr
    iorr_rowkey = Column(String, nullable=True)         # RowKey van de iorr in MKG
    arti_code = Column(String, nullable=False)
    quantity = Column(Float, nullable=False)
    reservation_results = Column(JSON, nullable=True)   # resultaat per prmv-reservering

    purchase_order = relationship("PurchaseOrder", back_populates="lines")
