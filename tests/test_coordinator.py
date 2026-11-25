"""Tests for EmmasaDataUpdateCoordinator and Recorder external statistics sync."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    statistics_during_period,
)
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
from custom_components.emmasa.const import (
    CONF_BACKFILL_DAYS,
    CONF_SCAN_INTERVAL_HOURS,
    DOMAIN,
)
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
    assert coordinator.update_interval == timedelta(hours=8)

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


async def test_coordinator_retroactive_backfill_on_options_increase(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    sample_contract: ContractInfo,
    sample_snapshot: ContractSnapshot,
    mock_recorder_stats: dict[str, Any],
) -> None:
    """Increasing backfill_days retroactively prepends older hours and shifts existing sums."""
    mock_config_entry.add_to_hass(hass)

    # 1. Initial sync with default 30 days imports T0..T2 (sums: 0.004, 0.048, 0.055)
    coord_initial = EmmasaDataUpdateCoordinator(hass, mock_config_entry, mock_emmasa_client)
    await coord_initial._async_update_data()
    assert len(mock_recorder_stats["added_calls"]) == 1

    # 2. Simulate OptionsFlowWithReload: user increases backfill_days to 90 -> new coordinator
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={CONF_SCAN_INTERVAL_HOURS: 8, CONF_BACKFILL_DAYS: 90},
    )
    first_ts = sample_snapshot.hourly_readings[0].timestamp_utc
    last_ts = sample_snapshot.hourly_readings[-1].timestamp_utc
    older_1 = HourlyReading(timestamp_utc=first_ts - timedelta(days=45, hours=2), volume_m3=0.100)
    older_2 = HourlyReading(timestamp_utc=first_ts - timedelta(days=45, hours=1), volume_m3=0.250)
    newer_1 = HourlyReading(timestamp_utc=last_ts + timedelta(hours=1), volume_m3=0.015)

    expanded_snapshot = ContractSnapshot(
        contract=sample_contract,
        hourly_readings=(
            older_1,
            older_2,
            *sample_snapshot.hourly_readings,
            newer_1,
        ),
        latest_meter_reading=sample_snapshot.latest_meter_reading,
        current_period_consumption=sample_snapshot.current_period_consumption,
        yesterday_consumption_liters=sample_snapshot.yesterday_consumption_liters,
        latest_billed_consumption=sample_snapshot.latest_billed_consumption,
        latest_invoice=sample_snapshot.latest_invoice,
        alerts=sample_snapshot.alerts,
        last_reading_timestamp_utc=newer_1.timestamp_utc,
    )
    mock_emmasa_client.async_fetch_contract_snapshot.return_value = expanded_snapshot

    coord_reloaded = EmmasaDataUpdateCoordinator(hass, mock_config_entry, mock_emmasa_client)
    data_expanded = await coord_reloaded._async_update_data()

    # Verify EMMASA was queried back ~90 days (much earlier than the 48h incremental window)
    since_arg: datetime = mock_emmasa_client.async_fetch_contract_snapshot.await_args.args[1]
    assert (datetime.now(UTC) - since_arg) >= timedelta(days=89)

    # 2 older + 1 newer = 3 newly imported hours; 3 existing hours shifted by +0.350
    assert data_expanded.imported_statistics_counts[sample_contract.contract_id] == 3
    _, batch_stats = mock_recorder_stats["added_calls"][1]
    assert [s["sum"] for s in batch_stats] == [0.100, 0.350, 0.354, 0.398, 0.405, 0.420]
    assert [s["state"] for s in batch_stats] == [0.100, 0.250, 0.004, 0.044, 0.007, 0.015]

    # 3. Subsequent periodic poll on same coordinator uses incremental 48h window, not 90d
    await coord_reloaded._async_update_data()
    since_next: datetime = mock_emmasa_client.async_fetch_contract_snapshot.await_args.args[1]
    assert (datetime.now(UTC) - since_next) <= timedelta(days=3)

    # 4. Reload when backfill_days is already covered (e.g. lowered to 30) skips backward query
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={CONF_SCAN_INTERVAL_HOURS: 8, CONF_BACKFILL_DAYS: 30},
    )
    coord_covered = EmmasaDataUpdateCoordinator(hass, mock_config_entry, mock_emmasa_client)
    data_covered = await coord_covered._async_update_data()
    assert data_covered.imported_statistics_counts[sample_contract.contract_id] == 0
    since_covered: datetime = mock_emmasa_client.async_fetch_contract_snapshot.await_args.args[1]
    assert (datetime.now(UTC) - since_covered) <= timedelta(days=3)

    # 5. Zero-volume older hours prepend cleanly without rewriting existing rows
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={CONF_SCAN_INTERVAL_HOURS: 8, CONF_BACKFILL_DAYS: 120},
    )
    zero_older = HourlyReading(timestamp_utc=older_1.timestamp_utc - timedelta(days=20), volume_m3=0.0)
    mock_emmasa_client.async_fetch_contract_snapshot.return_value = ContractSnapshot(
        contract=sample_contract,
        hourly_readings=(zero_older, *expanded_snapshot.hourly_readings),
        latest_meter_reading=sample_snapshot.latest_meter_reading,
        current_period_consumption=sample_snapshot.current_period_consumption,
        yesterday_consumption_liters=sample_snapshot.yesterday_consumption_liters,
        latest_billed_consumption=sample_snapshot.latest_billed_consumption,
        latest_invoice=sample_snapshot.latest_invoice,
        alerts=sample_snapshot.alerts,
        last_reading_timestamp_utc=newer_1.timestamp_utc,
    )
    coord_zero = EmmasaDataUpdateCoordinator(hass, mock_config_entry, mock_emmasa_client)
    data_zero = await coord_zero._async_update_data()
    assert data_zero.imported_statistics_counts[sample_contract.contract_id] == 1
    _, zero_batch = mock_recorder_stats["added_calls"][2]
    assert len(zero_batch) == 1
    assert zero_batch[0]["start"] == zero_older.timestamp_utc
    assert zero_batch[0]["sum"] == 0.0


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


async def test_coordinator_real_recorder_retroactive_backfill_e2e(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_emmasa_client: MagicMock,
    sample_contract: ContractInfo,
    sample_snapshot: ContractSnapshot,
) -> None:
    """Verify real SQLite Recorder preserves exact hourly changes after retroactive backfill."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    # Expand history via options reload with older + existing + newer hours
    first_ts = sample_snapshot.hourly_readings[0].timestamp_utc
    last_ts = sample_snapshot.hourly_readings[-1].timestamp_utc
    older_1 = HourlyReading(timestamp_utc=first_ts - timedelta(days=50, hours=2), volume_m3=0.120)
    older_2 = HourlyReading(timestamp_utc=first_ts - timedelta(days=50, hours=1), volume_m3=0.080)
    newer_1 = HourlyReading(timestamp_utc=last_ts + timedelta(hours=1), volume_m3=0.025)
    mock_emmasa_client.async_fetch_contract_snapshot.return_value = ContractSnapshot(
        contract=sample_contract,
        hourly_readings=(older_1, older_2, *sample_snapshot.hourly_readings, newer_1),
        latest_meter_reading=sample_snapshot.latest_meter_reading,
        current_period_consumption=sample_snapshot.current_period_consumption,
        yesterday_consumption_liters=sample_snapshot.yesterday_consumption_liters,
        latest_billed_consumption=sample_snapshot.latest_billed_consumption,
        latest_invoice=sample_snapshot.latest_invoice,
        alerts=sample_snapshot.alerts,
        last_reading_timestamp_utc=newer_1.timestamp_utc,
    )

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_SCAN_INTERVAL_HOURS: 8, CONF_BACKFILL_DAYS: 90},
    )
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    stat_id = statistic_id_for_contract(sample_contract)
    period_stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime.fromtimestamp(0, tz=UTC),
        None,
        {stat_id},
        "hour",
        None,
        {"state", "sum", "change"},
    )
    rows = period_stats[stat_id]
    assert len(rows) == 6
    expected_vols = [0.120, 0.080, 0.004, 0.044, 0.007, 0.025]
    expected_sums = [0.120, 0.200, 0.204, 0.248, 0.255, 0.280]
    assert [r["state"] for r in rows] == pytest.approx(expected_vols)
    assert [r["sum"] for r in rows] == pytest.approx(expected_sums)
    assert [r["change"] for r in rows] == pytest.approx(expected_vols)


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
