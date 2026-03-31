from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Header
from sqlalchemy.orm import Session
from typing import List, Optional
from collections import defaultdict
import logging
from datetime import datetime

from database.database import get_db
from database.models import MaterialOrder, MaterialLine, CuttingPlan, OptimizationStatus, TenantEnvironment
from api.schemas import (
    MaterialOrderCreate, MaterialOrderResponse,
    CuttingPlanResponse, WebhookPayload
)
from services.optimizer import CuttingOptimizer
from services.mkg_client import get_mkg_client, get_mkg_client_for_env, get_trace_log, clear_trace_log
from auth.dependencies import get_current_user
from config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# ASCII zaagplan generator
# ---------------------------------------------------------------------------

def _ascii_cutting_plan(order: MaterialOrder, result: dict) -> str:
    """Genereer een leesbare ASCII tabel van het zaagplan voor in de MKG memo."""
    settings = get_settings()
    now = datetime.utcnow().strftime("%d-%m-%Y %H:%M")
    lines = []

    def hr(left="├", mid="┼", right="┤", fill="─", widths=None):
        return left + mid.join(fill * w for w in widths) + right

    lines.append("╔" + "═" * 62 + "╗")
    lines.append("║  ZAAGPLAN" + " " * 52 + "║")
    lines.append("╠" + "═" * 62 + "╣")
    lines.append(f"║  Aanvraag : {order.order_id:<49}║")
    lines.append(f"║  Artikel  : {order.article_code:<49}║")
    lines.append(f"║  Staaflengte: {int(order.stock_length)} mm" + " " * (47 - len(str(int(order.stock_length)))) + "║")
    lines.append(f"║  Gegenereerd: {now:<48}║")
    lines.append("╠" + "═" * 62 + "╣")

    summary = result.get("summary", {})
    lines.append(f"║  Staven nodig : {result.get('total_stock_used','?'):<45}║")
    lines.append(f"║  Totaal stukken: {summary.get('total_pieces','?'):<44}║")
    lines.append(f"║  Gem. efficiëntie: {summary.get('average_efficiency',0):.1f}%{' ':<41}║")
    lines.append(f"║  Totaal afval : {result.get('total_waste',0):.0f} mm{' ':<43}║")
    lines.append("╠" + "═" * 62 + "╣")

    # Staven tabel
    # Kolom breedtes: staaf(6) snedes(36) stuks(6) eff(12)
    W = [6, 36, 6, 10]
    lines.append("║ " + "┌" + "┬".join("─" * w for w in W) + "┐" + " ║")
    def row(cells):
        parts = [f" {c:<{W[i]}} " for i, c in enumerate(cells)]
        return "║ │" + "│".join(parts) + "│ ║"
    lines.append(row(["Staaf", "Snedes (lengte×aantal)", "Stuks", "Efficiëntie"]))
    lines.append("║ " + "├" + "┼".join("─" * (w+2) for w in W) + "┤" + " ║")

    cutting_plan = result.get("cutting_plan", [])
    for stock in cutting_plan:
        # Groepeer snedes per lengte
        counts: dict = {}
        for cut in stock.get("cuts", []):
            l = int(cut["length"])
            counts[l] = counts.get(l, 0) + 1
        pills = "  ".join(f"{l}×{n}" for l, n in sorted(counts.items(), reverse=True))
        if len(pills) > W[1]:
            pills = pills[:W[1]-1] + "…"
        total_cuts = sum(counts.values())
        eff = stock.get("efficiency", 0)
        lines.append(row([
            str(stock["stock_number"]),
            pills,
            str(total_cuts),
            f"{eff:.1f}%",
        ]))

    lines.append("║ " + "└" + "┴".join("─" * (w+2) for w in W) + "┘" + " ║")
    lines.append("╚" + "═" * 62 + "╝")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Background optimization task
# ---------------------------------------------------------------------------

async def process_cutting_optimization(order_id: int, db: Session):
    """Background task to process cutting optimization"""
    cutting_plan = None
    try:
        order = db.query(MaterialOrder).filter(MaterialOrder.id == order_id).first()
        if not order:
            return

        cutting_plan = db.query(CuttingPlan).filter(CuttingPlan.order_id == order_id).first()
        if not cutting_plan:
            cutting_plan = CuttingPlan(order_id=order_id, status=OptimizationStatus.PROCESSING)
            db.add(cutting_plan)
        else:
            cutting_plan.status = OptimizationStatus.PROCESSING

        db.commit()

        # Optimalisatie uitvoeren
        required_pieces = []
        for line in order.material_lines:
            required_pieces.append({
                'length': line.required_length,
                'quantity': line.quantity,
                'line_number': line.line_number
            })

        optimizer = CuttingOptimizer(stock_length=order.stock_length, saw_kerf=3.0)
        result = optimizer.optimize(required_pieces)

        cutting_plan.status = OptimizationStatus.COMPLETED
        cutting_plan.total_stock_used = result['total_stock_used']
        cutting_plan.total_waste = result['total_waste']
        cutting_plan.waste_percentage = result['waste_percentage']
        cutting_plan.optimization_data = result
        cutting_plan.completed_at = datetime.utcnow()
        db.commit()

        logger.info(f"Optimalisatie klaar voor order {order_id}")

        # MKG terugkoppeling alleen als use_mkg=True in de tenant-omgeving
        if order.mkg_document and order.mkg_rowkey and order.user_id:
            env = db.query(TenantEnvironment).filter(
                TenantEnvironment.user_id == order.user_id
            ).first()
            if env and env.use_mkg:
                await _send_plan_to_mkg(order, cutting_plan, result, db, env=env)
            else:
                logger.info(f"MKG terugkoppeling overgeslagen (use_mkg=False) voor order {order_id}")

    except Exception as e:
        logger.error(f"Optimalisatie mislukt voor order {order_id}: {e}")
        if cutting_plan:
            cutting_plan.status = OptimizationStatus.FAILED
            cutting_plan.error_message = str(e)
            db.commit()


async def _send_plan_to_mkg(order: MaterialOrder, cutting_plan: CuttingPlan, result: dict, db: Session, env=None):
    """Stuur zaagplan ASCII tabel + document-URL terug naar MKG iofa memo velden."""
    settings = get_settings()

    # Determine which client + base URL to use
    if env is not None:
        client = get_mkg_client_for_env(env)
        base_url = env.mkg_base_url or ""
        context_path = env.mkg_context_path or "/mkg"
    else:
        client = get_mkg_client()
        base_url = settings.mkg_base_url or ""
        context_path = settings.mkg_context_path or "/mkg"

    doc_url = (
        f"{base_url}{context_path}"
        f"/web/v3/MKG/Documents/{order.mkg_document}/{order.mkg_rowkey}"
    )

    try:
        ascii_table = _ascii_cutting_plan(order, result)
        logger.info(f"MKG terugkoppeling → document={order.mkg_document}, rowkey={order.mkg_rowkey}")
        logger.debug(f"ASCII tabel:\n{ascii_table}")

        await client.update_production_order_memo(
            document=order.mkg_document,
            rowkey=order.mkg_rowkey,
            memo_extern=ascii_table,
            memo_intern=doc_url,
        )
        logger.info(f"MKG memo bijgewerkt voor iofa {order.mkg_iofa_num}")
    except Exception as e:
        # Terugkoppeling mislukt = geen reden om het zaagplan als failed te markeren
        logger.error(f"MKG terugkoppeling mislukt voor order {order.order_id}: {e}")

@router.get("/webhook/mkg/{webhook_token}", status_code=200)
async def mkg_webhook_verify(
    webhook_token: str,
    db: Session = Depends(get_db)
):
    """GET verification endpoint — MKG probes this URL before sending events."""
    env = db.query(TenantEnvironment).filter(
        TenantEnvironment.webhook_token == webhook_token
    ).first()
    if not env:
        raise HTTPException(status_code=404, detail="Unknown webhook token")
    return {"status": "ok", "webhook": "ready"}


@router.post("/webhook/mkg/{webhook_token}", status_code=202)
async def mkg_webhook(
    webhook_token: str,
    payload: WebhookPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """Per-tenant webhook endpoint for MKG events.
    Each user gets a unique webhook URL: /api/webhook/mkg/{webhook_token}
    """
    # ── Resolve the tenant environment ───────────────────────────────────────
    env = db.query(TenantEnvironment).filter(
        TenantEnvironment.webhook_token == webhook_token
    ).first()
    if not env:
        raise HTTPException(status_code=404, detail="Unknown webhook token")

    user_id = env.user_id
    use_mkg = env.use_mkg
    logger.info(f"=== WEBHOOK RECEIVED === user_id={user_id}")
    logger.info(f"Type: {payload.type}, Timestamp: {payload.timestamp}")
    logger.info(f"Data: {payload.data}")

    # Extraheer document en rowkey uit het MKG webhook formaat
    rowkey = payload.data.get("rowkey")
    document = payload.data.get("document")

    if not rowkey:
        raise HTTPException(status_code=422, detail="data.rowkey ontbreekt in webhook payload")
    if document is None:
        raise HTTPException(status_code=422, detail="data.document ontbreekt in webhook payload")

    logger.info(f"Verwerken: document={document}, rowkey={rowkey}")

    created_orders = []

    try:
        if use_mkg:
            # -----------------------------------------------------------
            # ECHTE MKG DATA
            # -----------------------------------------------------------
            logger.info("USE_MKG=true — ophalen header + prmv regels uit MKG...")
            client = get_mkg_client_for_env(env)

            # Stap 1: basisinformatie van de iofa ophalen
            try:
                header = await client.get_production_order_header(document, rowkey)
                logger.info(f"MKG iofa header: {header}")
            except Exception as e:
                logger.warning(f"Kon iofa header niet ophalen: {e} — doorgaan zonder")
                header = {}

            # iofa_num uit header, fallback naar document nummer
            iofa_num = (
                header.get("iofa_num")
                or header.get("IoFa_Num")
                or header.get("IOFA_NUM")
                or str(document)
            )
            logger.info(f"iofa_num = {iofa_num}")

            # Stap 2: prmv materiaalregels ophalen
            try:
                prmv_lines = await client.get_production_order_materials(
                    document, rowkey
                )
                logger.info(f"MKG prmv opgehaald: {len(prmv_lines)} regels")
            except Exception as e:
                logger.error(f"Kon prmv niet ophalen uit MKG: {e}")
                raise HTTPException(status_code=502, detail=f"MKG prmv ophalen mislukt: {e}")

            if not prmv_lines:
                raise HTTPException(
                    status_code=422,
                    detail="Geen materiaalregels gevonden. Controleer table/rowkey."
                )

            # Groepeer per arti_code — voor elk artikel een apart zaagplan
            lines_by_article: dict = defaultdict(list)
            stock_per_article: dict = {}
            for i, line in enumerate(prmv_lines, start=1):
                arti_code = line.get("arti_code") or "UNKNOWN"
                stock_per_article[arti_code] = float(
                    line.get("arti_code.arti_mat_lengte") or 6000.0
                )
                required_length = float(line.get("prmv_lengte") or 0)
                quantity = int(float(line.get("totaal_aantal") or 1))
                if required_length <= 0:
                    logger.warning(f"Regel {i}: prmv_lengte=0, overgeslagen")
                    continue
                lines_by_article[arti_code].append({
                    "line_number": int(line.get("prmv_num") or i),
                    "required_length": required_length,
                    "quantity": quantity,
                    "description": f"Regel {line.get('prdr_num', '')}-{line.get('prmv_num', i)}",
                    "mkg_reference": line.get("RowKey"),
                })
                logger.info(f"  [{arti_code}] regel {i}: {required_length}mm × {quantity}")

        else:
            # -----------------------------------------------------------
            # DUMMY DATA
            # -----------------------------------------------------------
            logger.info("USE_MKG=false — dummy data wordt gebruikt")
            iofa_num = str(document)
            lines_by_article = {
                "ST 1.0503 45X33": [
                    {"line_number": 1, "required_length": 340.0,  "quantity": 18, "description": "Staander kort",    "mkg_reference": None},
                    {"line_number": 2, "required_length": 580.0,  "quantity": 12, "description": "Ligger midden",    "mkg_reference": None},
                    {"line_number": 3, "required_length": 210.0,  "quantity": 25, "description": "Verbindingsstuk",  "mkg_reference": None},
                    {"line_number": 4, "required_length": 760.0,  "quantity": 8,  "description": "Draagbalk",        "mkg_reference": None},
                    {"line_number": 5, "required_length": 130.0,  "quantity": 30, "description": "Koppelstuk klein", "mkg_reference": None},
                    {"line_number": 6, "required_length": 920.0,  "quantity": 6,  "description": "Hoofdbalk",        "mkg_reference": None},
                    {"line_number": 7, "required_length": 450.0,  "quantity": 14, "description": "Tussenregel",      "mkg_reference": None},
                    {"line_number": 8, "required_length": 275.0,  "quantity": 20, "description": "Steunstuk",        "mkg_reference": None},
                ]
            }
            stock_per_article = {"ST 1.0503 45X33": 6000.0}

        # -------------------------------------------------------------------
        # Sla per artikel een MaterialOrder + regels op en start optimalisatie
        # -------------------------------------------------------------------
        for arti_code, lines in lines_by_article.items():
            stock_length = stock_per_article.get(arti_code, 6000.0)
            # order_id = iofa_num + artikel — uniek per artikel per aanvraag
            order_id = f"{iofa_num}-{arti_code}"

            # Als het order al bestaat (herhaalde webhook), verwijder dan de oude
            # versie zodat het zaagplan opnieuw berekend wordt met actuele MKG data
            existing = db.query(MaterialOrder).filter(
                MaterialOrder.order_id == order_id,
                MaterialOrder.user_id == user_id,
            ).first()
            if existing:
                logger.info(f"Order {order_id} bestaat al — wordt overschreven met actuele data")
                old_plan = db.query(CuttingPlan).filter(
                    CuttingPlan.order_id == existing.id
                ).first()
                if old_plan:
                    db.delete(old_plan)
                db.delete(existing)
                db.commit()

            logger.info(f"Aanmaken order: {order_id} | staaf {stock_length}mm | {len(lines)} regels")
            material_order = MaterialOrder(
                order_id=order_id,
                article_code=arti_code,
                stock_length=stock_length,
                user_id=user_id,
                mkg_iofa_num=iofa_num,
                mkg_document=document,
                mkg_rowkey=rowkey,
            )
            db.add(material_order)
            db.flush()

            for line_data in lines:
                db.add(MaterialLine(
                    order_id=material_order.id,
                    line_number=line_data["line_number"],
                    required_length=line_data["required_length"],
                    quantity=line_data["quantity"],
                    description=line_data.get("description"),
                    mkg_reference=line_data.get("mkg_reference"),
                ))

            db.commit()
            background_tasks.add_task(process_cutting_optimization, material_order.id, db)
            created_orders.append({
                "internal_id": material_order.id,
                "order_id": order_id,
                "article_code": arti_code,
                "lines": len(lines),
            })

        logger.info(f"=== WEBHOOK PROCESSED === {len(created_orders)} zaagplan(nen) aangemaakt")
        return {
            "status": "accepted",
            "source": "mkg" if use_mkg else "dummy",
            "tenant_user_id": user_id,
            "document": document,
            "iofa_num": iofa_num,
            "rowkey": rowkey,
            "cutting_plans_created": len(created_orders),
            "orders": created_orders,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"=== WEBHOOK PROCESSING FAILED === {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Webhook verwerkingsfout: {str(e)}")


@router.put("/webhook/mkg/{webhook_token}", status_code=202)
async def mkg_webhook_put(
    webhook_token: str,
    payload: WebhookPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """PUT alias for the MKG webhook — delegates to the POST handler."""
    return await mkg_webhook(webhook_token, payload, background_tasks, db)


@router.post("/orders", response_model=MaterialOrderResponse)
async def create_order(
    order: MaterialOrderCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Create material order and start optimization"""
    # Check if order exists for this user
    existing = db.query(MaterialOrder).filter(
        MaterialOrder.order_id == order.order_id,
        MaterialOrder.user_id == current_user.id,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Order already exists")
    
    # Create order
    db_order = MaterialOrder(
        order_id=order.order_id,
        article_code=order.article_code,
        stock_length=order.stock_length,
        user_id=current_user.id,
    )
    db.add(db_order)
    db.flush()
    
    # Add lines
    for line in order.material_lines:
        db_line = MaterialLine(
            order_id=db_order.id,
            **line.model_dump()
        )
        db.add(db_line)
    
    db.commit()
    db.refresh(db_order)
    
    # Start optimization
    background_tasks.add_task(process_cutting_optimization, db_order.id, db)
    
    return db_order

@router.get("/orders", response_model=List[MaterialOrderResponse])
def list_orders(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all material orders for the current user"""
    orders = db.query(MaterialOrder).filter(
        MaterialOrder.user_id == current_user.id
    ).offset(skip).limit(limit).all()
    return orders

@router.get("/orders/{order_id}", response_model=MaterialOrderResponse)
def get_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get specific order (must belong to current user)"""
    order = db.query(MaterialOrder).filter(
        MaterialOrder.id == order_id,
        MaterialOrder.user_id == current_user.id,
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@router.get("/cutting-plans/{order_id}", response_model=CuttingPlanResponse)
def get_cutting_plan(
    order_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get cutting plan for order (must belong to current user)"""
    plan = db.query(CuttingPlan).join(MaterialOrder).filter(
        CuttingPlan.order_id == order_id,
        MaterialOrder.user_id == current_user.id,
    ).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Cutting plan not found")
    return plan

@router.get("/cutting-plans", response_model=List[CuttingPlanResponse])
def list_cutting_plans(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all cutting plans for the current user"""
    plans = db.query(CuttingPlan).join(MaterialOrder).filter(
        MaterialOrder.user_id == current_user.id
    ).order_by(CuttingPlan.created_at.desc()).offset(skip).limit(limit).all()
    return plans


# ---------------------------------------------------------------------------
# MKG Debug / Probe endpoints
# ---------------------------------------------------------------------------

@router.get("/mkg/trace")
async def mkg_trace():
    """Geeft de laatste MKG API calls terug (request + response) voor debugging."""
    return {"calls": get_trace_log(), "total": len(get_trace_log())}


@router.delete("/mkg/trace")
async def mkg_trace_clear():
    """Wist de trace log."""
    clear_trace_log()
    return {"status": "cleared"}


@router.get("/mkg/probe/{document}/{rowkey}")
async def mkg_probe(
    document: int,
    rowkey: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Roept MKG aan voor het opgegeven document + rowkey en toont de ruwe response.
    Gebruikt de MKG-instellingen van de ingelogde gebruiker.
    Slaat NIETS op — puur voor inspectie.

    Voorbeeld: GET /api/mkg/probe/242/0x0000000008a0f385
    """
    env = db.query(TenantEnvironment).filter(
        TenantEnvironment.user_id == current_user.id
    ).first()
    if not env or not env.mkg_base_url:
        raise HTTPException(
            status_code=400,
            detail="Geen MKG omgeving geconfigureerd. Stel eerst je omgeving in via /settings."
        )

    clear_trace_log()
    client = get_mkg_client_for_env(env)
    result = {"document": document, "rowkey": rowkey, "steps": []}

    # Stap 1: Login
    step_login = {"step": "login"}
    try:
        ok = await client._login()
        step_login["success"] = ok
        step_login["jsessionid_obtained"] = bool(client.jsessionid)
    except Exception as e:
        step_login["success"] = False
        step_login["error"] = str(e)
    result["steps"].append(step_login)

    if not client.jsessionid:
        result["error"] = "Login mislukt — controleer MKG credentials in .env"
        result["trace"] = get_trace_log()
        return result

    # Stap 2: header ophalen
    step_header = {"step": "get_header",
                   "endpoint": f"/web/v3/MKG/Documents/{document}/{rowkey}"}
    header = {}
    try:
        header = await client.get_production_order_header(document, rowkey)
        step_header["success"] = True
        step_header["response"] = header
        step_header["iofa_num"] = (
            header.get("iofa_num") or header.get("IoFa_Num") or header.get("IOFA_NUM") or str(document)
        )
    except Exception as e:
        step_header["success"] = False
        step_header["error"] = str(e)
    result["steps"].append(step_header)

    # Stap 3: prmv regels ophalen
    step_prmv = {"step": "get_prmv",
                 "endpoint": f"/web/v3/MKG/Documents/{document}/{rowkey}/prmv"}
    prmv_lines = []
    try:
        prmv_lines = await client.get_production_order_materials(document, rowkey)
        step_prmv["success"] = True
        step_prmv["count"] = len(prmv_lines)
        step_prmv["response"] = prmv_lines
    except Exception as e:
        step_prmv["success"] = False
        step_prmv["error"] = str(e)
    result["steps"].append(step_prmv)

    articles: dict = {}
    for line in prmv_lines:
        arti = line.get("arti_code", "UNKNOWN")
        articles[arti] = articles.get(arti, 0) + 1

    result["trace"] = get_trace_log()
    result["summary"] = {
        "iofa_num": step_header.get("iofa_num", str(document)),
        "total_lines": len(prmv_lines),
        "articles": articles,
        "cutting_plans_to_create": len(articles),
        "field_names": list(prmv_lines[0].keys()) if prmv_lines else [],
        "header_field_names": list(header.keys()) if header else [],
    }
    return result


@router.get("/mkg/status")
def mkg_status(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Laat zien of USE_MKG aan staat en wat de MKG configuratie is voor de huidige gebruiker."""
    settings = get_settings()
    env = db.query(TenantEnvironment).filter(
        TenantEnvironment.user_id == current_user.id
    ).first()

    base_url = env.mkg_base_url if env else None
    context_path = (env.mkg_context_path if env else None) or "/mkg"
    use_mkg = env.use_mkg if env else False
    username = env.mkg_username if env else None
    webhook_token = env.webhook_token if env else None
    webhook_url = f"{settings.app_base_url}/api/webhook/mkg/{webhook_token}" if webhook_token else None

    return {
        "use_mkg": use_mkg,
        "mkg_base_url": base_url,
        "mkg_context_path": context_path,
        "login_url": f"{base_url}{context_path}/static/auth/j_spring_security_check" if base_url else None,
        "mkg_username": username,
        "webhook_url": webhook_url,
        "webhook_payload_example": {
            "type": "update_iofa",
            "timestamp": "2026-03-26T22:23:57.000",
            "data": {
                "document": 242,
                "rowkey": "0x0000000008a0f385"
            }
        },
        "probe_url_example": "/api/mkg/probe/242/0x0000000008a0f385",
        "note": "Zet use_mkg=true in je omgevingsinstellingen om echte MKG data te gebruiken.",
    }
