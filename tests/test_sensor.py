"""Tests for EMMASA sensor entities."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from homeassistant.components.recorder import Recorder
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emmasa.api import (
    ContractAlert,
    ContractInfo,
    ContractSnapshot,
    EmmasaConnectionError,
)
from custom_components.emmasa.const import DOMAIN
from custom_components.emmasa.sensor import _compute_lag_hours, _find_alert

from .conftest import MOCK_CONTRACT_NAME, MOCK_CONTRACT_UUID


async def test_sensors_setup_states_attributes_and_device_info(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    mock_recorder_stats: dict[str, Any],
) -> None:
    """Verify enabled/disabled sensors, values, units, state_class=None on water, and DeviceInfo."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, MOCK_CONTRACT_UUID),
        mock_config_entry.entry_id,
    )
    assert device is not None
    assert device.name == f"EMMASA Contrato {MOCK_CONTRACT_NAME}"
    assert device.manufacturer == "EMMASA (Elliot Cloud)"

    # Enabled primary & diagnostic sensors
    meter_state = hass.states.get("sensor.emmasa_contrato_12345_meter_reading")
    assert meter_state is not None
    assert meter_state.state == "412.345"
    assert meter_state.attributes["unit_of_measurement"] == UnitOfVolume.CUBIC_METERS
    assert meter_state.attributes["device_class"] == SensorDeviceClass.WATER
    assert "state_class" not in meter_state.attributes
    assert meter_state.attributes["energy_dashboard_statistic_id"] == "emmasa:water_consumption_12345"

    period_state = hass.states.get("sensor.emmasa_contrato_12345_current_period_consumption")
    assert period_state is not None
    assert period_state.state == "13.1"
    assert period_state.attributes["period_start_utc"] == "2026-09-16T02:00:00+00:00"

    yesterday_state = hass.states.get("sensor.emmasa_contrato_12345_yesterday_consumption")
    assert yesterday_state is not None
    assert yesterday_state.state == "177.0"
    assert yesterday_state.attributes["unit_of_measurement"] == UnitOfVolume.LITERS
    assert yesterday_state.attributes["reading_date"] == "2026-11-24"

    billed_state = hass.states.get("sensor.emmasa_contrato_12345_last_billed_consumption")
    assert billed_state is not None
    assert billed_state.state == "11.0"
    assert billed_state.attributes["period_label"] == "Sep-Oct 2026"

    invoice_state = hass.states.get("sensor.emmasa_contrato_12345_last_invoice_amount")
    assert invoice_state is not None
    assert invoice_state.state == "35.36"
    assert invoice_state.attributes["unit_of_measurement"] == "EUR"
    assert invoice_state.attributes["period_code"] == "202605"

    ts_state = hass.states.get("sensor.emmasa_contrato_12345_last_telelectura_reading")
    assert ts_state is not None
    assert ts_state.state == "2026-11-25T08:00:00+00:00"

    # Disabled-by-default diagnostic sensors are registered in entity registry
    for key in ("telelectura_lag", "daytime_alert_limit", "nighttime_alert_limit"):
        entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{MOCK_CONTRACT_UUID}_{key}")
        assert entity_id is not None
        entry = ent_reg.async_get(entity_id)
        assert entry is not None
        assert entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION
        assert entry.entity_category == EntityCategory.DIAGNOSTIC

    # Enable disabled-by-default diagnostic sensors and reload to verify values
    for key in ("telelectura_lag", "daytime_alert_limit", "nighttime_alert_limit"):
        entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{MOCK_CONTRACT_UUID}_{key}")
        assert entity_id is not None
        ent_reg.async_update_entity(entity_id, disabled_by=None)

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    day_limit = hass.states.get("sensor.emmasa_contrato_12345_daytime_alert_threshold")
    assert day_limit is not None
    assert day_limit.state == "500.0"
    assert day_limit.attributes["device_class"] == SensorDeviceClass.VOLUME

    night_limit = hass.states.get("sensor.emmasa_contrato_12345_nighttime_alert_threshold")
    assert night_limit is not None
    assert night_limit.state == "100.0"
    assert night_limit.attributes["device_class"] == SensorDeviceClass.VOLUME

    lag = hass.states.get("sensor.emmasa_contrato_12345_telelectura_publication_lag")
    assert lag is not None
    assert float(lag.state) >= 0.0

    # Coordinator failure makes sensors unavailable
    mock_emmasa_client.async_get_contracts.side_effect = EmmasaConnectionError("offline")
    await mock_config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    failed_state = hass.states.get("sensor.emmasa_contrato_12345_meter_reading")
    assert failed_state is not None
    assert failed_state.state == STATE_UNAVAILABLE


async def test_dynamic_contract_addition_creates_sensors(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_legacy_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    sample_contract: ContractInfo,
    sample_contract_2: ContractInfo,
    sample_snapshot: ContractSnapshot,
    mock_recorder_stats: dict[str, Any],
) -> None:
    """New contracts appearing in later coordinator updates dynamically add sensor entities."""
    mock_legacy_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_legacy_config_entry.entry_id)
    await hass.async_block_till_done()

    snapshot_2 = ContractSnapshot(
        contract=sample_contract_2,
        hourly_readings=(),
        latest_meter_reading=None,
        current_period_consumption=None,
        yesterday_consumption_liters=None,
        latest_billed_consumption=None,
        latest_invoice=None,
        alerts=(),
        last_reading_timestamp_utc=None,
    )
    mock_emmasa_client.async_get_contracts.return_value = [sample_contract, sample_contract_2]
    mock_emmasa_client.async_fetch_contract_snapshot.side_effect = [sample_snapshot, snapshot_2]

    await mock_legacy_config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("sensor.emmasa_contrato_67890_meter_reading") is not None


def test_alert_keyword_fallback_and_none_lag(sample_contract: ContractInfo) -> None:
    """Verify alert matching by keyword when UUID differs and None lag when timestamp missing."""
    custom_alert = ContractAlert(
        alert_id="other-uuid",
        alert_name="Alarma consumo diurno personalizada",
        alert_description="",
        config_id=None,
        user_limit_liters=300.0,
        is_active=True,
        allows_alert_configuration=True,
    )
    snap = ContractSnapshot(
        contract=sample_contract,
        hourly_readings=(),
        latest_meter_reading=None,
        current_period_consumption=None,
        yesterday_consumption_liters=None,
        latest_billed_consumption=None,
        latest_invoice=None,
        alerts=(custom_alert,),
        last_reading_timestamp_utc=None,
    )
    assert _find_alert(snap, "non-matching-id", "diurn") is custom_alert
    assert _find_alert(snap, "non-matching-id", "nocturn") is None
    assert _compute_lag_hours(snap) is None
