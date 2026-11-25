"""Diagnostics support for EMMASA (Portal Ciudadano)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import EmmasaConfigEntry
from .const import TO_REDACT
from .coordinator import statistic_id_for_contract


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: EmmasaConfigEntry,
) -> dict[str, Any]:
    """Return redacted diagnostic data for a config entry."""
    runtime = entry.runtime_data
    client = runtime.client
    coordinator = runtime.coordinator

    contracts_diag: list[dict[str, Any]] = []
    for contract_id, snapshot in coordinator.data.contracts.items():
        contracts_diag.append(
            {
                "contract_id": contract_id,
                "name": snapshot.contract.name,
                "allows_alert_configuration": snapshot.contract.allows_alert_configuration,
                "statistic_id": statistic_id_for_contract(snapshot.contract),
                "hourly_readings_in_window": len(snapshot.hourly_readings),
                "imported_statistics_last_sync": coordinator.data.imported_statistics_counts.get(
                    contract_id, 0
                ),
                "last_reading_timestamp_utc": (
                    snapshot.last_reading_timestamp_utc.isoformat()
                    if snapshot.last_reading_timestamp_utc is not None
                    else None
                ),
                "has_meter_reading": snapshot.latest_meter_reading is not None,
                "has_current_period": snapshot.current_period_consumption is not None,
                "has_yesterday_consumption": snapshot.yesterday_consumption_liters is not None,
                "has_latest_billed": snapshot.latest_billed_consumption is not None,
                "has_latest_invoice": snapshot.latest_invoice is not None,
                "alerts_count": len(snapshot.alerts),
            }
        )

    tokens = client.token_bundle
    grafana = client.grafana_session
    payload: dict[str, Any] = {
        "entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "client": {
            "datasource_uid": client.datasource_uid,
            "has_access_token": bool(tokens and tokens.access_token),
            "has_refresh_token": bool(tokens and tokens.refresh_token),
            "access_token_expired": tokens.is_expired() if tokens else True,
            "has_grafana_session": bool(grafana and grafana.session_token),
            "grafana_session_expired": grafana.is_expired() if grafana else True,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                int(coordinator.update_interval.total_seconds())
                if coordinator.update_interval is not None
                else None
            ),
            "last_sync_utc": coordinator.data.last_sync_utc.isoformat(),
            "contracts_count": len(contracts_diag),
            "contracts": contracts_diag,
        },
    }
    return async_redact_data(payload, TO_REDACT)
