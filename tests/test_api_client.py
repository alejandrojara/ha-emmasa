"""Unit tests for the decoupled EMMASA aiohttp API client and SQL query builders."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.emmasa.api import (
    ContractInfo,
    EmmasaApiClient,
    EmmasaAuthError,
    EmmasaConnectionError,
    EmmasaMfaRequiredError,
    EmmasaPasswordChangeRequiredError,
    EmmasaQueryError,
    HourlyReading,
    TokenBundle,
)
from custom_components.emmasa.api.client import (
    _compute_yesterday_liters_from_hourly,
    _decode_jwt_exp,
    _epoch_to_utc_datetime,
    _extract_frame_rows,
)
from custom_components.emmasa.api.const import (
    ACUAMA_API_BASE,
    GRAFANA_BASE,
    GRAFANA_CONSUMOS_DASHBOARD_UID,
    PORTAL_API_BASE,
)
from custom_components.emmasa.api.queries import (
    build_current_period_query,
    build_hourly_telelectura_query,
    build_latest_billed_query,
    build_latest_daily_consumption_query,
    build_latest_invoice_query,
    build_latest_meter_reading_query,
    validate_contract_uuid,
)

from .conftest import (
    MOCK_CONTRACT_NAME,
    MOCK_CONTRACT_UUID,
    MOCK_PASSWORD,
    MOCK_USERNAME,
    make_test_jwt,
)


def _mock_resp(
    method: str,
    url: str,
    *,
    status: int = 200,
    json: Any = None,
    text: str | None = None,
    headers: dict[str, str] | None = None,
    exc: Exception | None = None,
) -> AiohttpClientMockResponse:
    """Create an AiohttpClientMockResponse helper."""
    return AiohttpClientMockResponse(
        method=method,
        url=URL(url),
        status=status,
        json=json,
        text=text,
        headers=headers,
        exc=exc,
    )


def _seq_side_effect(
    responses: list[AiohttpClientMockResponse],
) -> Callable[[str, URL, Any], Any]:
    """Return an async side_effect yielding responses sequentially."""
    iterator = iter(responses)

    async def _handler(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        return next(iterator)

    return _handler


def _make_grafana_ds_response(
    *,
    hourly_rows: list[tuple[int, float]] | None = None,
    meter_row: tuple[int, float] | None = None,
    period_row: tuple[int, float] | None = None,
    daily_row: tuple[str, float] | None = None,
    billed_row: tuple[int, float, str] | None = None,
    invoice_row: tuple[str, float, int] | None = None,
    error_ref: str | None = None,
) -> dict[str, Any]:
    """Construct a realistic Grafana 10.4 /api/ds/query DataFrame response."""
    results: dict[str, Any] = {}

    if hourly_rows is not None:
        results["hourly"] = {
            "status": 200,
            "frames": [
                {
                    "schema": {
                        "fields": [
                            {"name": "ts_hour", "type": "time"},
                            {"name": "vol_h", "type": "number"},
                        ]
                    },
                    "data": {"values": [[r[0] for r in hourly_rows], [r[1] for r in hourly_rows]]},
                }
            ],
        }
    if meter_row is not None:
        results["meter"] = {
            "status": 200,
            "frames": [
                {
                    "schema": {
                        "fields": [
                            {"name": "ts", "type": "time"},
                            {"name": "vol_acc", "type": "number"},
                        ]
                    },
                    "data": {"values": [[meter_row[0]], [meter_row[1]]]},
                }
            ],
        }
    if period_row is not None:
        results["period"] = {
            "status": 200,
            "frames": [
                {
                    "schema": {
                        "fields": [
                            {"name": "fecha_ultimo_periodo", "type": "time"},
                            {"name": "consumo_periodo", "type": "number"},
                        ]
                    },
                    "data": {"values": [[period_row[0]], [period_row[1]]]},
                }
            ],
        }
    if daily_row is not None:
        results["daily"] = {
            "status": 200,
            "frames": [
                {
                    "schema": {
                        "fields": [
                            {"name": "fecha_simple", "type": "string"},
                            {"name": "volumen_litros", "type": "number"},
                        ]
                    },
                    "data": {"values": [[daily_row[0]], [daily_row[1]]]},
                }
            ],
        }
    if billed_row is not None:
        results["billed"] = {
            "status": 200,
            "frames": [
                {
                    "schema": {
                        "fields": [
                            {"name": "codigo_periodo", "type": "number"},
                            {"name": "consumo_registrado", "type": "number"},
                            {"name": "periodo_texto", "type": "string"},
                        ]
                    },
                    "data": {"values": [[billed_row[0]], [billed_row[1]], [billed_row[2]]]},
                }
            ],
        }
    if invoice_row is not None:
        results["invoice"] = {
            "status": 200,
            "frames": [
                {
                    "schema": {
                        "fields": [
                            {"name": "cod_periodo", "type": "string"},
                            {"name": "importe_factura", "type": "number"},
                            {"name": "fecha_factura", "type": "time"},
                        ]
                    },
                    "data": {"values": [[invoice_row[0]], [invoice_row[1]], [invoice_row[2]]]},
                }
            ],
        }
    if error_ref is not None:
        results[error_ref] = {"status": 500, "error": "db query error"}

    return {"results": results}


def test_query_builders_validate_uuid_and_reject_injection() -> None:
    """SQL builders validate UUIDs strictly and reject injection attempts."""
    valid = validate_contract_uuid(MOCK_CONTRACT_UUID.upper())
    assert valid == MOCK_CONTRACT_UUID

    with pytest.raises(ValueError):
        validate_contract_uuid("not-a-uuid'; DROP TABLE users; --")

    since = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    hourly_sql = build_hourly_telelectura_query(MOCK_CONTRACT_UUID, since)
    assert f"'{MOCK_CONTRACT_UUID}'" in hourly_sql
    assert "SUM(tw.vol_h)" in hourly_sql
    assert "DISTINCT vol_h" not in hourly_sql

    # Naive datetime is normalized as UTC
    naive_sql = build_hourly_telelectura_query(MOCK_CONTRACT_UUID, datetime(2026, 11, 20, 12, 0))
    assert "2026-11-20T12:00:00+00:00" in naive_sql

    for builder in (
        build_latest_meter_reading_query,
        build_current_period_query,
        build_latest_daily_consumption_query,
        build_latest_billed_query,
        build_latest_invoice_query,
    ):
        sql = builder(MOCK_CONTRACT_UUID)
        assert f"'{MOCK_CONTRACT_UUID}'" in sql


def test_client_low_level_helpers() -> None:
    """Verify JWT exp decoding, epoch/ISO conversion, DataFrame parsing, and yesterday fallback."""
    assert _decode_jwt_exp("not-a-jwt") is None
    assert _decode_jwt_exp("a.!!!invalid!!!.c") is None
    no_exp_payload = base64.urlsafe_b64encode(b'{"sub":"x"}').rstrip(b"=").decode()
    assert _decode_jwt_exp(f"hdr.{no_exp_payload}.sig") is None

    assert _epoch_to_utc_datetime(1700000000) == datetime.fromtimestamp(1700000000, tz=UTC)
    assert _epoch_to_utc_datetime(1700000000000) == datetime.fromtimestamp(1700000000, tz=UTC)
    assert _epoch_to_utc_datetime("2026-11-25T06:00:00Z") == datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
    assert _epoch_to_utc_datetime("2026-11-25T06:00:00") == datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
    assert _epoch_to_utc_datetime("invalid-iso") is None
    assert _epoch_to_utc_datetime(None) is None

    assert _extract_frame_rows({}, "hourly") == []
    assert _extract_frame_rows({"results": {}}, "hourly") == []
    assert _extract_frame_rows({"results": {"hourly": {"frames": []}}}, "hourly") == []
    assert (
        _extract_frame_rows(
            {"results": {"hourly": {"frames": [{"schema": {"fields": []}, "data": {"values": []}}]}}},
            "hourly",
        )
        == []
    )

    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    assert _compute_yesterday_liters_from_hourly((), now) is None
    assert (
        _compute_yesterday_liters_from_hourly(
            (HourlyReading(timestamp_utc=now, volume_m3=0.1),),
            now,
        )
        is None
    )


async def test_full_pkce_login_and_token_callback() -> None:
    """Full PKCE S256 login follows authorize redirect with explicit cookie and verifies challenge."""
    mocker = AiohttpClientMocker()
    captured: dict[str, Any] = {}
    persisted: list[TokenBundle] = []

    async def on_tokens(bundle: TokenBundle) -> None:
        persisted.append(bundle)

    async def login_cb(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        captured["state"] = data["oauthState"]
        captured["challenge"] = data["oauthCodeChallenge"]
        return _mock_resp(
            "POST",
            str(url),
            status=200,
            json={
                "success": True,
                "redirect_url": (
                    "https://acuamaemmasa.sacyr.com/webApi/api/UsuariosOnline/authorize"
                    f"?state={data['oauthState']}"
                ),
            },
            headers={"Set-Cookie": "ov_idp_session=idp-cookie-xyz; path=/; HttpOnly"},
        )

    async def authorize_cb(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        _, _, _, req_headers = mocker.mock_calls[-1]
        assert "ov_idp_session=idp-cookie-xyz" in req_headers.get("Cookie", "")
        return _mock_resp(
            "GET",
            str(url),
            status=302,
            headers={
                "Set-Cookie": "ov_extra=abc; path=/",
                "Location": (
                    "https://portalciudadano.emmasa.elliotcloud.com/callback"
                    f"?code=auth-code-123&state={captured['state']}"
                ),
            },
        )

    async def token_cb(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        form = parse_qs(data)
        verifier = form["code_verifier"][0]
        expected_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert expected_challenge == captured["challenge"]
        assert form["code"][0] == "auth-code-123"
        return _mock_resp(
            "POST",
            str(url),
            status=200,
            json={
                "access_token": make_test_jwt(),
                "refresh_token": "rt-initial",
                "id_token": make_test_jwt(),
                "expires_in": 7200,
                "token_type": "Bearer",
            },
        )

    mocker.post(
        f"{ACUAMA_API_BASE}/UsuariosOnline/Login?explotacionCodigo=Emmasa",
        side_effect=login_cb,
    )
    mocker.get(
        re.compile(r"https://acuamaemmasa\.sacyr\.com/webApi/api/UsuariosOnline/authorize.*"),
        side_effect=authorize_cb,
    )
    mocker.post(f"{ACUAMA_API_BASE}/UsuariosOnline/token", side_effect=token_cb)
    mocker.post(f"{PORTAL_API_BASE}/auth/load", status=204)

    async with mocker.create_session(asyncio.get_running_loop()) as session:
        client = EmmasaApiClient(
            session,
            MOCK_USERNAME,
            MOCK_PASSWORD,
            on_tokens_updated=on_tokens,
        )
        tokens = await client.async_authenticate()

    assert tokens.refresh_token == "rt-initial"
    assert not tokens.is_expired()
    assert len(persisted) == 1
    assert persisted[0].refresh_token == "rt-initial"


async def test_login_direct_token_and_expiry_fallbacks() -> None:
    """Handle direct token in Login response and opaque access_token TTL fallbacks."""
    # Direct access_token in Login body with numeric expires_in (non-JWT token)
    mocker = AiohttpClientMocker()
    mocker.post(
        f"{ACUAMA_API_BASE}/UsuariosOnline/Login?explotacionCodigo=Emmasa",
        status=200,
        json={"access_token": "opaque-token-1", "expires_in": 3600},
    )
    mocker.post(f"{PORTAL_API_BASE}/auth/load", status=204)

    async with mocker.create_session(asyncio.get_running_loop()) as session:
        client = EmmasaApiClient(session, MOCK_USERNAME, MOCK_PASSWORD)
        tokens = await client.async_authenticate()
        assert tokens.access_token == "opaque-token-1"
        assert tokens.expires_at > time.time() + 3000

    # Opaque token without expires_in falls back to default 7200s TTL
    mocker2 = AiohttpClientMocker()
    mocker2.post(
        f"{ACUAMA_API_BASE}/UsuariosOnline/Login?explotacionCodigo=Emmasa",
        status=200,
        json={"access_token": "opaque-token-2"},
    )
    mocker2.post(f"{PORTAL_API_BASE}/auth/load", status=204)

    async with mocker2.create_session(asyncio.get_running_loop()) as session:
        client2 = EmmasaApiClient(session, MOCK_USERNAME, MOCK_PASSWORD)
        tokens2 = await client2.async_authenticate()
        assert tokens2.expires_at > time.time() + 6000


async def test_login_error_conditions() -> None:
    """Verify MFA, password change, invalid credentials, OAuth errors, and upstream 5xx handling."""
    cases: list[tuple[int, Any, type[Exception]]] = [
        (401, {"Message": "Unauthorized"}, EmmasaAuthError),
        (503, {"Message": "Down"}, EmmasaConnectionError),
        (200, ["not-a-dict"], EmmasaConnectionError),
        (200, {"Ex": {"Message": "Credenciales inválidas"}}, EmmasaAuthError),
        (400, {"Message": "Bad request"}, EmmasaAuthError),
        (200, {"loginA2P": True, "success": True}, EmmasaMfaRequiredError),
        (200, {"requiereCambioPassword": True, "success": True}, EmmasaPasswordChangeRequiredError),
        (200, {"success": True}, EmmasaAuthError),
        (
            200,
            {"redirect_url": "https://portalciudadano.emmasa.elliotcloud.com/callback?error=access_denied"},
            EmmasaAuthError,
        ),
        (
            200,
            {"redirect_url": "https://portalciudadano.emmasa.elliotcloud.com/callback?code=c1&state=wrong-state"},
            EmmasaAuthError,
        ),
    ]
    for status, body, exc_type in cases:
        mocker = AiohttpClientMocker()
        mocker.post(
            f"{ACUAMA_API_BASE}/UsuariosOnline/Login?explotacionCodigo=Emmasa",
            status=status,
            json=body,
        )
        async with mocker.create_session(asyncio.get_running_loop()) as session:
            client = EmmasaApiClient(session, MOCK_USERNAME, MOCK_PASSWORD)
            with pytest.raises(exc_type):
                await client.async_authenticate()

    # Authorize hop errors: OAuth redirect error, missing code, network error, external redirect
    for hop_kwargs, exc_type in (
        (
            {"status": 302, "headers": {"Location": "https://portalciudadano.emmasa.elliotcloud.com/callback?error=denied"}},
            EmmasaAuthError,
        ),
        ({"status": 200, "headers": {}}, EmmasaAuthError),
        (
            {"status": 302, "headers": {"Location": "https://external.example.com/no-code"}},
            EmmasaAuthError,
        ),
        ({"exc": aiohttp.ClientConnectionError("hop broken")}, EmmasaConnectionError),
    ):
        mocker = AiohttpClientMocker()
        mocker.post(
            f"{ACUAMA_API_BASE}/UsuariosOnline/Login?explotacionCodigo=Emmasa",
            status=200,
            json={
                "success": True,
                "redirect_url": "https://acuamaemmasa.sacyr.com/webApi/api/UsuariosOnline/authorize?x=1",
            },
        )
        mocker.get(
            re.compile(r"https://acuamaemmasa\.sacyr\.com/webApi/api/UsuariosOnline/authorize.*"),
            **hop_kwargs,
        )
        async with mocker.create_session(asyncio.get_running_loop()) as session:
            client = EmmasaApiClient(session, MOCK_USERNAME, MOCK_PASSWORD)
            with pytest.raises(exc_type):
                await client.async_authenticate()

    # Token exchange 400, 500, and missing access_token
    for tok_status, tok_body, exc_type in (
        (400, {"error": "invalid_grant"}, EmmasaAuthError),
        (502, {"error": "upstream"}, EmmasaConnectionError),
        (200, {"token_type": "Bearer"}, EmmasaAuthError),
    ):
        mocker = AiohttpClientMocker()
        mocker.post(
            f"{ACUAMA_API_BASE}/UsuariosOnline/Login?explotacionCodigo=Emmasa",
            status=200,
            json={
                "success": True,
                "redirect_url": "https://portalciudadano.emmasa.elliotcloud.com/callback?code=ok-code",
            },
        )
        mocker.post(f"{ACUAMA_API_BASE}/UsuariosOnline/token", status=tok_status, json=tok_body)
        async with mocker.create_session(asyncio.get_running_loop()) as session:
            client = EmmasaApiClient(session, MOCK_USERNAME, MOCK_PASSWORD)
            with pytest.raises(exc_type):
                await client.async_authenticate()


async def test_refresh_token_rotation_and_lock_serialization() -> None:
    """Concurrent callers serialize refresh_token rotation via _auth_lock and persist rotated token."""
    mocker = AiohttpClientMocker()
    persisted: list[str] = []
    refresh_calls = 0

    async def on_tokens(bundle: TokenBundle) -> None:
        if bundle.refresh_token:
            persisted.append(bundle.refresh_token)

    async def refresh_cb(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        nonlocal refresh_calls
        refresh_calls += 1
        form = parse_qs(data)
        assert form["grant_type"][0] == "refresh_token"
        assert form["refresh_token"][0] == "rt-old"
        return _mock_resp(
            "POST",
            str(url),
            status=200,
            json={
                "access_token": make_test_jwt(),
                "refresh_token": "rt-rotated",
                "expires_in": 7200,
            },
        )

    mocker.post(f"{ACUAMA_API_BASE}/UsuariosOnline/token", side_effect=refresh_cb)
    mocker.post(f"{PORTAL_API_BASE}/auth/load", status=204)

    async with mocker.create_session(asyncio.get_running_loop()) as session:
        client = EmmasaApiClient(
            session,
            MOCK_USERNAME,
            MOCK_PASSWORD,
            refresh_token="rt-old",
            access_token=make_test_jwt(exp=time.time() - 10),
            expires_at=time.time() - 10,
            on_tokens_updated=on_tokens,
        )
        results = await asyncio.gather(
            client.async_ensure_authenticated(),
            client.async_ensure_authenticated(),
            client.async_ensure_authenticated(),
        )
        # Subsequent call hits fast-path without re-locking or extra HTTP calls
        fast_tok = await client.async_ensure_authenticated()

    assert len(set(results)) == 1
    assert fast_tok == results[0]
    assert refresh_calls == 1
    assert persisted == ["rt-rotated"]


async def test_refresh_rejected_falls_back_to_full_pkce_login_and_5xx_raises() -> None:
    """If refresh_token is rejected (400), client falls back to PKCE; 500 raises ConnectionError."""
    mocker = AiohttpClientMocker()
    state_holder: dict[str, str] = {}

    async def login_cb(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        state_holder["state"] = data["oauthState"]
        return _mock_resp(
            "POST",
            str(url),
            status=200,
            json={
                "success": True,
                "redirect_url": (
                    "https://portalciudadano.emmasa.elliotcloud.com/callback"
                    f"#/callback?code=direct-code&state={state_holder['state']}"
                ),
            },
        )

    mocker.post(
        f"{ACUAMA_API_BASE}/UsuariosOnline/token",
        side_effect=_seq_side_effect(
            [
                _mock_resp("POST", f"{ACUAMA_API_BASE}/UsuariosOnline/token", status=400, json={"error": "invalid_grant"}),
                _mock_resp(
                    "POST",
                    f"{ACUAMA_API_BASE}/UsuariosOnline/token",
                    status=200,
                    json={"access_token": make_test_jwt(), "refresh_token": "rt-fresh", "expires_in": 7200},
                ),
            ]
        ),
    )
    mocker.post(
        f"{ACUAMA_API_BASE}/UsuariosOnline/Login?explotacionCodigo=Emmasa",
        side_effect=login_cb,
    )
    mocker.post(f"{PORTAL_API_BASE}/auth/load", status=204)

    async with mocker.create_session(asyncio.get_running_loop()) as session:
        client = EmmasaApiClient(
            session,
            MOCK_USERNAME,
            MOCK_PASSWORD,
            refresh_token="rt-expired",
        )
        token = await client.async_ensure_authenticated()

    assert token
    assert client.token_bundle is not None
    assert client.token_bundle.refresh_token == "rt-fresh"

    # 500 from token refresh raises EmmasaConnectionError directly
    mocker2 = AiohttpClientMocker()
    mocker2.post(f"{ACUAMA_API_BASE}/UsuariosOnline/token", status=503, json={"error": "down"})
    async with mocker2.create_session(asyncio.get_running_loop()) as session:
        client2 = EmmasaApiClient(session, MOCK_USERNAME, MOCK_PASSWORD, refresh_token="rt-1")
        with pytest.raises(EmmasaConnectionError):
            await client2.async_ensure_authenticated()


async def test_bff_profile_contracts_alerts_and_401_retry() -> None:
    """Verify profile, contract discovery, alert parsing, 401 refresh retry, and logout."""
    mocker = AiohttpClientMocker()
    jwt_1 = make_test_jwt()
    jwt_2 = make_test_jwt()

    mocker.post(f"{PORTAL_API_BASE}/auth/load", status=204)
    mocker.get(
        f"{PORTAL_API_BASE}/auth/me",
        side_effect=_seq_side_effect(
            [
                _mock_resp("GET", f"{PORTAL_API_BASE}/auth/me", status=401, json={"error": "expired"}),
                _mock_resp(
                    "GET",
                    f"{PORTAL_API_BASE}/auth/me",
                    status=200,
                    json={"result": {"user": {"username": "Ana", "login": "12345678z", "email": "a@b.es"}}},
                ),
            ]
        ),
    )
    mocker.post(
        f"{ACUAMA_API_BASE}/UsuariosOnline/token",
        status=200,
        json={"access_token": jwt_2, "refresh_token": "rt-2", "expires_in": 7200},
    )
    mocker.post(
        f"{PORTAL_API_BASE}/contract/advanced-search?page=0&size=1000&sort=ASC&sortField=id",
        status=200,
        json={
            "content": [
                "skip-non-dict",
                {"name": "missing-id"},
                {
                    "id": MOCK_CONTRACT_UUID,
                    "name": MOCK_CONTRACT_NAME,
                    "allowsAlertConfiguration": True,
                },
            ]
        },
    )
    mocker.get(
        f"{PORTAL_API_BASE}/contract-alerts/{MOCK_CONTRACT_UUID}?page=0&size=1000&sort=ASC&sortField=id",
        status=200,
        json={
            "content": [
                "skip-non-dict",
                {
                    "alertId": "1986c39c-b7f3-4d10-bd6b-1464dd50b760",
                    "alertName": "Consumo horario diurno",
                    "alertDescription": "Desc",
                    "userAlertConfiguration": {"id": "123", "userLimit": 250.0, "isActive": True},
                    "allowsAlertConfiguration": True,
                },
            ]
        },
    )
    mocker.get(f"{PORTAL_API_BASE}/auth/logout", status=200)

    async with mocker.create_session(asyncio.get_running_loop()) as session:
        client = EmmasaApiClient(
            session,
            MOCK_USERNAME,
            MOCK_PASSWORD,
            access_token=jwt_1,
            refresh_token="rt-1",
            expires_at=time.time() + 3600,
        )
        profile = await client.async_get_profile()
        contracts = await client.async_get_contracts()
        alerts = await client.async_get_contract_alerts(MOCK_CONTRACT_UUID)
        await client.async_logout()

    assert profile.login == MOCK_USERNAME
    assert profile.username == "Ana"
    assert profile.email == "a@b.es"
    assert len(contracts) == 1
    assert contracts[0].contract_id == MOCK_CONTRACT_UUID
    assert len(alerts) == 1
    assert alerts[0].user_limit_liters == 250.0
    assert alerts[0].is_active is True


async def test_bff_error_paths_and_logout_suppression() -> None:
    """Verify BFF /auth/load errors, persistent 403/500 portal errors, and malformed payloads."""
    jwt_tok = make_test_jwt()

    # /auth/load 401 -> EmmasaAuthError; /auth/load 500 -> EmmasaConnectionError
    for load_status, exc_type in ((401, EmmasaAuthError), (500, EmmasaConnectionError)):
        mocker = AiohttpClientMocker()
        mocker.post(f"{PORTAL_API_BASE}/auth/load", status=load_status, json={"error": "bad"})
        async with mocker.create_session(asyncio.get_running_loop()) as session:
            client = EmmasaApiClient(
                session, MOCK_USERNAME, MOCK_PASSWORD, access_token=jwt_tok, expires_at=time.time() + 3600
            )
            with pytest.raises(exc_type):
                await client.async_get_profile()

    # Portal 403 -> EmmasaAuthError, 500 -> EmmasaConnectionError
    for api_status, exc_type in ((403, EmmasaAuthError), (500, EmmasaConnectionError)):
        mocker = AiohttpClientMocker()
        mocker.post(f"{PORTAL_API_BASE}/auth/load", status=204)
        mocker.get(f"{PORTAL_API_BASE}/auth/me", status=api_status, json={"error": "err"})
        async with mocker.create_session(asyncio.get_running_loop()) as session:
            client = EmmasaApiClient(
                session, MOCK_USERNAME, MOCK_PASSWORD, access_token=jwt_tok, expires_at=time.time() + 3600
            )
            with pytest.raises(exc_type):
                await client.async_get_profile()

    # Malformed /auth/me, /contract/advanced-search, /contract-alerts, and /auth/grafana
    mocker = AiohttpClientMocker()
    mocker.post(f"{PORTAL_API_BASE}/auth/load", status=204)
    mocker.get(
        f"{PORTAL_API_BASE}/auth/me",
        side_effect=_seq_side_effect(
            [
                _mock_resp("GET", f"{PORTAL_API_BASE}/auth/me", status=200, json=["bad"]),
                _mock_resp("GET", f"{PORTAL_API_BASE}/auth/me", status=200, json={"result": "not-dict"}),
            ]
        ),
    )
    mocker.post(
        f"{PORTAL_API_BASE}/contract/advanced-search?page=0&size=1000&sort=ASC&sortField=id",
        status=200,
        json={"content": "not-a-list"},
    )
    mocker.get(
        f"{PORTAL_API_BASE}/contract-alerts/{MOCK_CONTRACT_UUID}?page=0&size=1000&sort=ASC&sortField=id",
        status=200,
        json={"content": "not-a-list"},
    )
    mocker.post(
        f"{PORTAL_API_BASE}/auth/grafana",
        side_effect=_seq_side_effect(
            [
                _mock_resp("POST", f"{PORTAL_API_BASE}/auth/grafana", status=200, json={}),
                _mock_resp(
                    "POST",
                    f"{PORTAL_API_BASE}/auth/grafana",
                    status=200,
                    json={"grafana_session": "gs-fallback", "grafana_session_expiry": "non-numeric"},
                ),
            ]
        ),
    )
    mocker.get(f"{PORTAL_API_BASE}/auth/logout", exc=aiohttp.ClientConnectionError("offline"))

    async with mocker.create_session(asyncio.get_running_loop()) as session:
        # Unauthenticated logout is a clean no-op
        unauth_client = EmmasaApiClient(session, MOCK_USERNAME, MOCK_PASSWORD)
        await unauth_client.async_logout()

        client = EmmasaApiClient(
            session, MOCK_USERNAME, MOCK_PASSWORD, access_token=jwt_tok, expires_at=time.time() + 3600
        )
        with pytest.raises(EmmasaConnectionError):
            await client.async_get_profile()
        with pytest.raises(EmmasaConnectionError):
            await client.async_get_profile()
        with pytest.raises(EmmasaConnectionError):
            await client.async_get_contracts()
        assert await client.async_get_contract_alerts(MOCK_CONTRACT_UUID) == []
        with pytest.raises(EmmasaConnectionError):
            await client.async_ensure_grafana_session()
        gs = await client.async_ensure_grafana_session()
        assert gs.session_token == "gs-fallback"
        assert client.grafana_session is gs
        # Cached grafana_session returned without extra call
        assert await client.async_ensure_grafana_session() is gs
        # Best-effort logout swallows connection error
        await client.async_logout()
        assert client.grafana_session is None


async def test_fetch_contract_snapshot_batch_and_grafana_recovery() -> None:
    """Batch ds/query parses all 6 frames, handles 401 session renewal and 404 UID auto-discovery."""
    mocker = AiohttpClientMocker()
    jwt_tok = make_test_jwt()
    contract = ContractInfo(
        contract_id=MOCK_CONTRACT_UUID,
        name=MOCK_CONTRACT_NAME,
        allows_alert_configuration=True,
    )
    t0 = int(datetime(2026, 11, 25, 6, 0, tzinfo=UTC).timestamp() * 1000)
    t1 = int(datetime(2026, 11, 25, 7, 0, tzinfo=UTC).timestamp() * 1000)
    meter_ms = int(datetime(2026, 11, 25, 0, 0, tzinfo=UTC).timestamp() * 1000)
    period_ms = int(datetime(2026, 9, 16, 2, 0, tzinfo=UTC).timestamp() * 1000)
    inv_ms = int(datetime(2026, 9, 24, 22, 0, tzinfo=UTC).timestamp() * 1000)

    ds_ok = _make_grafana_ds_response(
        hourly_rows=[(t1, 0.044), (t0, 0.004)],  # intentionally unsorted to verify sort ASC
        meter_row=(meter_ms, 500.123),
        period_row=(period_ms, 13.1),
        daily_row=("2026-11-24", 177.0),
        billed_row=(202605, 11.0, "Sep-Oct 2026"),
        invoice_row=("202605", 35.36, inv_ms),
    )

    mocker.post(f"{PORTAL_API_BASE}/auth/load", status=204)
    mocker.post(
        f"{PORTAL_API_BASE}/auth/grafana",
        side_effect=_seq_side_effect(
            [
                _mock_resp(
                    "POST",
                    f"{PORTAL_API_BASE}/auth/grafana",
                    status=200,
                    json={"grafana_session": "gs-1", "grafana_session_expiry": int(time.time()) + 600},
                ),
                _mock_resp(
                    "POST",
                    f"{PORTAL_API_BASE}/auth/grafana",
                    status=200,
                    json={"grafana_session": "gs-2", "grafana_session_expiry": int(time.time()) + 600},
                ),
            ]
        ),
    )
    mocker.post(
        f"{GRAFANA_BASE}/api/ds/query",
        side_effect=_seq_side_effect(
            [
                _mock_resp("POST", f"{GRAFANA_BASE}/api/ds/query", status=401, json={"message": "Unauthorized"}),
                _mock_resp("POST", f"{GRAFANA_BASE}/api/ds/query", status=404, json={"message": "Data source not found"}),
                _mock_resp("POST", f"{GRAFANA_BASE}/api/ds/query", status=200, json=ds_ok),
            ]
        ),
    )
    mocker.get(
        f"{GRAFANA_BASE}/api/dashboards/uid/{GRAFANA_CONSUMOS_DASHBOARD_UID}",
        status=200,
        json={
            "dashboard": {
                "panels": [
                    "non-dict-panel",
                    {"id": 1, "datasource": {"type": "other", "uid": "x"}},
                    {
                        "id": 4,
                        "datasource": {"type": "grafana-postgresql-datasource", "uid": "new-pg-uid"},
                    },
                ]
            }
        },
    )
    mocker.get(
        f"{PORTAL_API_BASE}/contract-alerts/{MOCK_CONTRACT_UUID}?page=0&size=1000&sort=ASC&sortField=id",
        status=200,
        json={"content": []},
    )

    async with mocker.create_session(asyncio.get_running_loop()) as session:
        client = EmmasaApiClient(
            session,
            MOCK_USERNAME,
            MOCK_PASSWORD,
            access_token=jwt_tok,
            refresh_token="rt-1",
            expires_at=time.time() + 3600,
        )
        snapshot = await client.async_fetch_contract_snapshot(
            contract,
            datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
        )

    assert client.datasource_uid == "new-pg-uid"
    assert len(snapshot.hourly_readings) == 2
    assert snapshot.hourly_readings[0].volume_m3 == 0.004
    assert snapshot.hourly_readings[1].volume_m3 == 0.044
    assert snapshot.latest_meter_reading is not None
    assert snapshot.latest_meter_reading.volume_acc_m3 == 500.123
    assert snapshot.current_period_consumption is not None
    assert snapshot.current_period_consumption.volume_m3 == 13.1
    assert snapshot.yesterday_consumption_liters == 177.0
    assert snapshot.latest_billed_consumption is not None
    assert snapshot.latest_billed_consumption.period_label == "Sep-Oct 2026"
    assert snapshot.latest_invoice is not None
    assert snapshot.latest_invoice.amount_eur == 35.36
    assert snapshot.last_reading_timestamp_utc == datetime(2026, 11, 25, 7, 0, tzinfo=UTC)


async def test_grafana_discover_and_ds_query_error_paths() -> None:
    """Verify Grafana datasource discovery 401 retry/errors and ds/query 401/500/400 failures."""
    jwt_tok = make_test_jwt()
    contract = ContractInfo(
        contract_id=MOCK_CONTRACT_UUID,
        name=MOCK_CONTRACT_NAME,
        allows_alert_configuration=False,
    )

    # Dashboard discovery 401 retry + missing PostgreSQL UID error + HTTP 500 error
    mocker = AiohttpClientMocker()
    mocker.post(f"{PORTAL_API_BASE}/auth/load", status=204)
    mocker.post(
        f"{PORTAL_API_BASE}/auth/grafana",
        status=200,
        json={"grafana_session": "gs-1", "grafana_session_expiry": int(time.time()) + 600},
    )
    mocker.get(
        f"{GRAFANA_BASE}/api/dashboards/uid/{GRAFANA_CONSUMOS_DASHBOARD_UID}",
        side_effect=_seq_side_effect(
            [
                _mock_resp("GET", f"{GRAFANA_BASE}/api/dashboards/uid/{GRAFANA_CONSUMOS_DASHBOARD_UID}", status=401, json={}),
                _mock_resp(
                    "GET",
                    f"{GRAFANA_BASE}/api/dashboards/uid/{GRAFANA_CONSUMOS_DASHBOARD_UID}",
                    status=200,
                    json={"dashboard": {"panels": []}},
                ),
                _mock_resp("GET", f"{GRAFANA_BASE}/api/dashboards/uid/{GRAFANA_CONSUMOS_DASHBOARD_UID}", status=500, json={}),
            ]
        ),
    )
    async with mocker.create_session(asyncio.get_running_loop()) as session:
        client = EmmasaApiClient(
            session, MOCK_USERNAME, MOCK_PASSWORD, access_token=jwt_tok, expires_at=time.time() + 3600
        )
        with pytest.raises(EmmasaQueryError):
            await client.async_discover_datasource_uid()
        with pytest.raises(EmmasaQueryError):
            await client.async_discover_datasource_uid()

    # ds/query persistent 401 -> EmmasaAuthError, 502 -> EmmasaConnectionError, 400 -> EmmasaQueryError
    for statuses, exc_type in (
        ([401, 401], EmmasaAuthError),
        ([502], EmmasaConnectionError),
        ([400], EmmasaQueryError),
    ):
        mocker2 = AiohttpClientMocker()
        mocker2.post(f"{PORTAL_API_BASE}/auth/load", status=204)
        mocker2.post(
            f"{PORTAL_API_BASE}/auth/grafana",
            status=200,
            json={"grafana_session": "gs-1", "grafana_session_expiry": int(time.time()) + 600},
        )
        mocker2.post(
            f"{GRAFANA_BASE}/api/ds/query",
            side_effect=_seq_side_effect(
                [_mock_resp("POST", f"{GRAFANA_BASE}/api/ds/query", status=st, json={"message": "err"}) for st in statuses]
            ),
        )
        async with mocker2.create_session(asyncio.get_running_loop()) as session:
            client2 = EmmasaApiClient(
                session, MOCK_USERNAME, MOCK_PASSWORD, access_token=jwt_tok, expires_at=time.time() + 3600
            )
            with pytest.raises(exc_type):
                await client2.async_fetch_contract_snapshot(contract, datetime(2026, 11, 20, tzinfo=UTC))


async def test_fetch_contract_snapshot_daily_fallback_and_query_error() -> None:
    """Yesterday fallback sums Atlantic/Canary hourly rows; frame error raises EmmasaQueryError."""
    mocker = AiohttpClientMocker()
    jwt_tok = make_test_jwt()
    contract = ContractInfo(
        contract_id=MOCK_CONTRACT_UUID,
        name=MOCK_CONTRACT_NAME,
        allows_alert_configuration=False,
    )
    yesterday_h1 = (datetime.now(UTC) - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    yesterday_h2 = yesterday_h1 + timedelta(hours=1)
    ds_fallback = _make_grafana_ds_response(
        hourly_rows=[
            (int(yesterday_h1.timestamp() * 1000), 0.05),
            (int(yesterday_h2.timestamp() * 1000), 0.125),
        ],
    )

    mocker.post(f"{PORTAL_API_BASE}/auth/load", status=204)
    mocker.post(
        f"{PORTAL_API_BASE}/auth/grafana",
        status=200,
        json={"grafana_session": "gs-1", "grafana_session_expiry": int(time.time()) + 600},
    )
    mocker.post(
        f"{GRAFANA_BASE}/api/ds/query",
        side_effect=_seq_side_effect(
            [
                _mock_resp("POST", f"{GRAFANA_BASE}/api/ds/query", status=200, json=ds_fallback),
                _mock_resp("POST", f"{GRAFANA_BASE}/api/ds/query", status=200, json=_make_grafana_ds_response(error_ref="hourly")),
            ]
        ),
    )

    async with mocker.create_session(asyncio.get_running_loop()) as session:
        client = EmmasaApiClient(
            session,
            MOCK_USERNAME,
            MOCK_PASSWORD,
            access_token=jwt_tok,
            expires_at=time.time() + 3600,
        )
        snapshot = await client.async_fetch_contract_snapshot(
            contract,
            yesterday_h1 - timedelta(days=1),
        )
        assert snapshot.yesterday_consumption_liters == 175.0

        with pytest.raises(EmmasaQueryError):
            await client.async_fetch_contract_snapshot(contract, yesterday_h1)


async def test_network_error_and_non_json_responses() -> None:
    """aiohttp ClientError and non-JSON responses raise appropriate typed exceptions."""
    mocker = AiohttpClientMocker()
    mocker.post(
        f"{ACUAMA_API_BASE}/UsuariosOnline/Login?explotacionCodigo=Emmasa",
        exc=aiohttp.ClientConnectionError("dns failure"),
    )
    async with mocker.create_session(asyncio.get_running_loop()) as session:
        client = EmmasaApiClient(session, MOCK_USERNAME, MOCK_PASSWORD)
        with pytest.raises(EmmasaConnectionError):
            await client.async_authenticate()

    mocker2 = AiohttpClientMocker()
    mocker2.post(
        f"{ACUAMA_API_BASE}/UsuariosOnline/Login?explotacionCodigo=Emmasa",
        status=502,
        text="<html>Bad Gateway</html>",
    )
    async with mocker2.create_session(asyncio.get_running_loop()) as session:
        client2 = EmmasaApiClient(session, MOCK_USERNAME, MOCK_PASSWORD)
        with pytest.raises(EmmasaConnectionError):
            await client2.async_authenticate()

    mocker3 = AiohttpClientMocker()
    mocker3.post(
        f"{ACUAMA_API_BASE}/UsuariosOnline/Login?explotacionCodigo=Emmasa",
        status=401,
        text="Unauthorized",
    )
    async with mocker3.create_session(asyncio.get_running_loop()) as session:
        client3 = EmmasaApiClient(session, MOCK_USERNAME, MOCK_PASSWORD)
        with pytest.raises(EmmasaAuthError):
            await client3.async_authenticate()
