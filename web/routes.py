from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional

from database.database import get_db
from database.models import MaterialOrder, CuttingPlan, OptimizationStatus, User, PurchaseOrder, PurchaseOrderLine
from auth.dependencies import get_current_user_optional

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


def _require_login(current_user, redirect="/login"):
    """Return a redirect response when user is not logged in, else None."""
    if not current_user:
        return RedirectResponse(redirect, status_code=302)
    return None


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Dashboard with statistics"""
    redir = _require_login(current_user)
    if redir:
        return redir

    total_orders = db.query(MaterialOrder).filter(
        MaterialOrder.user_id == current_user.id
    ).count()

    stats = {
        'total_orders': total_orders,
        'processing': db.query(CuttingPlan).join(MaterialOrder).filter(
            MaterialOrder.user_id == current_user.id,
            CuttingPlan.status == OptimizationStatus.PROCESSING
        ).count(),
        'completed': db.query(CuttingPlan).join(MaterialOrder).filter(
            MaterialOrder.user_id == current_user.id,
            CuttingPlan.status.in_([
                OptimizationStatus.COMPLETED,
                OptimizationStatus.GEOPTIMALISEERD,
                OptimizationStatus.INKOOPORDER_AANGEMAAKT,
            ])
        ).count(),
        'failed': db.query(CuttingPlan).join(MaterialOrder).filter(
            MaterialOrder.user_id == current_user.id,
            CuttingPlan.status == OptimizationStatus.FAILED
        ).count(),
    }

    recent_plans = (
        db.query(CuttingPlan)
        .join(MaterialOrder)
        .filter(MaterialOrder.user_id == current_user.id)
        .order_by(CuttingPlan.created_at.desc())
        .limit(10)
        .all()
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {"stats": stats, "recent_plans": recent_plans, "current_user": current_user},
    )


@router.get("/cutting-plans", response_class=HTMLResponse)
async def cutting_plans_list(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """List all cutting plans for the current user"""
    redir = _require_login(current_user)
    if redir:
        return redir

    plans = (
        db.query(CuttingPlan)
        .join(MaterialOrder)
        .filter(MaterialOrder.user_id == current_user.id)
        .order_by(CuttingPlan.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request,
        "cutting_plans_list.html",
        {"plans": plans, "current_user": current_user},
    )


@router.get("/cutting-plans/{order_id}", response_class=HTMLResponse)
async def cutting_plan_detail(
    request: Request,
    order_id: int,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Cutting plan detail view"""
    redir = _require_login(current_user)
    if redir:
        return redir

    plan = (
        db.query(CuttingPlan)
        .join(MaterialOrder)
        .filter(
            CuttingPlan.order_id == order_id,
            MaterialOrder.user_id == current_user.id,
        )
        .first()
    )
    if not plan:
        return templates.TemplateResponse(
            request,
            "404.html",
            {"current_user": current_user},
            status_code=404,
        )

    return templates.TemplateResponse(
        request,
        "cutting_plan_detail.html",
        {"plan": plan, "order": plan.order, "current_user": current_user},
    )


@router.get("/purchase-orders/{purchase_order_id}", response_class=HTMLResponse)
async def purchase_order_confirm(
    request: Request,
    purchase_order_id: int,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Bevestigingspagina voor een aangemaakte inkooporder."""
    redir = _require_login(current_user)
    if redir:
        return redir

    po = (
        db.query(PurchaseOrder)
        .filter(
            PurchaseOrder.id == purchase_order_id,
            PurchaseOrder.user_id == current_user.id,
        )
        .first()
    )
    if not po:
        return templates.TemplateResponse(
            request,
            "404.html",
            {"current_user": current_user},
            status_code=404,
        )

    # Haal bijbehorende zaagplan-orders op voor weergave
    related_orders = (
        db.query(MaterialOrder)
        .filter(
            MaterialOrder.order_id.in_(po.cutting_plan_order_ids or []),
            MaterialOrder.user_id == current_user.id,
        )
        .all()
    ) if po.cutting_plan_order_ids else []

    return templates.TemplateResponse(
        request,
        "purchase_order_confirm.html",
        {
            "po": po,
            "related_orders": related_orders,
            "current_user": current_user,
        },
    )
