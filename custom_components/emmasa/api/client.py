"""Asynchronous HTTP client for EMMASA Portal Ciudadano (Elliot Cloud / Sacyr Acuama)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    ACUAMA_API_BASE,
    API_VERSION,
    DEFAULT_GRAFANA_DATASOURCE_TYPE,
    DEFAULT_GRAFANA_DATASOURCE_UID,
    EMMASA_TIMEZONE,
    EXPLOTACION_CODE,
    GRAFANA_BASE,
    GRAFANA_CONSUMOS_DASHBOARD_UID,
    GRAFANA_ORG_ID,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    PORTAL_API_BASE,
)
from .exceptions import (
    EmmasaAuthError,
    EmmasaConnectionError,
    EmmasaMfaRequiredError,
    EmmasaPasswordChangeRequiredError,
    EmmasaQueryError,
)
from .models import (
    BilledConsumption,
    ContractAlert,
    ContractInfo,
    ContractSnapshot,
    GrafanaSession,
    HourlyReading,
    InvoiceSummary,
    MeterReading,
    PeriodConsumption,
    TokenBundle,
    UserProfile,
)
from .queries import (
    build_current_period_query,
    build_hourly_telelectura_query,
    build_latest_billed_query,
    build_latest_daily_consumption_query,
    build_latest_invoice_query,
    build_latest_meter_reading_query,
    validate_contract_uuid,
)

_LOGGER = logging.getLogger(__name__)
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
_DEFAULT_GRAFANA_TTL_SECONDS = 600.0
_DEFAULT_JWT_TTL_SECONDS = 7200.0
_CANARY_TZ = ZoneInfo(EMMASA_TIMEZONE)

TokenUpdateCallback = Callable[[TokenBundle], Awaitable[None]]


def _b64url(data: bytes) -> str:
    """Encode bytes as unpadded base64url ASCII string."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _generate_pkce() -> tuple[str, str, str]:
    """Generate OAuth2 PKCE S256 (code_verifier, code_challenge, state)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(16))
    return verifier, challenge, state


def _decode_jwt_exp(token: str) -> float | None:
    """Extract numeric exp claim from a JWT without verifying signature."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    exp = payload.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def _extract_oauth_code_and_state(url: str) -> tuple[str | None, str | None, str | None]:
    """Extract OAuth authorization code, state, and error from URL query or fragment."""
    parsed = urlparse(url)
    sources = [parse_qs(parsed.query)]
    if parsed.fragment:
        if "?" in parsed.fragment:
            sources.append(parse_qs(parsed.fragment.split("?", 1)[1]))
        sources.append(parse_qs(parsed.fragment.lstrip("#/")))

    code: str | None = None
    state: str | None = None
    error: str | None = None
    for params in sources:
        if code is None and params.get("code"):
            code = params["code"][0]
        if state is None and params.get("state"):
            state = params["state"][0]
        if error is None and params.get("error"):
            error = params["error"][0]
    return code, state, error


def _extract_set_cookies(resp: aiohttp.ClientResponse) -> dict[str, str]:
    """Extract cookie name/value pairs directly from Set-Cookie response headers."""
    cookies: dict[str, str] = {}
    for raw_header in resp.headers.getall("Set-Cookie", ()):
        parsed: SimpleCookie = SimpleCookie()
        parsed.load(raw_header)
        for name, morsel in parsed.items():
            cookies[name] = morsel.value
    return cookies


def _epoch_to_utc_datetime(raw: Any) -> datetime | None:
    """Convert Grafana numeric epoch (ms or s) or ISO string to UTC datetime."""
    if isinstance(raw, (int, float)):
        scale = 1000.0 if abs(raw) > 1e11 else 1.0
        return datetime.fromtimestamp(float(raw) / scale, tz=UTC)
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _extract_frame_rows(ds_response: dict[str, Any], ref_id: str) -> list[dict[str, Any]]:
    """Convert a Grafana DataFrame result into a list of column-name keyed row dicts."""
    results = ds_response.get("results")
    if not isinstance(results, dict):
        return []
    entry = results.get(ref_id)
    if not isinstance(entry, dict):
        return []
    if entry.get("error"):
        raise EmmasaQueryError(f"Grafana query '{ref_id}' failed: {entry['error']}")
    frames = entry.get("frames")
    if not isinstance(frames, list) or not frames:
        return []

    frame = frames[0]
    fields = frame.get("schema", {}).get("fields", [])
    values = frame.get("data", {}).get("values", [])
    if not isinstance(fields, list) or not isinstance(values, list) or not values:
        return []

    col_names = [str(f.get("name", f"col_{idx}")) for idx, f in enumerate(fields)]
    row_count = len(values[0]) if isinstance(values[0], list) else 0
    rows: list[dict[str, Any]] = []
    for row_idx in range(row_count):
        row: dict[str, Any] = {}
        for col_idx, name in enumerate(col_names):
            if col_idx < len(values) and row_idx < len(values[col_idx]):
                row[name] = values[col_idx][row_idx]
        rows.append(row)
    return rows


def _compute_yesterday_liters_from_hourly(
    readings: tuple[HourlyReading, ...],
    now_utc: datetime,
) -> float | None:
    """Fallback calculation of yesterday's consumption in Atlantic/Canary from hourly vol_h."""
    if not readings:
        return None
    yesterday_date = (now_utc.astimezone(_CANARY_TZ) - timedelta(days=1)).date()
    day_values = [
        r.volume_m3
        for r in readings
        if r.timestamp_utc.astimezone(_CANARY_TZ).date() == yesterday_date
    ]
    if not day_values:
        return None
    return round(sum(day_values) * 1000.0, 1)


class EmmasaApiClient:
    """Async aiohttp client for EMMASA Portal Ciudadano with injected websession."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        refresh_token: str | None = None,
        access_token: str | None = None,
        expires_at: float = 0.0,
        on_tokens_updated: TokenUpdateCallback | None = None,
        datasource_uid: str = DEFAULT_GRAFANA_DATASOURCE_UID,
    ) -> None:
        self._session = session
        self._username = username.strip().upper()
        self._password = password
        self._on_tokens_updated = on_tokens_updated
        self._datasource_uid = datasource_uid
        self._tokens: TokenBundle | None = (
            TokenBundle(
                access_token=access_token or "",
                refresh_token=refresh_token,
                expires_at=expires_at,
            )
            if access_token or refresh_token
            else None
        )
        self._grafana_session: GrafanaSession | None = None
        self._bff_loaded_for_token: str | None = None
        self._auth_lock = asyncio.Lock()

    @property
    def token_bundle(self) -> TokenBundle | None:
        """Return the current OAuth2 token bundle, if any."""
        return self._tokens

    @property
    def grafana_session(self) -> GrafanaSession | None:
        """Return the current Grafana session, if any."""
        return self._grafana_session

    @property
    def datasource_uid(self) -> str:
        """Return the active Grafana PostgreSQL datasource UID."""
        return self._datasource_uid

    def _acuama_headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "api-version": API_VERSION,
            "origen": "Web",
            "origenAcceso": "OVWEB",
        }
        if json_body:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return headers

    def _portal_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "api-version": API_VERSION,
            "origen": "Web",
        }

    def _grafana_headers(self, grafana: GrafanaSession, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Grafana-Org-Id": GRAFANA_ORG_ID,
            "Cookie": (
                f"grafana_session={grafana.session_token}; "
                f"grafana_session_expiry={int(grafana.expires_at)}"
            ),
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str | int] | None = None,
        json_data: dict[str, Any] | None = None,
        data: str | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        """Execute an HTTP request and decode JSON safely."""
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_data,
                data=data,
                timeout=_REQUEST_TIMEOUT,
            ) as resp:
                cookies = _extract_set_cookies(resp)
                text = await resp.text()
                if not text:
                    return resp.status, None, cookies
                try:
                    body: Any = json.loads(text)
                except json.JSONDecodeError as err:
                    if resp.status in (401, 403):
                        raise EmmasaAuthError(f"Authentication rejected (HTTP {resp.status})") from err
                    raise EmmasaConnectionError(
                        f"Non-JSON response from {urlparse(url).path} (HTTP {resp.status})"
                    ) from err
                return resp.status, body, cookies
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EmmasaConnectionError(f"Network error calling {urlparse(url).path}: {err}") from err

    async def _notify_tokens_updated(self, tokens: TokenBundle) -> None:
        if self._on_tokens_updated is not None:
            await self._on_tokens_updated(tokens)

    def _parse_token_response(
        self,
        body: Any,
        *,
        fallback_refresh_token: str | None = None,
    ) -> TokenBundle:
        if not isinstance(body, dict) or not body.get("access_token"):
            raise EmmasaAuthError("Token endpoint did not return an access_token")
        access_token = str(body["access_token"])
        refresh_token = str(body["refresh_token"]) if body.get("refresh_token") else fallback_refresh_token
        id_token = str(body["id_token"]) if body.get("id_token") else None
        expires_in = body.get("expires_in")
        jwt_exp = _decode_jwt_exp(access_token)
        if jwt_exp is not None:
            expires_at = jwt_exp
        elif isinstance(expires_in, (int, float)):
            expires_at = time.time() + float(expires_in)
        else:
            expires_at = time.time() + _DEFAULT_JWT_TTL_SECONDS
        return TokenBundle(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            id_token=id_token,
        )

    async def _perform_full_login(self) -> TokenBundle:
        """Perform full OAuth2 Authorization Code + PKCE S256 login against Acuama IdP."""
        verifier, challenge, state = _generate_pkce()
        login_payload = {
            "UserName": self._username,
            "Password": self._password,
            "RememberMe": False,
            "oauthClientId": OAUTH_CLIENT_ID,
            "oauthRedirectUri": OAUTH_REDIRECT_URI,
            "oauthScope": OAUTH_SCOPE,
            "oauthState": state,
            "oauthCodeChallenge": challenge,
            "oauthCodeChallengeMethod": "S256",
        }
        status, body, cookies = await self._request_json(
            "POST",
            f"{ACUAMA_API_BASE}/UsuariosOnline/Login",
            params={"explotacionCodigo": EXPLOTACION_CODE},
            json_data=login_payload,
            headers=self._acuama_headers(json_body=True),
        )

        if status in (401, 403):
            raise EmmasaAuthError("Invalid EMMASA credentials")
        if status >= 500:
            raise EmmasaConnectionError(f"Acuama IdP unavailable (HTTP {status})")
        if not isinstance(body, dict):
            raise EmmasaConnectionError(f"Unexpected Acuama login payload (HTTP {status})")

        if body.get("Ex") or status >= 400:
            ex = body.get("Ex")
            msg = ex.get("Message") if isinstance(ex, dict) else body.get("Message")
            raise EmmasaAuthError(str(msg or f"Login rejected (HTTP {status})"))

        if body.get("loginA2P") is True:
            raise EmmasaMfaRequiredError("EMMASA account requires SMS MFA (loginA2P)")
        if body.get("requiereCambioPassword") is True:
            raise EmmasaPasswordChangeRequiredError("EMMASA account requires password change")

        redirect_url = body.get("redirect_url")
        if not isinstance(redirect_url, str) or not redirect_url:
            if body.get("access_token"):
                tokens = self._parse_token_response(body)
                self._tokens = tokens
                self._bff_loaded_for_token = None
                self._grafana_session = None
                await self._notify_tokens_updated(tokens)
                return tokens
            raise EmmasaAuthError("Acuama login succeeded without OAuth redirect_url")

        code, returned_state, oauth_error = _extract_oauth_code_and_state(redirect_url)
        if oauth_error:
            raise EmmasaAuthError(f"OAuth authorization error: {oauth_error}")

        if not code:
            # Follow Acuama authorize hop carrying explicit ov_idp_session cookie
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
            next_url = urljoin("https://acuamaemmasa.sacyr.com/", redirect_url)
            for _ in range(5):
                parsed = urlparse(next_url)
                if parsed.netloc != "acuamaemmasa.sacyr.com":
                    break
                hop_headers = self._acuama_headers()
                if cookie_header:
                    hop_headers["Cookie"] = cookie_header
                try:
                    async with self._session.get(
                        next_url,
                        headers=hop_headers,
                        allow_redirects=False,
                        timeout=_REQUEST_TIMEOUT,
                    ) as hop_resp:
                        new_cookies = _extract_set_cookies(hop_resp)
                        if new_cookies:
                            cookies.update(new_cookies)
                            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
                        loc = hop_resp.headers.get("Location", "")
                except (aiohttp.ClientError, TimeoutError) as err:
                    raise EmmasaConnectionError(f"Network error during OAuth authorize hop: {err}") from err
                if not loc:
                    break
                code, returned_state, oauth_error = _extract_oauth_code_and_state(loc)
                if oauth_error:
                    raise EmmasaAuthError(f"OAuth redirect error: {oauth_error}")
                if code:
                    break
                next_url = urljoin(next_url, loc)

        if not code:
            raise EmmasaAuthError("OAuth authorize redirect did not yield an authorization code")
        if returned_state and returned_state != state:
            raise EmmasaAuthError("OAuth state verification failed")

        token_form = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "client_id": OAUTH_CLIENT_ID,
                "code_verifier": verifier,
            }
        )
        token_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "api-version": API_VERSION,
            "origen": "Web",
        }
        tok_status, tok_body, _ = await self._request_json(
            "POST",
            f"{ACUAMA_API_BASE}/UsuariosOnline/token",
            data=token_form,
            headers=token_headers,
        )
        if tok_status in (400, 401, 403):
            raise EmmasaAuthError(f"OAuth token exchange failed (HTTP {tok_status})")
        if tok_status >= 500:
            raise EmmasaConnectionError(f"Acuama token endpoint error (HTTP {tok_status})")

        tokens = self._parse_token_response(tok_body)
        self._tokens = tokens
        self._bff_loaded_for_token = None
        self._grafana_session = None
        await self._notify_tokens_updated(tokens)
        return tokens

    async def _perform_refresh(self, refresh_token: str) -> TokenBundle:
        """Renew OAuth2 tokens using a rotating refresh_token."""
        refresh_form = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": OAUTH_CLIENT_ID,
            }
        )
        token_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "api-version": API_VERSION,
            "origen": "Web",
        }
        status, body, _ = await self._request_json(
            "POST",
            f"{ACUAMA_API_BASE}/UsuariosOnline/token",
            data=refresh_form,
            headers=token_headers,
        )
        if status in (400, 401, 403):
            raise EmmasaAuthError(f"Refresh token rejected (HTTP {status})")
        if status >= 500:
            raise EmmasaConnectionError(f"Acuama token refresh error (HTTP {status})")

        tokens = self._parse_token_response(body, fallback_refresh_token=refresh_token)
        self._tokens = tokens
        self._bff_loaded_for_token = None
        self._grafana_session = None
        await self._notify_tokens_updated(tokens)
        return tokens

    async def _load_bff_session(self, access_token: str) -> None:
        """Notify Portal Ciudadano BFF (/api/auth/load) of the active access token."""
        if self._bff_loaded_for_token == access_token:
            return
        status, _, _ = await self._request_json(
            "POST",
            f"{PORTAL_API_BASE}/auth/load",
            json_data={"userToken": access_token},
            headers=self._portal_headers(access_token),
        )
        if status in (401, 403):
            raise EmmasaAuthError(f"Portal BFF /auth/load rejected token (HTTP {status})")
        if status >= 400:
            raise EmmasaConnectionError(f"Portal BFF /auth/load failed (HTTP {status})")
        self._bff_loaded_for_token = access_token

    async def async_authenticate(self) -> TokenBundle:
        """Force a full PKCE authentication and load the Portal BFF session."""
        async with self._auth_lock:
            tokens = await self._perform_full_login()
            await self._load_bff_session(tokens.access_token)
            return tokens

    async def async_ensure_authenticated(self, *, force_refresh: bool = False) -> str:
        """Ensure a valid OAuth2 access token and BFF session exist, returning the JWT."""
        if (
            not force_refresh
            and self._tokens is not None
            and self._tokens.access_token
            and not self._tokens.is_expired()
            and self._bff_loaded_for_token == self._tokens.access_token
        ):
            return self._tokens.access_token

        async with self._auth_lock:
            if (
                not force_refresh
                and self._tokens is not None
                and self._tokens.access_token
                and not self._tokens.is_expired()
            ):
                await self._load_bff_session(self._tokens.access_token)
                return self._tokens.access_token

            tokens: TokenBundle | None = None
            if self._tokens is not None and self._tokens.refresh_token:
                try:
                    tokens = await self._perform_refresh(self._tokens.refresh_token)
                except EmmasaAuthError:
                    _LOGGER.debug("EMMASA refresh_token rejected; falling back to full PKCE login")
                    tokens = None

            if tokens is None:
                tokens = await self._perform_full_login()

            await self._load_bff_session(tokens.access_token)
            return tokens.access_token

    async def _portal_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> Any:
        """Perform an authenticated Portal BFF request with single 401 refresh retry."""
        access_token = await self.async_ensure_authenticated()
        url = f"{PORTAL_API_BASE}/{path.lstrip('/')}"
        status, body, _ = await self._request_json(
            method,
            url,
            headers=self._portal_headers(access_token),
            params=params,
            json_data=json_data,
        )
        if status == 401:
            access_token = await self.async_ensure_authenticated(force_refresh=True)
            status, body, _ = await self._request_json(
                method,
                url,
                headers=self._portal_headers(access_token),
                params=params,
                json_data=json_data,
            )
        if status in (401, 403):
            raise EmmasaAuthError(f"Portal API {path} unauthorized (HTTP {status})")
        if status >= 400:
            raise EmmasaConnectionError(f"Portal API {path} failed (HTTP {status})")
        return body

    async def async_ensure_grafana_session(self, *, force_refresh: bool = False) -> GrafanaSession:
        """Obtain or renew the short-lived (~10 min) Grafana session cookie."""
        if (
            not force_refresh
            and self._grafana_session is not None
            and not self._grafana_session.is_expired()
        ):
            return self._grafana_session

        body = await self._portal_request("POST", "/auth/grafana", json_data={})
        if not isinstance(body, dict) or not body.get("grafana_session"):
            raise EmmasaConnectionError("Portal /auth/grafana did not return grafana_session")
        raw_expiry = body.get("grafana_session_expiry")
        expires_at = (
            float(raw_expiry)
            if isinstance(raw_expiry, (int, float, str)) and str(raw_expiry).isdigit()
            else time.time() + _DEFAULT_GRAFANA_TTL_SECONDS
        )
        self._grafana_session = GrafanaSession(
            session_token=str(body["grafana_session"]),
            expires_at=expires_at,
        )
        return self._grafana_session

    async def async_get_profile(self) -> UserProfile:
        """Fetch authenticated user profile from /api/auth/me."""
        body = await self._portal_request("GET", "/auth/me")
        if not isinstance(body, dict):
            raise EmmasaConnectionError("Invalid response from /api/auth/me")
        user_obj = body.get("result", body)
        if isinstance(user_obj, dict) and isinstance(user_obj.get("user"), dict):
            user_obj = user_obj["user"]
        if not isinstance(user_obj, dict):
            raise EmmasaConnectionError("Malformed user object in /api/auth/me")
        login = str(user_obj.get("login") or self._username).strip().upper()
        username = str(user_obj.get("username") or login).strip()
        email = str(user_obj["email"]).strip() if user_obj.get("email") else None
        return UserProfile(username=username, login=login, email=email)

    async def async_get_contracts(self) -> list[ContractInfo]:
        """Fetch all water supply contracts associated with the account."""
        body = await self._portal_request(
            "POST",
            "/contract/advanced-search",
            params={"page": 0, "size": 1000, "sort": "ASC", "sortField": "id"},
            json_data={"where": {}},
        )
        page_obj = body.get("result", body) if isinstance(body, dict) else {}
        raw_items = page_obj.get("content", []) if isinstance(page_obj, dict) else []
        if not isinstance(raw_items, list):
            raise EmmasaConnectionError("Invalid contract list from /api/contract/advanced-search")

        contracts: list[ContractInfo] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id") or item.get("contratoId")
            if not raw_id:
                continue
            contract_id = validate_contract_uuid(str(raw_id))
            name = str(item.get("name") or contract_id[:8]).strip()
            allows_alerts = bool(item.get("allowsAlertConfiguration", False))
            contracts.append(
                ContractInfo(
                    contract_id=contract_id,
                    name=name,
                    allows_alert_configuration=allows_alerts,
                )
            )
        return contracts

    async def async_get_contract_alerts(self, contract_id: str) -> list[ContractAlert]:
        """Fetch read-only consumption alerts for a given contract."""
        cid = validate_contract_uuid(contract_id)
        body = await self._portal_request(
            "GET",
            f"/contract-alerts/{cid}",
            params={"page": 0, "size": 1000, "sort": "ASC", "sortField": "id"},
        )
        page_obj = body.get("result", body) if isinstance(body, dict) else {}
        raw_items = page_obj.get("content", []) if isinstance(page_obj, dict) else []
        if not isinstance(raw_items, list):
            return []

        alerts: list[ContractAlert] = []
        for item in raw_items:
            if not isinstance(item, dict) or not item.get("alertId"):
                continue
            cfg = item.get("userAlertConfiguration")
            cfg_dict = cfg if isinstance(cfg, dict) else {}
            raw_limit = cfg_dict.get("userLimit")
            user_limit = float(raw_limit) if isinstance(raw_limit, (int, float)) else None
            alerts.append(
                ContractAlert(
                    alert_id=str(item["alertId"]),
                    alert_name=str(item.get("alertName", "")),
                    alert_description=str(item.get("alertDescription", "")),
                    config_id=str(cfg_dict["id"]) if cfg_dict.get("id") is not None else None,
                    user_limit_liters=user_limit,
                    is_active=bool(cfg_dict.get("isActive", False)),
                    allows_alert_configuration=bool(item.get("allowsAlertConfiguration", False)),
                )
            )
        return alerts

    async def async_discover_datasource_uid(self) -> str:
        """Auto-discover Grafana PostgreSQL datasource UID from the consumos dashboard."""
        grafana = await self.async_ensure_grafana_session()
        status, body, _ = await self._request_json(
            "GET",
            f"{GRAFANA_BASE}/api/dashboards/uid/{GRAFANA_CONSUMOS_DASHBOARD_UID}",
            headers=self._grafana_headers(grafana),
        )
        if status in (401, 403):
            grafana = await self.async_ensure_grafana_session(force_refresh=True)
            status, body, _ = await self._request_json(
                "GET",
                f"{GRAFANA_BASE}/api/dashboards/uid/{GRAFANA_CONSUMOS_DASHBOARD_UID}",
                headers=self._grafana_headers(grafana),
            )
        if status != 200 or not isinstance(body, dict):
            raise EmmasaQueryError(f"Failed to fetch Grafana dashboard metadata (HTTP {status})")

        panels = body.get("dashboard", {}).get("panels", [])
        if isinstance(panels, list):
            for panel in panels:
                if not isinstance(panel, dict):
                    continue
                ds = panel.get("datasource")
                if (
                    isinstance(ds, dict)
                    and ds.get("type") == DEFAULT_GRAFANA_DATASOURCE_TYPE
                    and isinstance(ds.get("uid"), str)
                    and ds["uid"]
                ):
                    self._datasource_uid = ds["uid"]
                    return self._datasource_uid
        raise EmmasaQueryError("Could not locate PostgreSQL datasource UID in Grafana dashboard")

    async def _grafana_ds_query(
        self,
        queries: list[dict[str, Any]],
        *,
        from_ms: int,
        to_ms: int,
    ) -> dict[str, Any]:
        """Execute a batch POST /grafana/api/ds/query with session & UID recovery."""
        grafana = await self.async_ensure_grafana_session()
        payload: dict[str, Any] = {
            "queries": queries,
            "from": str(from_ms),
            "to": str(to_ms),
        }
        status, body, _ = await self._request_json(
            "POST",
            f"{GRAFANA_BASE}/api/ds/query",
            headers=self._grafana_headers(grafana, json_body=True),
            json_data=payload,
        )

        if status in (401, 403):
            grafana = await self.async_ensure_grafana_session(force_refresh=True)
            status, body, _ = await self._request_json(
                "POST",
                f"{GRAFANA_BASE}/api/ds/query",
                headers=self._grafana_headers(grafana, json_body=True),
                json_data=payload,
            )

        if status == 404 or (
            status == 400
            and isinstance(body, dict)
            and "datasource" in str(body.get("message", "")).lower()
        ):
            previous_uid = self._datasource_uid
            new_uid = await self.async_discover_datasource_uid()
            if new_uid != previous_uid:
                for q in queries:
                    if isinstance(q.get("datasource"), dict):
                        q["datasource"] = {
                            "type": DEFAULT_GRAFANA_DATASOURCE_TYPE,
                            "uid": new_uid,
                        }
                grafana = await self.async_ensure_grafana_session()
                status, body, _ = await self._request_json(
                    "POST",
                    f"{GRAFANA_BASE}/api/ds/query",
                    headers=self._grafana_headers(grafana, json_body=True),
                    json_data={"queries": queries, "from": str(from_ms), "to": str(to_ms)},
                )

        if status in (401, 403):
            raise EmmasaAuthError(f"Grafana ds/query unauthorized (HTTP {status})")
        if status >= 500:
            raise EmmasaConnectionError(f"Grafana ds/query server error (HTTP {status})")
        if status >= 400 or not isinstance(body, dict):
            raise EmmasaQueryError(f"Grafana ds/query failed (HTTP {status})")
        return body

    async def async_fetch_contract_snapshot(
        self,
        contract: ContractInfo,
        since_utc: datetime,
        *,
        include_alerts: bool = True,
    ) -> ContractSnapshot:
        """Fetch hourly readings, meter index, period, billing, and alerts in one Grafana batch."""
        now_utc = datetime.now(UTC)
        from_ms = int(since_utc.astimezone(UTC).timestamp() * 1000)
        to_ms = int(now_utc.timestamp() * 1000)
        ds = {
            "type": DEFAULT_GRAFANA_DATASOURCE_TYPE,
            "uid": self._datasource_uid,
        }

        def _q(ref_id: str, sql: str) -> dict[str, Any]:
            return {
                "refId": ref_id,
                "datasource": ds,
                "rawQuery": True,
                "format": "table",
                "rawSql": sql,
            }

        queries = [
            _q("hourly", build_hourly_telelectura_query(contract.contract_id, since_utc)),
            _q("meter", build_latest_meter_reading_query(contract.contract_id)),
            _q("period", build_current_period_query(contract.contract_id)),
            _q("daily", build_latest_daily_consumption_query(contract.contract_id)),
            _q("billed", build_latest_billed_query(contract.contract_id)),
            _q("invoice", build_latest_invoice_query(contract.contract_id)),
        ]
        ds_response = await self._grafana_ds_query(queries, from_ms=from_ms, to_ms=to_ms)

        # 1. Hourly readings (ts_telelectura.vol_h)
        hourly_list: list[HourlyReading] = []
        for row in _extract_frame_rows(ds_response, "hourly"):
            ts = _epoch_to_utc_datetime(row.get("ts_hour"))
            val = row.get("vol_h")
            if ts is not None and isinstance(val, (int, float)):
                # Normalize exact top-of-hour UTC
                ts_hour = ts.replace(minute=0, second=0, microsecond=0)
                hourly_list.append(HourlyReading(timestamp_utc=ts_hour, volume_m3=round(max(float(val), 0.0), 6)))
        hourly_list.sort(key=lambda r: r.timestamp_utc)
        hourly_readings = tuple(hourly_list)

        # 2. Latest cumulative meter reading (vol_acc)
        latest_meter: MeterReading | None = None
        meter_rows = _extract_frame_rows(ds_response, "meter")
        if meter_rows:
            ts = _epoch_to_utc_datetime(meter_rows[0].get("ts"))
            vol = meter_rows[0].get("vol_acc")
            if ts is not None and isinstance(vol, (int, float)):
                latest_meter = MeterReading(timestamp_utc=ts, volume_acc_m3=round(float(vol), 3))

        # 3. Current unbilled period consumption
        current_period: PeriodConsumption | None = None
        period_rows = _extract_frame_rows(ds_response, "period")
        if period_rows:
            vol = period_rows[0].get("consumo_periodo")
            since_dt = _epoch_to_utc_datetime(period_rows[0].get("fecha_ultimo_periodo"))
            if isinstance(vol, (int, float)):
                current_period = PeriodConsumption(since_utc=since_dt, volume_m3=round(float(vol), 3))

        # 4. Yesterday / latest closed complete day consumption (L)
        yesterday_liters: float | None = None
        daily_rows = _extract_frame_rows(ds_response, "daily")
        if daily_rows:
            val = daily_rows[0].get("volumen_litros")
            if isinstance(val, (int, float)):
                yesterday_liters = round(float(val), 1)
        if yesterday_liters is None:
            yesterday_liters = _compute_yesterday_liters_from_hourly(hourly_readings, now_utc)

        # 5. Latest officially billed bimonthly consumption
        latest_billed: BilledConsumption | None = None
        billed_rows = _extract_frame_rows(ds_response, "billed")
        if billed_rows:
            code_raw = billed_rows[0].get("codigo_periodo")
            vol_raw = billed_rows[0].get("consumo_registrado")
            label_raw = billed_rows[0].get("periodo_texto")
            if isinstance(code_raw, (int, float)) and isinstance(vol_raw, (int, float)):
                latest_billed = BilledConsumption(
                    period_code=int(code_raw),
                    period_label=str(label_raw or int(code_raw)),
                    volume_m3=round(float(vol_raw), 3),
                )

        # 6. Latest invoice
        latest_invoice: InvoiceSummary | None = None
        invoice_rows = _extract_frame_rows(ds_response, "invoice")
        if invoice_rows:
            amt = invoice_rows[0].get("importe_factura")
            period_code = invoice_rows[0].get("cod_periodo")
            inv_dt = _epoch_to_utc_datetime(invoice_rows[0].get("fecha_factura"))
            if isinstance(amt, (int, float)) and period_code is not None:
                latest_invoice = InvoiceSummary(
                    period_code=str(period_code),
                    amount_eur=round(float(amt), 2),
                    invoice_date_utc=inv_dt,
                )

        # 7. Alerts (read-only)
        alerts: tuple[ContractAlert, ...] = ()
        if include_alerts and contract.allows_alert_configuration:
            alerts = tuple(await self.async_get_contract_alerts(contract.contract_id))

        # Compute most recent telemetry timestamp
        candidate_ts = [
            ts
            for ts in (
                hourly_readings[-1].timestamp_utc if hourly_readings else None,
                latest_meter.timestamp_utc if latest_meter else None,
            )
            if ts is not None
        ]
        last_reading_ts = max(candidate_ts) if candidate_ts else None

        return ContractSnapshot(
            contract=contract,
            hourly_readings=hourly_readings,
            latest_meter_reading=latest_meter,
            current_period_consumption=current_period,
            yesterday_consumption_liters=yesterday_liters,
            latest_billed_consumption=latest_billed,
            latest_invoice=latest_invoice,
            alerts=alerts,
            last_reading_timestamp_utc=last_reading_ts,
        )

    async def async_logout(self) -> None:
        """Best-effort close of the Portal Ciudadano BFF session."""
        if self._tokens is None or not self._tokens.access_token:
            return
        try:
            await self._request_json(
                "GET",
                f"{PORTAL_API_BASE}/auth/logout",
                headers=self._portal_headers(self._tokens.access_token),
            )
        except (EmmasaConnectionError, EmmasaAuthError):
            _LOGGER.debug("Ignoring error during best-effort EMMASA logout")
        finally:
            self._grafana_session = None
            self._bff_loaded_for_token = None
