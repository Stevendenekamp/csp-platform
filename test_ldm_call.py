"""
Lokale test voor de LDM verzameling API-call.
Leest de credentials uit de database (eerste gebruiker met inloggegevens).

Gebruik:
    python test_ldm_call.py
of met vaste credentials:
    python test_ldm_call.py --url https://mkg-server.nl --user admin --pass geheim --key APIKEY
"""
import asyncio
import argparse
import sys
import json
import httpx

# ── Credentials via argumenten óf uit database ────────────────────────────────

def get_creds_from_db():
    """Haal de eerste gebruiker met MKG-instellingen op uit de database."""
    try:
        from database.database import get_db
        from database.models import TenantEnvironment
        from auth.security import decrypt_secret

        db = next(get_db())
        env = db.query(TenantEnvironment).filter(
            TenantEnvironment.mkg_base_url != None
        ).first()
        if not env:
            print("❌  Geen gebruiker met MKG-instellingen gevonden in de database.")
            sys.exit(1)

        password = ""
        if env.mkg_password_enc:
            try:
                password = decrypt_secret(env.mkg_password_enc)
            except Exception as e:
                print(f"⚠  Wachtwoord kon niet worden ontsleuteld: {e}")

        return {
            "base_url":     env.mkg_base_url.rstrip("/"),
            "context_path": (env.mkg_context_path or "/mkg").strip(),
            "api_key":      env.mkg_api_key or "",
            "username":     env.mkg_username or "",
            "password":     password,
        }
    except Exception as e:
        print(f"❌  Fout bij ophalen database-credentials: {e}")
        sys.exit(1)


def sep(title=""):
    print("\n" + "─" * 60)
    if title:
        print(f"  {title}")
        print("─" * 60)


async def run(creds: dict):
    base_url     = creds["base_url"]
    context_path = creds["context_path"]
    api_key      = creds["api_key"]
    username     = creds["username"]
    password     = creds["password"]

    login_url = f"{base_url}{context_path}/static/auth/j_spring_security_check"
    ldm_base  = f"{base_url}{context_path}/web/v3/MKG/documents/udof"
    user_url  = f"{base_url}{context_path}/web/v3/MKG/User"

    extra_headers = {"Accept": "application/json"}
    if api_key:
        extra_headers["X-CustomerID"] = api_key

    sep("CONFIGURATIE")
    print(f"  Base URL     : {base_url}")
    print(f"  Context pad  : {context_path}")
    print(f"  Gebruikersnaam: {username}")
    print(f"  Wachtwoord   : {'*' * len(password) if password else '(leeg)'}")
    print(f"  API Key      : {api_key or '(niet ingesteld)'}")

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:

        # ── STAP 1: Login ──────────────────────────────────────────────────
        sep("STAP 1 — Login")
        print(f"  POST {login_url}")
        print(f"  Body: j_username={username}, j_password={'*' * len(password)}")

        login_resp = await client.post(
            login_url,
            data={"j_username": username, "j_password": password},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                **({} if not api_key else {"X-CustomerID": api_key}),
            },
        )
        print(f"\n  Status       : {login_resp.status_code}")
        print(f"  Cookies      : {dict(login_resp.cookies)}")

        jsessionid = login_resp.cookies.get("JSESSIONID")
        if not jsessionid:
            print("\n  ❌ Geen JSESSIONID ontvangen — login mislukt.")
            print(f"  Response body: {login_resp.text[:500]}")
            return
        print(f"\n  ✓ JSESSIONID ontvangen: {jsessionid[:30]}…")

        # ── STAP 2: User check ─────────────────────────────────────────────
        sep("STAP 2 — User check")
        user_full = f"{user_url}?FieldList=gebr_code,gebr_naam"
        print(f"  GET {user_full}")
        user_resp = await client.get(
            user_full,
            cookies={"JSESSIONID": jsessionid},
            headers=extra_headers,
        )
        print(f"  Status: {user_resp.status_code}")
        if user_resp.status_code == 200:
            print(f"  Body  : {user_resp.text[:300]}")
        else:
            print(f"  Body  : {user_resp.text[:300]}")

        # ── STAP 3: LDM call — varianten testen ───────────────────────────
        sep("STAP 3 — LDM verzameling varianten")

        variants = [
            # Huidige implementatie
            (
                "Huidige implementatie (filter met %3D)",
                f"{ldm_base}?filter=udof_naam%3Dprmv%20and%20sdoc_num%3D242&fieldlist=udof_bron,udof_bron_query,udof_type",
            ),
            # Spaties als + i.p.v. %20
            (
                "Spaties als + encoding",
                f"{ldm_base}?filter=udof_naam%3Dprmv+and+sdoc_num%3D242&fieldlist=udof_bron,udof_bron_query,udof_type",
            ),
            # Letterlijke = (niet encoded)
            (
                "Letterlijke = (niet encoded)",
                f"{ldm_base}?filter=udof_naam=prmv and sdoc_num=242&fieldlist=udof_bron,udof_bron_query,udof_type",
            ),
            # Zonder sdoc_num filter
            (
                "Alleen udof_naam filter",
                f"{ldm_base}?filter=udof_naam%3Dprmv&fieldlist=udof_bron,udof_bron_query,udof_type",
            ),
            # Zonder filter — alle records ophalen
            (
                "Geen filter — alle udof records",
                f"{ldm_base}?fieldlist=udof_naam,udof_bron,udof_type",
            ),
        ]

        for label, url in variants:
            print(f"\n  [{label}]")
            print(f"  URL: {url}")
            try:
                resp = await client.get(
                    url,
                    cookies={"JSESSIONID": jsessionid},
                    headers=extra_headers,
                )
                print(f"  Status : {resp.status_code}")
                try:
                    body = resp.json()
                    # Response structuur: {"response":{"ResultData":[{"udof":[...]}]}}
                    try:
                        records = body["response"]["ResultData"][0].get("udof", [])
                    except (KeyError, IndexError, TypeError):
                        records = body.get("udof", [])
                    print(f"  Records: {len(records)}")
                    if records:
                        for r in records[:3]:
                            print(f"    → {json.dumps(r, ensure_ascii=False)[:200]}")
                    else:
                        print(f"  Raw body: {resp.text[:400]}")
                except Exception:
                    print(f"  Raw body: {resp.text[:400]}")
            except Exception as e:
                print(f"  ❌ Fout: {e}")

        sep("KLAAR")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test MKG LDM API-call")
    parser.add_argument("--url",  help="MKG basis-URL (bijv. https://mkg-server.nl)")
    parser.add_argument("--ctx",  default="/mkg", help="Context pad (default: /mkg)")
    parser.add_argument("--user", help="MKG gebruikersnaam")
    parser.add_argument("--pass", dest="pw", help="MKG wachtwoord")
    parser.add_argument("--key",  default="", help="X-CustomerID API key")
    args = parser.parse_args()

    if args.url and args.user and args.pw:
        creds = {
            "base_url":     args.url.rstrip("/"),
            "context_path": args.ctx,
            "api_key":      args.key,
            "username":     args.user,
            "password":     args.pw,
        }
    else:
        print("Geen credentials opgegeven — credentials worden uit de database geladen.\n")
        creds = get_creds_from_db()

    asyncio.run(run(creds))
