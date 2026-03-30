from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from database.models import OptimizationStatus

class MaterialLineCreate(BaseModel):
    line_number: int
    required_length: float = Field(gt=0)
    quantity: int = Field(gt=0)
    description: Optional[str] = None
    mkg_reference: Optional[str] = None

class MaterialLineResponse(MaterialLineCreate):
    id: int
    
    class Config:
        from_attributes = True

class MaterialOrderCreate(BaseModel):
    order_id: str
    article_code: str
    stock_length: float = Field(gt=0)
    material_lines: List[MaterialLineCreate]

class MaterialOrderResponse(BaseModel):
    id: int
    order_id: str
    article_code: str
    stock_length: float
    created_at: datetime
    material_lines: List[MaterialLineResponse]
    
    class Config:
        from_attributes = True

class CuttingPlanResponse(BaseModel):
    id: int
    order_id: int
    status: OptimizationStatus
    total_stock_used: Optional[int] = None
    total_waste: Optional[float] = None
    waste_percentage: Optional[float] = None
    optimization_data: Optional[dict] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    
    class Config:
        from_attributes = True

class WebhookPayload(BaseModel):
    """MKG webhook payload - echte structuur:
    {"type":"update_iofa","timestamp":"2026-03-26T22:23:57.000","data":{"document":242,"rowkey":"0x0000000008a0f385"}}
    """
    type: str                    # bijv. "update_iofa"
    timestamp: str
    data: dict                   # bevat: document (int), rowkey (str)
