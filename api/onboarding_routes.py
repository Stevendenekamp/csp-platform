"""
Onboarding API endpoints – used by the new-customer setup wizard.
"""
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import get_current_user_optional
from auth.security import encrypt_secret
from database.database import get_db
from database.models import TenantEnvironment, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


# ── Request models ─────────────────────────────────────────────────────────────

class ConnectionTestRequest(BaseModel):
    mkg_base_url: str
    mkg_context_path: str = "/mkg"
    mkg_api_key: str = ""
    mkg_username: str = ""
    mkg_password: str = ""
    save_on_success: bool = True


# ── Expected reference values ─────────────────────────────────────────────────

_LDM_EXPECTED = {
    "udof_bron": 308,
    "udof_bron_query": (
        "FOR EACH ioln WHERE\r\n"
        "ioln.admi_num = iofa.admi_num and\r\n"
        "ioln.iofa_num = iofa.iofa_num and\r\n"
        "ioln.ioln_type = 7,\r\n"
        "\r\n"
        "EACH prmv WHERE\r\n"
        "prmv.admi_num = ioln.admi_num and\r\n"
        "prmv.prdh_num = ioln.prdh_num and\r\n"
        "prmv.prdr_num = ioln.prdr_num and\r\n"
        "prmv.prmv_num = ioln.prmv_num"
    ),
    "udof_type": "collection",
}


# ── Helper ─────────────────────────────────────────────────────────────────────

def _get_or_create_env(user: User, db: Session) -> TenantEnvironment:
    env = db.query(TenantEnvironment).filter(
        TenantEnvironment.user_id == user.id
    ).first()
    if not env:
        env = TenantEnvironment(user_id=user.id)
        db.add(env)
        db.commit()
        db.refresh(env)
    return env


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/test-connection")
async def test_mkg_connection(
    payload: ConnectionTestRequest,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    """Test the MKG connection by logging in and calling the User endpoint.
    If save_on_success=True and the test passes, credentials are saved to the DB.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Niet ingelogd")

    base_url = payload.mkg_base_url.rstrip("/")
    context_path = (payload.mkg_context_path or "/mkg").strip()
    if not context_path.startswith("/"):
        context_path = "/" + context_path

    login_url = f"{base_url}{context_path}/static/auth/j_spring_security_check"
    test_url  = f"{base_url}{context_path}/web/v3/MKG/User"

    extra_headers: dict = {}
    if payload.mkg_api_key:
        extra_headers["X-CustomerID"] = payload.mkg_api_key

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            # ── Step 1: Login ──────────────────────────────────────────────
            login_resp = await client.post(
                login_url,
                data={
                    "j_username": payload.mkg_username,
                    "j_password": payload.mkg_password,
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    **extra_headers,
                },
            )
            logger.info(f"MKG onboarding login → {login_resp.status_code}")

            jsessionid = login_resp.cookies.get("JSESSIONID")
            if not jsessionid:
                return JSONResponse({
                    "success": False,
                    "message": (
                        f"Login mislukt (HTTP {login_resp.status_code}). "
                        "Controleer gebruikersnaam, wachtwoord en de basis-URL."
                    ),
                })

            # ── Step 2: Call User endpoint ─────────────────────────────────
            test_resp = await client.get(
                test_url,
                params={"FieldList": "gebr_code,gebr_naam"},
                cookies={"JSESSIONID": jsessionid},
                headers={"Accept": "application/json", **extra_headers},
            )
            logger.info(f"MKG onboarding user-check → {test_resp.status_code}")

            if test_resp.status_code == 200:
                # Optionally save credentials
                if payload.save_on_success:
                    env = _get_or_create_env(current_user, db)
                    env.mkg_base_url      = base_url
                    env.mkg_context_path  = context_path
                    env.mkg_api_key       = payload.mkg_api_key or None
                    env.mkg_username      = payload.mkg_username or None
                    if payload.mkg_password:
                        env.mkg_password_enc = encrypt_secret(payload.mkg_password)
                    env.use_mkg = True
                    db.commit()

                return JSONResponse({
                    "success": True,
                    "message": "Verbinding succesvol! Instellingen zijn opgeslagen.",
                })

            return JSONResponse({
                "success": False,
                "message": (
                    f"Verbinding kon niet worden geverifieerd (HTTP {test_resp.status_code}). "
                    "Controleer de URL en API-sleutel."
                ),
            })

    except httpx.ConnectError:
        return JSONResponse({
            "success": False,
            "message": "Kan geen verbinding maken met de MKG server. Controleer of de basis-URL correct is.",
        })
    except httpx.TimeoutException:
        return JSONResponse({
            "success": False,
            "message": "Verbinding verlopen (timeout 15 s). De server reageert niet op tijd.",
        })
    except Exception as exc:
        logger.error(f"MKG onboarding test-connection error: {exc}")
        return JSONResponse({
            "success": False,
            "message": f"Onverwachte fout: {exc}",
        })


@router.post("/verify-ldm")
async def verify_ldm(
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    """Verify that the LDM collection (prmv / sdoc_num=242) is correctly configured in MKG.
    Compares udof_bron, udof_bron_query, and udof_type against expected reference values.
    Returns a 'no rights' message on HTTP 403 instead of an error.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Niet ingelogd")

    env = _get_or_create_env(current_user, db)
    if not env.mkg_base_url or not env.mkg_username:
        return JSONResponse({
            "success": False,
            "message": "MKG koppeling nog niet ingesteld. Voltooi eerst stap 1.",
        })

    from auth.security import decrypt_secret
    password = ""
    if env.mkg_password_enc:
        try:
            password = decrypt_secret(env.mkg_password_enc)
        except Exception:
            pass

    base_url     = env.mkg_base_url.rstrip("/")
    context_path = (env.mkg_context_path or "/mkg").strip()
    extra_headers: dict = {"Accept": "application/json"}
    if env.mkg_api_key:
        extra_headers["X-CustomerID"] = env.mkg_api_key

    login_url = f"{base_url}{context_path}/static/auth/j_spring_security_check"
    ldm_url   = f"{base_url}{context_path}/web/v3/MKG/documents/udof"

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            # Login
            login_resp = await client.post(
                login_url,
                data={"j_username": env.mkg_username, "j_password": password},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    **({"X-CustomerID": env.mkg_api_key} if env.mkg_api_key else {}),
                },
            )
            jsessionid = login_resp.cookies.get("JSESSIONID")
            if not jsessionid:
                return JSONResponse({
                    "success": False,
                    "message": f"Login mislukt (HTTP {login_resp.status_code}). Controleer de MKG inloggegevens.",
                })

            # Fetch LDM record
            # Build URL manually to control filter encoding exactly as MKG expects.
            # %3D for '=' and %20 for spaces work correctly; '+' for spaces does NOT.
            ldm_full_url = (
                f"{ldm_url}"
                f"?filter=udof_naam%3Dprmv%20and%20sdoc_num%3D242"
                f"&fieldlist=udof_bron,udof_bron_query,udof_type"
            )
            ldm_resp = await client.get(
                ldm_full_url,
                cookies={"JSESSIONID": jsessionid},
                headers=extra_headers,
            )
            logger.info(f"MKG verify-ldm → {ldm_resp.status_code}")

            if ldm_resp.status_code == 403:
                return JSONResponse({
                    "success": None,
                    "message": (
                        "U heeft geen leesrechten voor de LDM-instellingen in MKG. "
                        "Vraag uw MKG-beheerder om toegang tot documenten/udof, "
                        "of sla deze controle over en ga handmatig te werk."
                    ),
                })

            if ldm_resp.status_code != 200:
                return JSONResponse({
                    "success": False,
                    "message": f"MKG gaf een onverwachte status terug: HTTP {ldm_resp.status_code}.",
                })

            data = ldm_resp.json()
            # Response structuur: {"response":{"ResultData":[{"udof":[...]}]}}
            try:
                records = data["response"]["ResultData"][0].get("udof", [])
            except (KeyError, IndexError, TypeError):
                records = data.get("udof", [])
            if not records:
                return JSONResponse({
                    "success": False,
                    "message": (
                        "Geen LDM-verzameling gevonden voor prmv / sdoc_num=242. "
                        "Maak de verzameling aan zoals beschreven in stap 2."
                    ),
                })

            # Compare fields of the first (expected) record
            rec = records[0]
            deviations = []
            for field, expected in _LDM_EXPECTED.items():
                actual = rec.get(field)
                if actual != expected:
                    deviations.append(
                        f"• {field}: verwacht {expected!r}, gevonden {actual!r}"
                    )

            if deviations:
                return JSONResponse({
                    "success": False,
                    "message": (
                        "LDM-verzameling gevonden maar er zijn afwijkingen:\n"
                        + "\n".join(deviations)
                    ),
                    "deviations": deviations,
                })

            return JSONResponse({
                "success": True,
                "message": "LDM-verzameling correct geconfigureerd. Alle velden komen overeen.",
            })

    except httpx.ConnectError:
        return JSONResponse({
            "success": False,
            "message": "Kan geen verbinding maken met de MKG server.",
        })
    except httpx.TimeoutException:
        return JSONResponse({
            "success": False,
            "message": "Verbinding verlopen (timeout 15 s).",
        })
    except Exception as exc:
        logger.error(f"MKG verify-ldm error: {exc}")
        return JSONResponse({
            "success": False,
            "message": f"Onverwachte fout: {exc}",
        })


@router.post("/verify-webhook")
async def verify_webhook(
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    """Verify that the webhook is correctly configured in MKG.
    API call details will be added once provided.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Niet ingelogd")

    # TODO: implement once API call details are provided
    return JSONResponse({
        "success": None,
        "message": "Webhook-verificatie wordt geconfigureerd zodra de API-details beschikbaar zijn.",
    })
