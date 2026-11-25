"""Tests for EMMASA diagnostic binary sensor entities."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from homeassistant.components.recorder import Recorder
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emmasa.api import ContractInfo, ContractSnapshot
from custom_components.emmasa.const import DOMAIN

from .conftest import MOCK_CONTRACT_UUID


async def test_binary_sensors_setup_and_states(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_legacy_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    sample_contract: ContractInfo,
    sample_contract_2: ContractInfo,
    sample_snapshot: ContractSnapshot,
    mock_recorder_stats: dict[str, Any],
) -> None:
    """Verify telelectura connectivity and alert binary sensors (including dynamic contracts)."""
    mock_legacy_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_legacy_config_entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    tele_state = hass.states.get("binary_sensor.emmasa_contrato_12345_telelectura_available")
    assert tele_state is not None
    assert tele_state.state == STATE_ON

    for key in ("daytime_alert_enabled", "nighttime_alert_enabled"):
        entity_id = ent_reg.async_get_entity_id("binary_sensor", DOMAIN, f"{MOCK_CONTRACT_UUID}_{key}")
        assert entity_id is not None
        entry = ent_reg.async_get(entity_id)
        assert entry is not None
        assert entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION
        assert entry.entity_category == EntityCategory.DIAGNOSTIC
        ent_reg.async_update_entity(entity_id, disabled_by=None)

    await hass.config_entries.async_reload(mock_legacy_config_entry.entry_id)
    await hass.async_block_till_done()

    day_alert = hass.states.get("binary_sensor.emmasa_contrato_12345_daytime_alert_enabled")
    assert day_alert is not None
    assert day_alert.state == STATE_ON

    night_alert = hass.states.get("binary_sensor.emmasa_contrato_12345_nighttime_alert_enabled")
    assert night_alert is not None
    assert night_alert.state == STATE_OFF

    # Dynamic second contract without telelectura
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

    tele_2 = hass.states.get("binary_sensor.emmasa_contrato_67890_telelectura_available")
    assert tele_2 is not None
    assert tele_2.state == STATE_OFF
