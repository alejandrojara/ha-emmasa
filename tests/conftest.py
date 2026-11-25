"""Shared pytest fixtures for EMMASA tests."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emmasa.api import (
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
from custom_components.emmasa.const import (
    ALERT_DAYTIME_ID,
    ALERT_NIGHTTIME_ID,
    CONF_CONTRACT_ID,
    CONF_CONTRACT_NAME,
    CONF_REFRESH_TOKEN,
    DEFAULT_GRAFANA_DATASOURCE_UID,
    DOMAIN,
)

MOCK_USERNAME = "12345678Z"
MOCK_PASSWORD = "super-secret-password"
MOCK_CONTRACT_UUID = "11111111-2222-4333-8444-555555555555"
MOCK_CONTRACT_UUID_2 = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
MOCK_CONTRACT_NAME = "12345"
MOCK_CONTRACT_NAME_2 = "67890"


def make_test_jwt(exp: float | None = None) -> str:
    """Build a structurally valid unsigned JWT for unit tests."""
    now = int(time.time())
    payload = {
        "iss": "https://acuamaemmasa.sacyr.com/webApi/",
        "aud": "F17573F7-2BF9-4A27-A011-3FA25F12CB77",
        "iat": now,
        "exp": int(exp if exp is not None else now + 7200),
        "sub": MOCK_USERNAME,
    }
    header_b64 = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header_b64}.{payload_b64}.signature"


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mock per-contract EMMASA ConfigEntry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"EMMASA ({MOCK_CONTRACT_NAME})",
        unique_id=MOCK_CONTRACT_UUID,
        data={
            CONF_USERNAME: MOCK_USERNAME,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_CONTRACT_ID: MOCK_CONTRACT_UUID,
            CONF_CONTRACT_NAME: MOCK_CONTRACT_NAME,
            CONF_REFRESH_TOKEN: "initial-refresh-token",
        },
        options={},
    )


@pytest.fixture
def mock_legacy_config_entry() -> MockConfigEntry:
    """Return a legacy account-scoped ConfigEntry without CONF_CONTRACT_ID."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"EMMASA ({MOCK_USERNAME})",
        unique_id=MOCK_USERNAME,
        data={
            CONF_USERNAME: MOCK_USERNAME,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_REFRESH_TOKEN: "initial-refresh-token",
        },
        options={},
    )


@pytest.fixture
def sample_contract() -> ContractInfo:
    """Return a sample water contract."""
    return ContractInfo(
        contract_id=MOCK_CONTRACT_UUID,
        name=MOCK_CONTRACT_NAME,
        allows_alert_configuration=True,
    )


@pytest.fixture
def sample_contract_2() -> ContractInfo:
    """Return a second sample water contract."""
    return ContractInfo(
        contract_id=MOCK_CONTRACT_UUID_2,
        name=MOCK_CONTRACT_NAME_2,
        allows_alert_configuration=False,
    )


@pytest.fixture
def sample_snapshot(sample_contract: ContractInfo) -> ContractSnapshot:
    """Return a rich ContractSnapshot matching real EMMASA structure."""
    base_ts = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
    return ContractSnapshot(
        contract=sample_contract,
        hourly_readings=(
            HourlyReading(timestamp_utc=base_ts, volume_m3=0.004),
            HourlyReading(timestamp_utc=base_ts + timedelta(hours=1), volume_m3=0.044),
            HourlyReading(timestamp_utc=base_ts + timedelta(hours=2), volume_m3=0.007),
        ),
        latest_meter_reading=MeterReading(
            timestamp_utc=datetime(2026, 11, 25, 0, 0, tzinfo=UTC),
            volume_acc_m3=412.345,
        ),
        current_period_consumption=PeriodConsumption(
            since_utc=datetime(2026, 9, 16, 2, 0, tzinfo=UTC),
            volume_m3=13.1,
        ),
        yesterday_consumption_liters=177.0,
        latest_billed_consumption=BilledConsumption(
            period_code=202605,
            period_label="Sep-Oct 2026",
            volume_m3=11.0,
        ),
        latest_invoice=InvoiceSummary(
            period_code="202605",
            amount_eur=35.36,
            invoice_date_utc=datetime(2026, 9, 24, 22, 0, tzinfo=UTC),
        ),
        alerts=(
            ContractAlert(
                alert_id=ALERT_DAYTIME_ID,
                alert_name="Consumo horario diurno",
                alert_description="Alerta diurna",
                config_id="cfg-day",
                user_limit_liters=500.0,
                is_active=True,
                allows_alert_configuration=True,
            ),
            ContractAlert(
                alert_id=ALERT_NIGHTTIME_ID,
                alert_name="Consumo horario nocturno",
                alert_description="Alerta nocturna",
                config_id="cfg-night",
                user_limit_liters=100.0,
                is_active=False,
                allows_alert_configuration=True,
            ),
        ),
        last_reading_timestamp_utc=base_ts + timedelta(hours=2),
        yesterday_consumption_date="2026-11-24",
    )


@pytest.fixture
def mock_recorder_stats() -> Generator[dict[str, Any]]:
    """Patch Home Assistant Recorder statistics helpers for deterministic coordinator tests."""
    recorder_instance = MagicMock()
    rows_store: dict[str, dict[float, dict[str, Any]]] = {}

    async def _fake_executor_job(func: Any, *args: Any) -> dict[str, list[dict[str, Any]]]:
        func_name = getattr(func, "__name__", "")
        if func_name == "statistics_during_period":
            statistic_ids = args[3]
            result: dict[str, list[dict[str, Any]]] = {}
            for stat_id in statistic_ids:
                if stat_rows := rows_store.get(stat_id):
                    result[stat_id] = [stat_rows[ts] for ts in sorted(stat_rows)]
            return result
        statistic_id = args[2]
        if stat_rows := rows_store.get(statistic_id):
            latest_ts = max(stat_rows)
            return {statistic_id: [stat_rows[latest_ts]]}
        return {}

    recorder_instance.async_add_executor_job = AsyncMock(side_effect=_fake_executor_job)
    added_calls: list[tuple[Any, list[Any]]] = []

    def _fake_add_external_stats(hass: Any, metadata: Any, statistics: Any) -> None:
        stats_list = list(statistics)
        added_calls.append((metadata, stats_list))
        stat_rows = rows_store.setdefault(metadata["statistic_id"], {})
        for item in stats_list:
            ts = item["start"].timestamp()
            row: dict[str, Any] = {"start": ts, "sum": item["sum"]}
            if "state" in item:
                row["state"] = item["state"]
            stat_rows[ts] = row

    with (
        patch("custom_components.emmasa.coordinator.get_instance", return_value=recorder_instance),
        patch(
            "custom_components.emmasa.coordinator.async_add_external_statistics",
            side_effect=_fake_add_external_stats,
        ),
    ):
        yield {
            "recorder": recorder_instance,
            "rows": rows_store,
            "added_calls": added_calls,
        }


@pytest.fixture
def mock_emmasa_client(
    enable_custom_integrations: None,
    sample_contract: ContractInfo,
    sample_snapshot: ContractSnapshot,
) -> Generator[MagicMock]:
    """Patch EmmasaApiClient in __init__ and config_flow for integration tests."""
    client = MagicMock()
    client.datasource_uid = DEFAULT_GRAFANA_DATASOURCE_UID
    client.token_bundle = TokenBundle(
        access_token=make_test_jwt(),
        refresh_token="rotated-refresh-token",
        expires_at=time.time() + 7200,
    )
    client.grafana_session = GrafanaSession(
        session_token="mock-grafana-cookie",
        expires_at=time.time() + 600,
    )
    client.async_authenticate = AsyncMock(return_value=client.token_bundle)
    client.async_ensure_authenticated = AsyncMock(return_value=client.token_bundle.access_token)
    client.async_get_profile = AsyncMock(
        return_value=UserProfile(
            username="Test User",
            login=MOCK_USERNAME,
            email="test@example.com",
        )
    )
    client.async_get_contracts = AsyncMock(return_value=[sample_contract])
    client.async_fetch_contract_snapshot = AsyncMock(return_value=sample_snapshot)
    client.async_logout = AsyncMock(return_value=None)

    with (
        patch("custom_components.emmasa.EmmasaApiClient", return_value=client) as init_cls,
        patch("custom_components.emmasa.config_flow.EmmasaApiClient", return_value=client),
    ):
        client._init_cls = init_cls
        yield client
