import httpx
import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import logging
from config import get_settings

logger = logging.getLogger(__name__)

# Collect trace records so /api/mkg/probe can return them
_trace_log: List[Dict] = []
MAX_TRACE = 50  # bewaar de laatste 50 calls

def _add_trace(entry: Dict):
    _trace_log.append(entry)
    if len(_trace_log) > MAX_TRACE:
        _trace_log.pop(0)

def get_trace_log() -> List[Dict]:
    return list(_trace_log)

def clear_trace_log():
    _trace_log.clear()


def _safe_body(content: bytes) -> Any:
    """Probeer bytes als JSON te parsen, anders als plain text."""
    if not content:
        return None
    try:
        return json.loads(content)
    except Exception:
        text = content.decode("utf-8", errors="replace")
        return text[:2000]  # cap lange HTML error pages


class MKGClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        context_path: Optional[str] = None,
        api_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        settings = get_settings()
        # Use provided values; fall back to global settings for legacy support
        self.base_url = base_url or settings.mkg_base_url or ""
        self.context_path = context_path or settings.mkg_context_path or "/mkg"
        self.api_key = api_key or settings.mkg_api_key or ""
        self.username = username or settings.mkg_username or ""
        self.password = password or settings.mkg_password or ""

        self.jsessionid: Optional[str] = None
        self.session_expires_at: Optional[datetime] = None
        self.client = httpx.AsyncClient(timeout=30.0)
    
    async def _login(self) -> bool:
        """Login to MKG and obtain JSESSIONID cookie"""
        # Correct pad per MKG API documentatie:
        # POST {basisUrl}{contextPath}/static/auth/j_spring_security_check
        url = f"{self.base_url}{self.context_path}/static/auth/j_spring_security_check"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        login_data = {
            "j_username": self.username,
            "j_password": "***",  # masked in trace
        }
        trace_req = {
            "timestamp": datetime.utcnow().isoformat(),
            "type": "LOGIN",
            "method": "POST",
            "url": url,
            "request_headers": dict(headers),
            "request_body": login_data,
        }
        logger.info(f"MKG LOGIN → POST {url}")

        try:
            response = await self.client.post(
                url,
                headers=headers,
                data={"j_username": self.username, "j_password": self.password},
            )

            trace_req.update({
                "response_status": response.status_code,
                "response_headers": dict(response.headers),
                "response_body": _safe_body(response.content),
                "cookies": dict(response.cookies),
            })
            _add_trace(trace_req)
            logger.info(f"MKG LOGIN ← {response.status_code}")
            logger.debug(f"Response headers: {dict(response.headers)}")

            if response.status_code == 200:
                self.jsessionid = response.cookies.get("JSESSIONID")
                if self.jsessionid:
                    self.session_expires_at = datetime.utcnow() + timedelta(minutes=25)
                    logger.info("MKG login geslaagd, JSESSIONID ontvangen")
                    return True
                logger.error("Login 200 maar geen JSESSIONID in cookies")
                return False

            logger.error(f"MKG login mislukt: {response.status_code}")
            return False

        except Exception as e:
            trace_req["error"] = str(e)
            _add_trace(trace_req)
            logger.error(f"MKG login exception: {e}")
            return False
    
    def _is_session_valid(self) -> bool:
        """Check if current session is still valid"""
        if not self.jsessionid or not self.session_expires_at:
            return False
        return datetime.utcnow() < self.session_expires_at
    
    async def _ensure_authenticated(self) -> bool:
        """Ensure we have a valid session, login if needed"""
        if self._is_session_valid():
            return True
        return await self._login()
    
    async def _make_request(
        self, 
        method: str, 
        endpoint: str, 
        **kwargs
    ) -> Optional[Dict[Any, Any]]:
        """Make authenticated request to MKG API with automatic re-authentication"""
        if not await self._ensure_authenticated():
            raise Exception("Failed to authenticate with MKG")
        
        kwargs.pop("headers", {})  # negeer eventuele meegegeven headers
        url = f"{self.base_url}{self.context_path}{endpoint}"

        def _build_headers():
            h = {
                "X-CustomerID": self.api_key,
                "Accept": "application/json",
                "Cookie": f"JSESSIONID={self.jsessionid}",
            }
            if kwargs.get("json") is not None:
                h["Content-Type"] = "application/json"
            return h

        headers = _build_headers()

        trace_req = {
            "timestamp": datetime.utcnow().isoformat(),
            "type": "API",
            "method": method.upper(),
            "url": url,
            "request_headers": {k: v for k, v in headers.items()
                                 if k.lower() not in ("x-api-key", "cookie")},
            "request_cookie_sent": f"JSESSIONID={self.jsessionid[:8]}...",
            "request_params": kwargs.get("params"),
            "request_body": kwargs.get("json"),
        }
        logger.info(f"MKG API → {method.upper()} {url}")

        try:
            response = await self.client.request(
                method=method,
                url=url,
                headers=headers,
                **kwargs
            )

            # 401 = sessie verlopen → opnieuw inloggen en retry
            # 403 op GET = rechten; 403 op PUT/POST kan session-issue zijn → ook retry
            if response.status_code in [401, 403]:
                logger.warning(f"MKG {response.status_code} — opnieuw inloggen en retry...")
                trace_req["auth_retry"] = True
                self.jsessionid = None
                if await self._login():
                    # Bouw headers opnieuw op met nieuw JSESSIONID
                    headers = _build_headers()
                    response = await self.client.request(
                        method=method, url=url,
                        headers=headers, **kwargs
                    )
            
            response_body = _safe_body(response.content)
            trace_req.update({
                "response_status": response.status_code,
                "response_headers": {k: v for k, v in response.headers.items()
                                     if k.lower() in ("content-type", "content-length", "x-total-count")},
                "response_body": response_body,
            })
            _add_trace(trace_req)
            logger.info(f"MKG API ← {response.status_code} ({len(response.content)} bytes)")

            response.raise_for_status()
            return response.json() if response.content else None
            
        except httpx.HTTPStatusError as e:
            trace_req["error"] = f"HTTP {e.response.status_code}: {e.response.text[:500]}"
            _add_trace(trace_req)
            logger.error(f"MKG HTTP error: {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            trace_req["error"] = str(e)
            _add_trace(trace_req)
            logger.error(f"MKG request error: {e}")
            raise
    
    async def get_production_order_header(self, document: int, rowkey: str) -> Dict:
        """
        Haalt de basisinformatie van een iofa document op.

        Endpoint: GET /web/v3/MKG/Documents/{document}/{rowkey}
        Geeft velden als iofa_num, aanmaakdatum, status etc.
        """
        endpoint = f"/web/v3/MKG/Documents/{document}/{rowkey}"
        params = {
            "fieldlist": "iofa_num,iofa_datum,iofa_status,iofa_oms,RelKey"
        }
        response = await self._make_request("GET", endpoint, params=params)

        # Response structuur: {"response":{"ResultData":[{"iofa":[{...}]}]}}
        try:
            data = response["response"]["ResultData"][0]
            # Probeer geneste tabel-sleutel (bijv. "iofa") en anders plat dict
            if isinstance(data, dict):
                for key, val in data.items():
                    if isinstance(val, list) and val:
                        return val[0]
                return data
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Onverwachte response structuur van MKG header endpoint: {e}")
            logger.error(f"Response was: {response}")
        return {}

    async def get_production_order_materials(self, document: int, rowkey: str) -> List[Dict]:
        """
        Haalt materiaalregels (prmv) op voor een productieorder.

        Endpoint: GET /web/v3/MKG/Documents/{document}/{rowkey}/prmv
          document = documentnummer uit webhook data.document, bijv. 242
          rowkey   = hex rowkey uit webhook data.rowkey, bijv. "0x0000000008a0f385"
        """
        endpoint = f"/web/v3/MKG/Documents/{document}/{rowkey}/prmv"
        params = {
            "fieldlist": "prdh_num,prdr_num,prmv_num,prmv_lengte,totaal_aantal," 
                         "arti_code,arti_code.arti_mat_lengte,arti_code.arti_handelslengte"
        }
        response = await self._make_request("GET", endpoint, params=params)

        try:
            return response["response"]["ResultData"][0]["prmv"]
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Onverwachte response structuur van MKG prmv endpoint: {e}")
            logger.error(f"Response was: {response}")
            return []
    
    async def create_purchase_order_header(self, cred_num: int = 99999) -> Dict:
        """
        Maakt een nieuwe inkooporder header aan in MKG.

        POST /web/v3/MKG/Documents/iorh/
        Body: {"request":{"InputData":{"iorh":[{"cred_num": <cred_num>}]}}}
        Retourneert: {"RowKey": ..., "admi_num": ..., "iorh_num": ..., "cred_num": ...}
        """
        endpoint = "/web/v3/MKG/Documents/iorh/"
        body = {
            "request": {
                "InputData": {
                    "iorh": [{"cred_num": cred_num}]
                }
            }
        }
        response = await self._make_request("POST", endpoint, json=body)
        try:
            return response["response"]["ResultData"][0]["iorh"][0]
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Onverwachte response structuur van MKG iorh endpoint: {e}")
            logger.error(f"Response was: {response}")
            return {}

    async def create_purchase_order_line(
        self, iorh_num: str, arti_code: str, quantity: float
    ) -> Dict:
        """
        Maakt een inkooporderregel aan onder de opgegeven inkooporder.

        POST /web/v3/MKG/Documents/iorr/
        Body: {"request":{"InputData":{"iorr":[{"iorh_num": ..., "arti_code": ..., "iorr_order_aantal": ...}]}}}
        Retourneert: {"RowKey": ..., "admi_num": ..., "iorh_num": ..., "iorr_num": ..., "arti_code": ..., "iorr_order_aantal": ...}
        """
        endpoint = "/web/v3/MKG/Documents/iorr/"
        body = {
            "request": {
                "InputData": {
                    "iorr": [
                        {
                            "iorh_num": iorh_num,
                            "arti_code": arti_code,
                            "iorr_order_aantal": quantity,
                        }
                    ]
                }
            }
        }
        response = await self._make_request("POST", endpoint, json=body)
        try:
            return response["response"]["ResultData"][0]["iorr"][0]
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Onverwachte response structuur van MKG iorr endpoint: {e}")
            logger.error(f"Response was: {response}")
            return {}

    async def get_pamt_for_order(self, iorh_num: str) -> List[Dict]:
        """
        Haalt batch-mutaties (pamt) op voor een inkooporder.

        GET /web/v3/MKG/Documents/pamt?filter=iorh_num={iorh_num} and pamt_type=2
                                        &fieldlist=iorh_num,iorr_num,iorr
        Retourneert: [{"iorh_num": ..., "iorr_num": ..., "RowKey": ..., "RowKeyParent": ...}]
        """
        # httpx herencodeert '=' in querystring-waarden naar '%3D'.
        # Door ze vooraf als '%3D' te schrijven bewaart httpx de encoding.
        # Spaties als '%20' (niet '+') want MKG verwacht RFC 3986 encoding.
        raw_query = (
            f"filter=iorh_num%3D{iorh_num}%20and%20pamt_type%3D2"
            "&fieldlist=iorh_num,iorr_num,iorr"
        )
        endpoint_with_qs = f"/web/v3/MKG/Documents/pamt?{raw_query}"
        response = await self._make_request("GET", endpoint_with_qs)
        try:
            return response["response"]["ResultData"][0]["pamt"]
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Onverwachte response structuur van MKG pamt endpoint: {e}")
            logger.error(f"Response was: {response}")
            return []

    async def create_reservation(
        self,
        admi_num: int,
        prdh_num,
        prdr_num,
        prmv_num,
        pamt_rowkey: str,
        quantity: float,
        unit: str = "st.",
        issue_date: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Maakt een reservering op een prmv materiaalregel via Service 'Booking stock'.

        PUT /web/v3/MKG/Documents/prmv/{admi_num}+{prdh_num}+{prdr_num}+{prmv_num}
            /Service/s_createreservation?DialogResult=1
        """
        if issue_date is None:
            issue_date = datetime.utcnow().strftime("%Y-%m-%d")

        endpoint = (
            f"/web/v3/MKG/Documents/prmv/"
            f"{admi_num}+{prdh_num}+{prdr_num}+{prmv_num}"
            f"/Service/s_createreservation"
        )
        params = {"DialogResult": 1}
        body = {
            "request": {
                "InputData": {
                    "Reservation": [
                        {
                            "t_eenh_reservering": unit,
                            "t_afgewerkt": True,
                            "t_uitgeven": False,
                            "t_uitgifte_datum": issue_date,
                            "RowKey": 1,
                        }
                    ],
                    "StockAvailableList": [
                        {
                            "RowKey": 1,
                            "t_ingave": quantity,
                            "t_link_aantal": quantity,
                            "t_pamt_row": pamt_rowkey,
                        }
                    ],
                }
            }
        }
        try:
            return await self._make_request("PUT", endpoint, json=body, params=params)
        except Exception as e:
            logger.error(
                f"Reservering mislukt voor prmv {admi_num}+{prdh_num}+{prdr_num}+{prmv_num}: {e}"
            )
            return None

    async def update_prmv_memo(
        self,
        admi_num: int,
        prdh_num,
        prdr_num,
        prmv_num,
        memo: str,
    ) -> Optional[Dict]:
        """
        Schrijft een memo terug op een prmv materiaalregel.

        PUT /web/v3/MKG/Documents/prmv/{admi_num}+{prdh_num}+{prdr_num}+{prmv_num}
        Body: {"request":{"InputData":{"prmv":[{"prmv_memo": "..."}]}}}
        """
        endpoint = (
            f"/web/v3/MKG/Documents/prmv/"
            f"{admi_num}+{prdh_num}+{prdr_num}+{prmv_num}"
        )
        body = {
            "request": {
                "InputData": {
                    "prmv": [{"prmv_memo": memo}]
                }
            }
        }
        try:
            return await self._make_request("PUT", endpoint, json=body)
        except Exception as e:
            logger.error(f"prmv_memo schrijven mislukt voor {admi_num}+{prdh_num}+{prdr_num}+{prmv_num}: {e}")
            return None

    async def update_iorr_memo(
        self,
        admi_num: int,
        iorh_num: str,
        iorr_num: int,
        memo_intern: str,
    ) -> Optional[Dict]:
        """
        Schrijft de iorr_memo_intern terug op een inkooporderregel.

        PUT /web/v3/MKG/Documents/iorr/{admi_num}+{iorh_num}+{iorr_num}
        Body: {"request":{"InputData":{"iorr":[{"iorr_memo_intern": "..."}]}}}
        """
        endpoint = f"/web/v3/MKG/Documents/iorr/{admi_num}+{iorh_num}+{iorr_num}"
        body = {
            "request": {
                "InputData": {
                    "iorr": [{"iorr_memo_intern": memo_intern}]
                }
            }
        }
        try:
            return await self._make_request("PUT", endpoint, json=body)
        except Exception as e:
            logger.error(f"iorr_memo_intern schrijven mislukt voor {admi_num}+{iorh_num}+{iorr_num}: {e}")
            return None

    async def update_production_order_memo(
        self, document: int, rowkey: str,
        memo_extern: str, memo_intern: str
    ) -> Dict:
        """
        Stuurt het gegenereerde zaagplan terug naar de iofa in MKG.

        PUT /web/v3/MKG/Documents/{document}/{rowkey}
          iofa_memo_extern  = ASCII zaagplan tabel (zichtbaar voor klant/operator)
          iofa_memo_intern  = URL naar het zaagplan in de webapp
        """
        endpoint = f"/web/v3/MKG/Documents/{document}/{rowkey}"
        body = {
            "request": {
                "InputData": {
                    "iofa": [
                        {
                            "iofa_memo_extern": memo_extern,
                            "iofa_memo_intern": memo_intern,
                        }
                    ]
                }
            }
        }
        return await self._make_request("PUT", endpoint, json=body)

    async def delete_purchase_order_line(
        self,
        admi_num: int,
        iorh_num: str,
        iorr_num: int,
    ) -> Optional[bool]:
        """
        Verwijdert een inkooporderregel (iorr) uit MKG.
        Retourneert True bij succes, None als de regel niet (meer) bestaat.
        Bij elke HTTP-fout op DELETE wordt eerst via GET gecontroleerd of de
        regel nog bestaat — zo nee, dan behandeld als 'al verwijderd'.
        """
        endpoint = f"/web/v3/MKG/Documents/iorr/{admi_num}+{iorh_num}+{iorr_num}"
        try:
            await self._make_request("DELETE", endpoint)
            return True
        except httpx.HTTPStatusError:
            # Bij elke HTTP-fout: check of de regel er nog is
            still_exists = await self.purchase_order_line_exists(admi_num, iorh_num, iorr_num)
            if not still_exists:
                return None  # al verwijderd, behandel als succes
            raise  # regel bestaat nog maar DELETE faalt — escaleer

    async def purchase_order_line_exists(
        self,
        admi_num: int,
        iorh_num: str,
        iorr_num: int,
    ) -> bool:
        """Controleert via GET of een iorr regel nog bestaat in MKG."""
        endpoint = f"/web/v3/MKG/Documents/iorr/{admi_num}+{iorh_num}+{iorr_num}"
        try:
            result = await self._make_request("GET", endpoint)
            return result is not None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    async def delete_purchase_order_header(
        self,
        admi_num: int,
        iorh_num: str,
    ) -> Optional[bool]:
        """
        Verwijdert een inkooporder header (iorh) uit MKG.
        Retourneert True bij succes, None als de header niet (meer) bestaat.
        Bij elke HTTP-fout op DELETE wordt eerst via GET gecontroleerd of de
        header nog bestaat — zo nee, dan behandeld als 'al verwijderd'.
        """
        endpoint = f"/web/v3/MKG/Documents/iorh/{admi_num}+{iorh_num}"
        try:
            await self._make_request("DELETE", endpoint)
            return True
        except httpx.HTTPStatusError:
            still_exists = await self.purchase_order_header_exists(admi_num, iorh_num)
            if not still_exists:
                return None  # al verwijderd, behandel als succes
            raise  # header bestaat nog maar DELETE faalt — escaleer

    async def purchase_order_header_exists(
        self,
        admi_num: int,
        iorh_num: str,
    ) -> bool:
        """Controleert via GET of een iorh header nog bestaat in MKG."""
        endpoint = f"/web/v3/MKG/Documents/iorh/{admi_num}+{iorh_num}"
        try:
            result = await self._make_request("GET", endpoint)
            return result is not None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    async def close(self):
        """Close the HTTP client"""
        await self.client.aclose()

# Singleton instance (global/legacy)
_mkg_client: Optional[MKGClient] = None

def get_mkg_client() -> MKGClient:
    """Returns the global singleton MKGClient (uses .env settings).
    For multi-tenant use, prefer get_mkg_client_for_env()."""
    global _mkg_client
    if _mkg_client is None:
        _mkg_client = MKGClient()
    return _mkg_client


def get_mkg_client_for_env(env) -> MKGClient:
    """Create a per-user MKGClient from a TenantEnvironment DB row.
    Decrypts the stored password before passing it to the client.
    """
    from auth.security import decrypt_secret

    password = ""
    if env.mkg_password_enc:
        try:
            password = decrypt_secret(env.mkg_password_enc)
        except Exception:
            password = ""

    return MKGClient(
        base_url=env.mkg_base_url or "",
        context_path=env.mkg_context_path or "/mkg",
        api_key=env.mkg_api_key or "",
        username=env.mkg_username or "",
        password=password,
    )
