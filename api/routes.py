from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Header, Request
from sqlalchemy.orm import Session
from typing import List, Optional
from collections import defaultdict
import asyncio
import httpx
import logging
from datetime import datetime

from database.database import get_db
from database.models import (
    MaterialOrder, MaterialLine, CuttingPlan, OptimizationStatus,
    TenantEnvironment, PurchaseOrder, PurchaseOrderLine,
)
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
    now = datetime.utcnow().strftime("%d-%m-%Y %H:%M")

    W    = 64          # totale breedte van elke regel
    SEP  = "=" * W
    DASH = "-" * W

    def fld(label: str, value: str) -> str:
        """Label links op vaste breedte (20), waarde daarna links uitgelijnd."""
        return f"  {label:<20} {value}"

    def fmt_mm(val) -> str:
        n = int(val) if isinstance(val, float) and val == int(val) else val
        s = f"{n:,}".replace(",", ".")
        return f"{s} mm"

    summary = result.get("summary", {})
    eff     = summary.get("average_efficiency", 0)
    waste   = result.get("total_waste", 0)

    lines = [
        SEP,
        f"  ZAAGPLAN",
        SEP,
        fld("Aanvraag      :", str(order.order_id)[:42]),
        fld("Artikel       :", str(order.article_code)[:42]),
        fld("Staaflengte   :", fmt_mm(order.stock_length)),
        fld("Gegenereerd   :", now),
        DASH,
        fld("Staven nodig  :", str(result.get("total_stock_used", "?"))),
        fld("Totaal stukken:", str(summary.get("total_pieces", "?"))),
        fld("Gem. efficientie:", f"{eff:.1f}%"),
        fld("Totaal afval  :", fmt_mm(waste)),
        DASH,
    ]

    # ── Staven tabel ──
    # Kolom breedtes (excl. spaties/scheidingstekens):
    #   Nr(3) | Snedes(35) | St(4) | Eff(7)
    # Opbouw: "  " + nr + " | " + snedes + " | " + st + " | " + eff
    # = 2 + 3 + 3 + 35 + 3 + 4 + 3 + 7 = 60 → past binnen W=64
    NC, SC, TC, EC = 3, 35, 4, 7

    def trow(nr, snedes, st, eff):
        return f"  {nr:<{NC}} | {snedes:<{SC}} | {str(st):>{TC}} | {eff:>{EC}}"

    def tsep():
        return "  " + "-" * (NC + 2) + "+" + "-" * (SC + 2) + "+" + "-" * (TC + 2) + "+" + "-" * (EC + 2)

    lines.append(trow("Nr", "Snedes (lengte x aantal)", "St.", "Eff."))
    lines.append(tsep())

    for stock in result.get("cutting_plan", []):
        counts: dict = {}
        for cut in stock.get("cuts", []):
            l = int(cut["length"])
            counts[l] = counts.get(l, 0) + 1
        snedes = "  ".join(f"{l}x{n}" for l, n in sorted(counts.items(), reverse=True))
        if len(snedes) > SC:
            snedes = snedes[:SC - 1] + "~"
        total_cuts = sum(counts.values())
        eff_pct    = stock.get("efficiency", 0)
        lines.append(trow(
            str(stock["stock_number"]),
            snedes,
            str(total_cuts),
            f"{eff_pct:.1f}%",
        ))

    lines.append(SEP)
    return "\n".join(lines)


def _prmv_verdeling_memo(
    order_id: str,
    arti_code: str,
    prmv_lengte: float,
    totaal_aantal: float,
    netto_mm: float,
    share_pct: float,
    waste_share_mm: float,
    total_waste_mm: float,
    qty_to_reserve: float,
) -> str:
    """Genereer een leesbare memo met de verdeelsleutelberekening voor prmv_memo."""
    datum = datetime.utcnow().strftime("%d-%m-%Y")

    W    = 64
    SEP  = "=" * W
    DASH = "-" * W

    def fmt(val: float, dec: int = 0) -> str:
        s = f"{val:,.{dec}f}"
        return s.replace(",", "X").replace(".", ",").replace("X", ".")

    def fld(label: str, value: str) -> str:
        """Label links op vaste breedte (24), waarde daarna links uitgelijnd."""
        return f"  {label:<24} {value}"

    val_line = "-" * 14  # scheidingslijn boven totaalregel

    lines = [
        SEP,
        "  VERDEELSLEUTEL ZAAGVERLIES",
        SEP,
        fld("Order   :", str(order_id)[:42]),
        fld("Artikel :", str(arti_code)[:42]),
        fld("Datum   :", datum),
        "",
        "  BEREKENING",
        DASH,
        fld("Lengte per stuk      :", f"{fmt(prmv_lengte)} mm"),
        fld("Aantal stuks         :", f"{fmt(totaal_aantal)} st"),
        fld("Netto materiaal      :", f"{fmt(netto_mm)} mm  (lengte x aantal)"),
        "",
        fld("Gewogen sleutel      :", fmt(netto_mm * prmv_lengte)),
        "    Langere stukken krijgen relatief meer zaagverlies,",
        "    omdat elke zaagsnede een groter deel van hun lengte beslaat.",
        "",
        fld("Aandeel in afval     :", f"{share_pct:.1f}%"),
        fld("Totaal afval (plan)  :", f"{fmt(total_waste_mm)} mm"),
        fld("Zaagverlies aandeel  :", f"{fmt(waste_share_mm)} mm  ({share_pct:.1f}% van {fmt(total_waste_mm)} mm)"),
        "",
        "  RESERVERING",
        DASH,
        fld("Netto materiaal      :", f"{fmt(netto_mm)} mm"),
        fld("+ Zaagverlies        :", f"{fmt(waste_share_mm)} mm"),
        "  " + " " * 25 + val_line,
        fld("= Totaal reservering :", f"{fmt(qty_to_reserve)} mm"),
        SEP,
    ]
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

        cutting_plan.status = OptimizationStatus.GEOPTIMALISEERD
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

async def process_mkg_webhook(
    request: Request,
    webhook_token: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Core webhook processing — shared by POST and PUT handlers."""
    # Parse body manually so we handle both POST and PUT
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Ongeldige JSON in request body")

    try:
        payload = WebhookPayload(**body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Webhook payload validatiefout: {e}")

    # ── Resolve the tenant environment ───────────────────────────────────────
    env = db.query(TenantEnvironment).filter(
        TenantEnvironment.webhook_token == webhook_token
    ).first()
    if not env:
        raise HTTPException(status_code=404, detail="Unknown webhook token")

    user_id = env.user_id
    use_mkg = env.use_mkg
    logger.info(f"=== WEBHOOK RECEIVED ({request.method}) === user_id={user_id}")
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
                handelslengte = line.get("arti_code.arti_handelslengte")
                mat_lengte = line.get("arti_code.arti_mat_lengte")
                # Fallback volgorde: handelslengte > mat_lengte > default uit instellingen
                try:
                    if handelslengte is not None and float(handelslengte) > 0:
                        stock_per_article[arti_code] = float(handelslengte)
                    elif mat_lengte is not None and float(mat_lengte) > 0:
                        stock_per_article[arti_code] = float(mat_lengte)
                    else:
                        stock_per_article[arti_code] = float(env.default_stock_length if env and getattr(env, 'default_stock_length', None) else 6000.0)
                except Exception:
                    stock_per_article[arti_code] = 6000.0
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
            order_id = f"{iofa_num}-{arti_code}"

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
# Purchase orders — zaagplannen omzetten naar inkooporder
# ---------------------------------------------------------------------------

@router.post("/purchase-orders")
async def create_purchase_order(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Maakt één inkooporder aan op basis van één of meerdere geselecteerde zaagplannen.

    Body (JSON of form): {"cutting_plan_order_ids": ["order_id_1", "order_id_2"]}

    Stappen:
    1. Valideer zaagplannen (bestaan, eigenaar, mkg_rowkey aanwezig).
    2. Haal per zaagplan de prmv materiaalregels op uit MKG.
    3. Aggregeer per arti_code; sommeer iorr_order_aantal over alle zaagplannen.
    4. Maak iorh header aan (cred_num 99999).
    5. Maak per arti_code een iorr regel aan.
    6. Haal pamt op voor de nieuwe iorh → mapping iorr_num → pamt_rowkey.
    7. Maak per prmv-regel een reservering via s_createreservation.
    8. Sla PurchaseOrder + PurchaseOrderLines op in de lokale DB.
    """
    # Accepteer zowel JSON als form-data (HTML form POST)
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        cutting_plan_order_ids = body.get("cutting_plan_order_ids", [])
    else:
        form = await request.form()
        cutting_plan_order_ids = form.getlist("cutting_plan_order_ids")

    if not cutting_plan_order_ids:
        raise HTTPException(status_code=422, detail="Geen zaagplannen geselecteerd")

    # ── Stap 1: valideer geselecteerde zaagplannen ──────────────────────────
    orders = (
        db.query(MaterialOrder)
        .filter(
            MaterialOrder.order_id.in_(cutting_plan_order_ids),
            MaterialOrder.user_id == current_user.id,
        )
        .all()
    )
    found_ids = {o.order_id for o in orders}
    missing = set(cutting_plan_order_ids) - found_ids
    if missing:
        raise HTTPException(status_code=404, detail=f"Zaagplannen niet gevonden: {missing}")

    for o in orders:
        if not o.mkg_document or not o.mkg_rowkey:
            raise HTTPException(
                status_code=422,
                detail=f"Zaagplan '{o.order_id}' heeft geen MKG koppeling (mkg_rowkey ontbreekt)",
            )

    # Haal MKG client op
    env = (
        db.query(TenantEnvironment)
        .filter(TenantEnvironment.user_id == current_user.id)
        .first()
    )
    if not env or not env.use_mkg:
        raise HTTPException(
            status_code=400,
            detail="MKG is niet ingeschakeld. Schakel gebruik MKG in via omgevingsinstellingen.",
        )

    client = get_mkg_client_for_env(env)

    # ── Stap 2: prmv regels ophalen per zaagplan ────────────────────────────
    # aggregation: arti_code -> {"total_qty": float, "prmv_lines": [{...}]}
    aggregated: dict = {}
    # ook per order bewaren voor logging
    all_prmv_by_order: dict = {}

    for order in orders:
        try:
            prmv_lines = await client.get_production_order_materials(
                order.mkg_document, order.mkg_rowkey
            )
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"prmv ophalen mislukt voor '{order.order_id}': {e}",
            )

        all_prmv_by_order[order.order_id] = prmv_lines

        # Aantal staven = total_stock_used uit het zaagplan (niet totaal_aantal = stuks)
        stock_used = (
            order.cutting_plan.total_stock_used
            if order.cutting_plan and order.cutting_plan.total_stock_used
            else 0
        )

        # Alleen de prmv-regels van het artikel van dit zaagplan verwerken.
        # Een iofa kan meerdere materialen bevatten (voor andere zaagplannen in
        # dezelfde iofa). We kijken dus uitsluitend naar regels die overeenkomen
        # met order.article_code zodat niet-geselecteerde zaagplannen worden
        # overgeslagen.
        arti_code = order.article_code or "UNKNOWN"
        matching_lines = [
            l for l in prmv_lines if l.get("arti_code") == arti_code
        ]

        if not matching_lines:
            logger.warning(
                f"Geen prmv-regels gevonden voor arti_code='{arti_code}' "
                f"in order '{order.order_id}' — alle prmv arti_codes: "
                f"{[l.get('arti_code') for l in prmv_lines]}"
            )

        if arti_code not in aggregated:
            aggregated[arti_code] = {"total_qty": 0, "total_waste_mm": 0.0, "orders": [], "prmv_lines": []}
        aggregated[arti_code]["total_qty"] += stock_used
        aggregated[arti_code]["total_waste_mm"] += float(order.cutting_plan.total_waste or 0) if order.cutting_plan else 0.0
        aggregated[arti_code]["orders"].append(order)
        aggregated[arti_code]["prmv_lines"].extend(matching_lines)

    if not aggregated:
        raise HTTPException(status_code=422, detail="Geen materiaalregels gevonden in MKG")

    # ── Stap 3: inkooporder header aanmaken ────────────────────────────────
    cred_num = env.mkg_cred_num
    if not cred_num:
        raise HTTPException(
            status_code=422,
            detail="Crediteur nummer (cred_num) is niet ingesteld. Stel dit in via Instellingen → Crediteur nummer.",
        )
    try:
        iorh_data = await client.create_purchase_order_header(cred_num=cred_num)
    except Exception as e:
        # Probeer de MKG response body uit de HTTPStatusError te halen
        import httpx as _httpx
        mkg_detail = str(e)
        if isinstance(e, _httpx.HTTPStatusError):
            try:
                mkg_detail = e.response.json()
            except Exception:
                mkg_detail = e.response.text[:500]
        raise HTTPException(status_code=502, detail=f"iorh aanmaken mislukt: {mkg_detail}")

    iorh_num = iorh_data.get("iorh_num")
    admi_num = iorh_data.get("admi_num")
    iorh_rowkey = iorh_data.get("RowKey")

    if not iorh_num:
        raise HTTPException(status_code=502, detail="MKG gaf geen iorh_num terug")

    logger.info(f"iorh aangemaakt: iorh_num={iorh_num}, admi_num={admi_num}")

    # ── Stap 4: per arti_code een iorr regel aanmaken ──────────────────────
    # arti_code -> {"iorr_num": ..., "iorr_rowkey": ...}
    iorr_map: dict = {}
    po_lines_data: list = []

    for arti_code, info in aggregated.items():
        try:
            iorr_data = await client.create_purchase_order_line(
                iorh_num=iorh_num,
                arti_code=arti_code,
                quantity=info["total_qty"],
            )
        except Exception as e:
            logger.error(f"iorr aanmaken mislukt voor {arti_code}: {e}")
            iorr_data = {}

        iorr_num = iorr_data.get("iorr_num")
        iorr_rowkey = iorr_data.get("RowKey")
        iorr_map[arti_code] = {"iorr_num": iorr_num, "iorr_rowkey": iorr_rowkey}

        # Schrijf het zaagplan terug naar iorr_memo_intern
        if iorr_num is not None:
            memo_lines = []
            for _order in aggregated[arti_code]["orders"]:
                if _order.cutting_plan and _order.cutting_plan.optimization_data:
                    memo_lines.append(
                        _ascii_cutting_plan(_order, _order.cutting_plan.optimization_data)
                    )
            if memo_lines:
                iorr_memo = ("\n\n" + "=" * 64 + "\n\n").join(memo_lines)
                try:
                    await client.update_iorr_memo(
                        admi_num=admi_num,
                        iorh_num=iorh_num,
                        iorr_num=iorr_num,
                        memo_intern=iorr_memo,
                    )
                    logger.info(f"iorr_memo_intern geschreven voor iorr {admi_num}+{iorh_num}+{iorr_num}")
                except Exception as e:
                    logger.warning(f"iorr_memo_intern schrijven mislukt: {e}")

        po_lines_data.append(
            {
                "arti_code": arti_code,
                "quantity": info["total_qty"],
                "iorr_num": iorr_num,
                "iorr_rowkey": iorr_rowkey,
            }
        )
        logger.info(f"iorr aangemaakt: iorh_num={iorh_num}, arti_code={arti_code}, iorr_num={iorr_num}")

    # ── Stap 5: pamt ophalen → iorr_num -> pamt_rowkey ─────────────────────
    # MKG verwerkt iorr-regels asynchroon; wacht tot pamt gevuld is (max ~10s)
    expected_count = len(iorr_map)  # verwacht 1 pamt-rij per iorr-regel
    pamt_lines: list = []
    for attempt in range(1, 7):  # max 6 pogingen
        await asyncio.sleep(2)   # wacht 2 seconden per poging
        try:
            pamt_lines = await client.get_pamt_for_order(iorh_num)
        except Exception as e:
            logger.warning(f"pamt ophalen mislukt (poging {attempt}): {e}")
            continue
        if len(pamt_lines) >= expected_count:
            logger.info(f"pamt gereed na poging {attempt}: {len(pamt_lines)} regels")
            break
        logger.info(
            f"pamt nog niet klaar (poging {attempt}): "
            f"{len(pamt_lines)}/{expected_count} regels — opnieuw proberen..."
        )
    else:
        logger.warning(
            f"pamt onvolledig na 6 pogingen: {len(pamt_lines)}/{expected_count} regels. "
            "Doorgaan met wat er is."
        )

    # Bouw mapping: iorr_num (int) -> RowKey van pamt
    pamt_map: dict = {}
    for p in pamt_lines:
        rn = p.get("iorr_num")
        rk = p.get("RowKey")
        if rn is not None and rk and int(rn) not in pamt_map:
            pamt_map[int(rn)] = rk

    logger.info(f"pamt map: {pamt_map}")

    # ── Stap 6: reserveringen aanmaken per prmv-regel ──────────────────────
    reservation_results: dict = {}  # arti_code -> [result, ...]

    for arti_code, info in aggregated.items():
        iorr_info = iorr_map.get(arti_code, {})
        iorr_num = iorr_info.get("iorr_num")
        pamt_rowkey = pamt_map.get(int(iorr_num)) if iorr_num is not None else None

        reservation_results[arti_code] = []

        if not pamt_rowkey:
            logger.warning(
                f"Geen pamt_rowkey gevonden voor arti_code={arti_code}, "
                f"iorr_num={iorr_num} — reservering overgeslagen"
            )
            reservation_results[arti_code].append({"skipped": True, "reason": "geen pamt_rowkey"})
            continue

        # ── Verdeelsleutel: bereken gewogen aandeel per prmv-regel ──────────
        # weight_i = netto_mm_i × prmv_lengte_i
        # Langere stukken krijgen relatief meer zaagverlies omdat elke zaagsnede
        # een groter deel van hun lengte beslaat.
        total_waste_mm = info["total_waste_mm"]
        line_data_weighted = []
        for line in info["prmv_lines"]:
            lengte = float(line.get("prmv_lengte") or 0)
            aantal = float(line.get("totaal_aantal") or 0)
            netto_mm = lengte * aantal
            weight = netto_mm * lengte  # dual component: volume × lengte
            line_data_weighted.append((line, lengte, aantal, netto_mm, weight))

        total_weight   = sum(w   for _, _, _, _, w   in line_data_weighted)
        total_netto_mm = sum(n   for _, _, _, n, _   in line_data_weighted)

        for line, prmv_lengte, totaal_aantal, netto_mm, weight in line_data_weighted:
            prdh_num = line.get("prdh_num")
            prdr_num = line.get("prdr_num")
            prmv_num = line.get("prmv_num")

            if not all([prdh_num, prdr_num, prmv_num]):
                logger.warning(f"prmv regel mist prdh_num/prdr_num/prmv_num: {line}")
                reservation_results[arti_code].append(
                    {"skipped": True, "reason": "ontbrekende prmv sleutels", "line": line}
                )
                continue

            # Verdeel zaagverlies proportioneel
            share = (weight / total_weight) if total_weight > 0 else 0
            waste_share_mm = share * total_waste_mm
            qty_to_reserve = netto_mm + waste_share_mm
            share_pct = share * 100

            # Afrondingscorrectie op de laatste regel:
            # zorg dat sum(reserveringen) == totaal_netto + totaal_afval
            is_last = (line == line_data_weighted[-1][0])
            if is_last:
                already_reserved = sum(
                    r["qty_mm"]
                    for r in reservation_results[arti_code]
                    if isinstance(r.get("qty_mm"), (int, float))
                )
                total_target = total_netto_mm + total_waste_mm
                qty_to_reserve = total_target - already_reserved
            order_id_for_memo = (
                info["orders"][0].order_id if info.get("orders") else "?"
            )
            memo_text = _prmv_verdeling_memo(
                order_id=order_id_for_memo,
                arti_code=arti_code,
                prmv_lengte=prmv_lengte,
                totaal_aantal=totaal_aantal,
                netto_mm=netto_mm,
                share_pct=share_pct,
                waste_share_mm=waste_share_mm,
                total_waste_mm=total_waste_mm,
                qty_to_reserve=qty_to_reserve,
            )
            await client.update_prmv_memo(
                admi_num=admi_num,
                prdh_num=prdh_num,
                prdr_num=prdr_num,
                prmv_num=prmv_num,
                memo=memo_text,
            )

            res = await client.create_reservation(
                admi_num=admi_num,
                prdh_num=prdh_num,
                prdr_num=prdr_num,
                prmv_num=prmv_num,
                pamt_rowkey=pamt_rowkey,
                quantity=qty_to_reserve,
                unit="mm",
            )
            reservation_results[arti_code].append(
                {"prmv_num": prmv_num, "netto_mm": netto_mm,
                 "waste_share_mm": round(waste_share_mm, 1),
                 "qty_mm": round(qty_to_reserve, 1), "result": res}
            )
            logger.info(
                f"Reservering: prmv={admi_num}+{prdh_num}+{prdr_num}+{prmv_num}, "
                f"netto={netto_mm:.0f}mm + afval={waste_share_mm:.0f}mm "
                f"({share_pct:.1f}%) = {qty_to_reserve:.0f}mm"
            )

    # ── Stap 7: opslaan in lokale DB ───────────────────────────────────────
    purchase_order = PurchaseOrder(
        user_id=current_user.id,
        iorh_num=iorh_num,
        admi_num=admi_num,
        iorh_rowkey=iorh_rowkey,
        cred_num=cred_num,
        cutting_plan_order_ids=list(cutting_plan_order_ids),
    )
    db.add(purchase_order)
    db.flush()

    for line_data in po_lines_data:
        db.add(
            PurchaseOrderLine(
                purchase_order_id=purchase_order.id,
                iorr_num=line_data["iorr_num"],
                iorr_rowkey=line_data["iorr_rowkey"],
                arti_code=line_data["arti_code"],
                quantity=line_data["quantity"],
                reservation_results=reservation_results.get(line_data["arti_code"]),
            )
        )

    db.commit()
    db.refresh(purchase_order)

    # Update status van alle geselecteerde zaagplannen → inkooporder_aangemaakt
    for order in orders:
        if order.cutting_plan:
            order.cutting_plan.status = OptimizationStatus.INKOOPORDER_AANGEMAAKT
            order.cutting_plan.purchase_order_id = purchase_order.id
    db.commit()

    logger.info(
        f"PurchaseOrder {purchase_order.id} opgeslagen: iorh_num={iorh_num}, "
        f"{len(po_lines_data)} regels"
    )

    return {"purchase_order_id": purchase_order.id, "iorh_num": iorh_num}


@router.get("/purchase-orders/{purchase_order_id}")
def get_purchase_order(
    purchase_order_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Haal een inkooporder op met alle regels."""
    po = (
        db.query(PurchaseOrder)
        .filter(
            PurchaseOrder.id == purchase_order_id,
            PurchaseOrder.user_id == current_user.id,
        )
        .first()
    )
    if not po:
        raise HTTPException(status_code=404, detail="Inkooporder niet gevonden")
    return {
        "id": po.id,
        "iorh_num": po.iorh_num,
        "admi_num": po.admi_num,
        "cred_num": po.cred_num,
        "cutting_plan_order_ids": po.cutting_plan_order_ids,
        "created_at": po.created_at.isoformat() if po.created_at else None,
        "lines": [
            {
                "arti_code": l.arti_code,
                "quantity": l.quantity,
                "iorr_num": l.iorr_num,
                "reservation_results": l.reservation_results,
            }
            for l in po.lines
        ],
    }


# ---------------------------------------------------------------------------
# Purchase orders — verwijderen
# ---------------------------------------------------------------------------

@router.delete("/purchase-orders/{po_id}")
async def delete_purchase_order(
    po_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Verwijdert een inkooporder uit MKG en synchroniseert dit naar de lokale database.

    Logica per iorr / iorh:
    - Probeer DELETE in MKG.
    - Bij 404-respons → doe een GET om te bevestigen dat de regel echt verdwenen is.
      -> Niet aanwezig: al verwijderd in MKG, sync naar lokale DB.
      -> Nog aanwezig:  onverwachte situatie, geef 502 terug.

    Na succesvol verwijderen:
    - Reset alle gekoppelde CuttingPlan statussen naar GEOPTIMALISEERD.
    - Verwijder purchase_order_id koppeling van die zaagplannen.
    - Verwijder PurchaseOrderLine + PurchaseOrder records uit de lokale DB.
    """
    po = db.query(PurchaseOrder).filter(
        PurchaseOrder.id == po_id,
        PurchaseOrder.user_id == current_user.id,
    ).first()
    if not po:
        raise HTTPException(status_code=404, detail="Inkooporder niet gevonden")

    if po.admi_num is None or not po.iorh_num:
        raise HTTPException(
            status_code=422,
            detail="Inkooporder heeft geen geldige MKG koppeling (admi_num of iorh_num ontbreekt)",
        )

    env = db.query(TenantEnvironment).filter(TenantEnvironment.user_id == current_user.id).first()
    if not env or not env.use_mkg:
        raise HTTPException(status_code=422, detail="MKG niet geconfigureerd voor deze gebruiker")

    mkg = get_mkg_client_for_env(env)
    try:
        # ── Stap 1: verwijder alle iorr regels ────────────────────────────
        # delete_purchase_order_line retourneert True (verwijderd) of None (al weg).
        # Bij elke andere MKG-fout terwijl de regel nog bestaat, gooit het een exception.
        for line in po.lines:
            if line.iorr_num is None:
                continue
            result = await mkg.delete_purchase_order_line(po.admi_num, po.iorh_num, line.iorr_num)
            if result is None:
                logger.info(f"iorr {line.iorr_num} was al verwijderd in MKG — gesynchroniseerd")

        # ── Stap 2: verwijder iorh header ─────────────────────────────────
        result = await mkg.delete_purchase_order_header(po.admi_num, po.iorh_num)
        if result is None:
            logger.info(f"iorh {po.iorh_num} was al verwijderd in MKG — gesynchroniseerd")
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"MKG fout bij verwijderen: HTTP {e.response.status_code} — {e.response.text[:200]}",
        )
    finally:
        await mkg.close()

    # ── Stap 3: sync lokale database ──────────────────────────────────────
    linked_plans = db.query(CuttingPlan).filter(
        CuttingPlan.purchase_order_id == po_id
    ).all()
    for plan in linked_plans:
        plan.purchase_order_id = None
        plan.status = OptimizationStatus.GEOPTIMALISEERD

    for line in list(po.lines):
        db.delete(line)
    db.delete(po)
    db.commit()

    return {
        "status": "deleted",
        "iorh_num": po.iorh_num,
        "reset_cutting_plans": len(linked_plans),
    }


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
