"""Tests for EmmasaDataUpdateCoordinator and Recorder external statistics sync."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.emmasa.api import (
    ContractInfo,
    ContractSnapshot,
    EmmasaAuthError,
    EmmasaConnectionError,
    EmmasaMfaRequiredError,
    EmmasaQueryError,
    HourlyReading,
)
from custom_components.emmasa.const import DOMAIN
from custom_components.emmasa.coordinator import (
    EmmasaDataUpdateCoordinator,
    statistic_id_for_contract,
)


async def test_coordinator_backfill_incremental_and_idempotent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    sample_contract: ContractInfo,
    sample_snapshot: ContractSnapshot,
    mock_recorder_stats: dict[str, Any],
) -> None:
    """Coordinator performs initial backfill, incremental monotonic sums, and idempotent no-op."""
    mock_config_entry.add_to_hass(hass)
    coordinator = EmmasaDataUpdateCoordinator(hass, mock_config_entry, mock_emmasa_client)

    # 1. Initial backfill (3 hourly points: 0.004, 0.044, 0.007)
    data1 = await coordinator._async_update_data()
    assert data1.imported_statistics_counts[sample_contract.contract_id] == 3
    assert len(mock_recorder_stats["added_calls"]) == 1

    meta, stats = mock_recorder_stats["added_calls"][0]
    assert meta["source"] == DOMAIN
    assert meta["statistic_id"] == statistic_id_for_contract(sample_contract)
    assert meta["mean_type"] == StatisticMeanType.NONE
    assert meta["has_sum"] is True
    assert meta["unit_class"] == "volume"
    assert meta["unit_of_measurement"] == UnitOfVolume.CUBIC_METERS
    assert [s["sum"] for s in stats] == [0.004, 0.048, 0.055]

    # 2. Idempotent re-sync with same readings -> zero new imports
    data2 = await coordinator._async_update_data()
    assert data2.imported_statistics_counts[sample_contract.contract_id] == 0
    assert len(mock_recorder_stats["added_calls"]) == 1

    # 3. Incremental sync with one newer completed hour + one incomplete current hour
    newer_ts = sample_snapshot.hourly_readings[-1].timestamp_utc + timedelta(hours=1)
    incomplete_current_hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    updated_snapshot = ContractSnapshot(
        contract=sample_contract,
        hourly_readings=(
            *sample_snapshot.hourly_readings,
            HourlyReading(timestamp_utc=newer_ts, volume_m3=0.015),
            HourlyReading(timestamp_utc=incomplete_current_hour, volume_m3=0.999),
        ),
        latest_meter_reading=sample_snapshot.latest_meter_reading,
        current_period_consumption=sample_snapshot.current_period_consumption,
        yesterday_consumption_liters=sample_snapshot.yesterday_consumption_liters,
        latest_billed_consumption=sample_snapshot.latest_billed_consumption,
        latest_invoice=sample_snapshot.latest_invoice,
        alerts=sample_snapshot.alerts,
        last_reading_timestamp_utc=newer_ts,
    )
    mock_emmasa_client.async_fetch_contract_snapshot.return_value = updated_snapshot

    data3 = await coordinator._async_update_data()
    assert data3.imported_statistics_counts[sample_contract.contract_id] == 1
    assert len(mock_recorder_stats["added_calls"]) == 2
    _, inc_stats = mock_recorder_stats["added_calls"][1]
    assert len(inc_stats) == 1
    assert inc_stats[0]["start"] == newer_ts
    assert inc_stats[0]["state"] == 0.015
    assert inc_stats[0]["sum"] == 0.07


async def test_coordinator_real_recorder_external_statistics_e2e(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    sample_contract: ContractInfo,
) -> None:
    """Verify real Home Assistant Recorder accepts and persists EMMASA external statistics."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    stat_id = statistic_id_for_contract(sample_contract)
    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        stat_id,
        True,
        {"sum", "state"},
    )
    assert stat_id in last_stats
    assert last_stats[stat_id][0]["sum"] == pytest.approx(0.055)
    assert last_stats[stat_id][0]["state"] == pytest.approx(0.007)


async def test_coordinator_filters_selected_contract_and_handles_missing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    sample_contract: ContractInfo,
    sample_contract_2: ContractInfo,
    sample_snapshot: ContractSnapshot,
    mock_recorder_stats: dict[str, Any],
) -> None:
    """Coordinator polls only the configured CONF_CONTRACT_ID and raises UpdateFailed if missing."""
    mock_config_entry.add_to_hass(hass)
    mock_emmasa_client.async_get_contracts.return_value = [sample_contract, sample_contract_2]
    coordinator = EmmasaDataUpdateCoordinator(hass, mock_config_entry, mock_emmasa_client)

    data = await coordinator._async_update_data()
    assert list(data.contracts.keys()) == [sample_contract.contract_id]
    assert mock_emmasa_client.async_fetch_contract_snapshot.await_count == 1
    assert mock_emmasa_client.async_fetch_contract_snapshot.await_args.args[0] == sample_contract

    # Configured contract no longer returned by EMMASA -> UpdateFailed
    mock_emmasa_client.async_get_contracts.return_value = [sample_contract_2]
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_removes_stale_devices(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_legacy_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    sample_contract: ContractInfo,
    sample_contract_2: ContractInfo,
    sample_snapshot: ContractSnapshot,
    mock_recorder_stats: dict[str, Any],
) -> None:
    """Stale devices persisted across restarts and contracts removed later are both cleaned up."""
    mock_legacy_config_entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    # Pre-register a stale device from a previous HA run
    stale_pre_id = "99999999-8888-4777-8666-555555555555"
    dev_reg.async_get_or_create(
        config_entry_id=mock_legacy_config_entry.entry_id,
        identifiers={(DOMAIN, stale_pre_id)},
        name="EMMASA Contrato Old",
    )

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

    assert await hass.config_entries.async_setup(mock_legacy_config_entry.entry_id)
    await hass.async_block_till_done()

    # Pre-existing stale device was removed on startup refresh
    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, stale_pre_id),
            mock_legacy_config_entry.entry_id,
        )
        is None
    )
    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, sample_contract_2.contract_id),
            mock_legacy_config_entry.entry_id,
        )
        is not None
    )

    # Second contract disappears on a subsequent refresh
    mock_emmasa_client.async_get_contracts.return_value = [sample_contract]
    mock_emmasa_client.async_fetch_contract_snapshot.side_effect = None
    mock_emmasa_client.async_fetch_contract_snapshot.return_value = sample_snapshot

    await mock_legacy_config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, sample_contract_2.contract_id),
            mock_legacy_config_entry.entry_id,
        )
        is None
    )


@pytest.mark.parametrize(
    ("api_error", "expected_ha_error"),
    [
        (EmmasaAuthError("unauthorized"), ConfigEntryAuthFailed),
        (EmmasaMfaRequiredError("mfa"), ConfigEntryAuthFailed),
        (EmmasaConnectionError("timeout"), UpdateFailed),
        (EmmasaQueryError("bad sql"), UpdateFailed),
    ],
)
async def test_coordinator_error_translation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    api_error: Exception,
    expected_ha_error: type[Exception],
) -> None:
    """Coordinator translates auth and network/query exceptions appropriately."""
    mock_config_entry.add_to_hass(hass)
    mock_emmasa_client.async_get_contracts.side_effect = api_error
    coordinator = EmmasaDataUpdateCoordinator(hass, mock_config_entry, mock_emmasa_client)

    with pytest.raises(expected_ha_error):
        await coordinator._async_update_data()


def test_statistic_id_fallback_for_non_slug_name() -> None:
    """Fallback to contract UUID slug when contract name has no slugifiable chars."""
    c = ContractInfo(
        contract_id="11111111-2222-4333-8444-555555555555",
        name="---",
        allows_alert_configuration=True,
    )
    assert statistic_id_for_contract(c) == "emmasa:water_consumption_11111111_2222_4333_8444_555555555555"
    assert datetime(2026, 11, 25, tzinfo=UTC).tzinfo is UTC
