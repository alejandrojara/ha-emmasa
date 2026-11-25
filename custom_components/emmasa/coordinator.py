"""DataUpdateCoordinator and Recorder external statistics sync for EMMASA."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import slugify
from homeassistant.util.unit_conversion import VolumeConverter

from .api import (
    ContractInfo,
    ContractSnapshot,
    EmmasaApiClient,
    EmmasaAuthError,
    EmmasaConnectionError,
    EmmasaMfaRequiredError,
    EmmasaPasswordChangeRequiredError,
    EmmasaQueryError,
)
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_CONTRACT_ID,
    CONF_SCAN_INTERVAL_HOURS,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    MAX_BACKFILL_DAYS,
)

_LOGGER = logging.getLogger(__name__)
_MIN_HOURLY_LOOKBACK_HOURS = 48


def statistic_id_for_contract(contract: ContractInfo) -> str:
    """Return a stable external Recorder statistic_id for a contract."""
    slug = slugify(contract.name)
    if not slug or slug == "unknown":
        slug = slugify(contract.contract_id)
    return f"{DOMAIN}:water_consumption_{slug}"


@dataclass(slots=True, frozen=True)
class EmmasaCoordinatorData:
    """Immutable snapshot returned by EmmasaDataUpdateCoordinator."""

    contracts: dict[str, ContractSnapshot]
    imported_statistics_counts: dict[str, int]
    last_sync_utc: datetime


class EmmasaDataUpdateCoordinator(DataUpdateCoordinator[EmmasaCoordinatorData]):
    """Coordinator managing polling and historical water statistics import for EMMASA."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: EmmasaApiClient,
    ) -> None:
        scan_hours = int(
            config_entry.options.get(CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(hours=scan_hours),
        )
        self.client = client
        self._backfill_days = int(
            config_entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
        )
        raw_contract_id = config_entry.data.get(CONF_CONTRACT_ID)
        self._selected_contract_id: str | None = (
            str(raw_contract_id) if raw_contract_id else None
        )
        self._backfill_checked: set[str] = set()

    async def _async_get_last_statistic(
        self,
        statistic_id: str,
    ) -> tuple[datetime | None, float]:
        """Return (last_start_utc, last_sum) from Recorder for an external statistic."""
        recorder = get_instance(self.hass)
        last_stats = await recorder.async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            True,
            {"sum"},
        )
        rows = last_stats.get(statistic_id)
        if not rows:
            return None, 0.0
        row = rows[0]
        raw_start = row["start"]
        last_start_utc = datetime.fromtimestamp(float(raw_start), tz=UTC)
        last_sum = float(row.get("sum") or 0.0)
        return last_start_utc, last_sum

    async def _async_get_existing_statistics(
        self,
        statistic_id: str,
    ) -> list[StatisticsRow]:
        """Return all stored hourly statistics rows for statistic_id in m³."""
        recorder = get_instance(self.hass)
        stats_by_id = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime.fromtimestamp(0, tz=UTC),
            None,
            {statistic_id},
            "hour",
            {VolumeConverter.UNIT_CLASS: UnitOfVolume.CUBIC_METERS},
            {"state", "sum"},
        )
        return list(stats_by_id.get(statistic_id, ()))

    async def _async_sync_external_statistics(
        self,
        snapshot: ContractSnapshot,
        last_start_utc: datetime | None,
        running_sum: float,
        current_hour_utc: datetime,
        *,
        existing_rows: list[StatisticsRow] | None = None,
    ) -> int:
        """Import completed hourly readings into HA external statistics.

        Supports both incremental forward sync and retroactive backward backfill
        (prepending older readings before the earliest stored statistic while
        atomically shifting existing cumulative sums forward so hourly deltas
        remain exact with no negative spikes).
        """
        completed_readings = [
            reading
            for reading in snapshot.hourly_readings
            if reading.timestamp_utc < current_hour_utc
        ]
        if not completed_readings:
            return 0

        first_start_utc: datetime | None = (
            datetime.fromtimestamp(float(existing_rows[0]["start"]), tz=UTC)
            if existing_rows
            else None
        )
        older_readings = (
            [r for r in completed_readings if r.timestamp_utc < first_start_utc]
            if first_start_utc is not None
            else []
        )
        newer_readings = [
            r
            for r in completed_readings
            if last_start_utc is None or r.timestamp_utc > last_start_utc
        ]
        if not older_readings and not newer_readings:
            return 0

        statistic_id = statistic_id_for_contract(snapshot.contract)
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"EMMASA {snapshot.contract.name} Consumo de agua",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )

        stats: list[StatisticData] = []
        offset = 0.0
        shifted_existing_count = 0

        if older_readings and existing_rows:
            older_sum = 0.0
            for reading in older_readings:
                older_sum = round(older_sum + reading.volume_m3, 6)
                stats.append(
                    StatisticData(
                        start=reading.timestamp_utc,
                        state=reading.volume_m3,
                        sum=older_sum,
                    )
                )
            offset = older_sum
            if offset != 0.0:
                for row in existing_rows:
                    row_start = datetime.fromtimestamp(float(row["start"]), tz=UTC)
                    shifted_sum = round(float(row.get("sum") or 0.0) + offset, 6)
                    shifted_row = StatisticData(start=row_start, sum=shifted_sum)
                    row_state = row.get("state")
                    if row_state is not None:
                        shifted_row["state"] = float(row_state)
                    stats.append(shifted_row)
                shifted_existing_count = len(existing_rows)

        current_sum = round(running_sum + offset, 6)
        for reading in newer_readings:
            current_sum = round(current_sum + reading.volume_m3, 6)
            stats.append(
                StatisticData(
                    start=reading.timestamp_utc,
                    state=reading.volume_m3,
                    sum=current_sum,
                )
            )

        async_add_external_statistics(self.hass, metadata, stats)
        imported_count = len(older_readings) + len(newer_readings)
        _LOGGER.debug(
            "Imported %d hourly water statistics for contract %s "
            "(older=%d, newer=%d, shifted_existing=%d, statistic_id=%s)",
            imported_count,
            snapshot.contract.name,
            len(older_readings),
            len(newer_readings),
            shifted_existing_count,
            statistic_id,
        )
        return imported_count

    def _remove_stale_devices(self, active_contract_ids: set[str]) -> None:
        """Remove persisted devices for contracts no longer associated with the account."""
        if not active_contract_ids:
            return
        dev_reg = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(dev_reg, self.config_entry.entry_id):
            if not any(
                domain == DOMAIN and identifier in active_contract_ids
                for domain, identifier in device.identifiers
            ):
                dev_reg.async_update_device(
                    device_id=device.id,
                    remove_config_entry_id=self.config_entry.entry_id,
                )

    async def _async_update_data(self) -> EmmasaCoordinatorData:
        """Fetch contracts, telemetry, billing, alerts, and update external statistics."""
        now_utc = datetime.now(UTC)
        current_hour_utc = now_utc.replace(minute=0, second=0, microsecond=0)
        max_lookback_utc = current_hour_utc - timedelta(days=MAX_BACKFILL_DAYS)
        try:
            contracts = await self.client.async_get_contracts()
            if self._selected_contract_id is not None:
                contracts = [
                    contract
                    for contract in contracts
                    if contract.contract_id == self._selected_contract_id
                ]
                if not contracts:
                    raise UpdateFailed(
                        translation_domain=DOMAIN,
                        translation_key="contract_not_found",
                    )

            snapshots: dict[str, ContractSnapshot] = {}
            imported_counts: dict[str, int] = {}

            for contract in contracts:
                stat_id = statistic_id_for_contract(contract)
                existing_rows: list[StatisticsRow] | None = None
                if contract.contract_id not in self._backfill_checked:
                    existing_rows = await self._async_get_existing_statistics(stat_id)
                    if existing_rows:
                        first_start_utc: datetime | None = datetime.fromtimestamp(
                            float(existing_rows[0]["start"]),
                            tz=UTC,
                        )
                        last_start_utc: datetime | None = datetime.fromtimestamp(
                            float(existing_rows[-1]["start"]),
                            tz=UTC,
                        )
                        last_sum = float(existing_rows[-1].get("sum") or 0.0)
                    else:
                        first_start_utc = None
                        last_start_utc = None
                        last_sum = 0.0
                else:
                    first_start_utc = None
                    last_start_utc, last_sum = await self._async_get_last_statistic(stat_id)

                target_backfill_start_utc = current_hour_utc - timedelta(days=self._backfill_days)
                if last_start_utc is None:
                    since_utc = target_backfill_start_utc
                else:
                    min_window = current_hour_utc - timedelta(hours=_MIN_HOURLY_LOOKBACK_HOURS)
                    incremental_since = max(
                        min(last_start_utc + timedelta(hours=1), min_window),
                        max_lookback_utc,
                    )
                    if first_start_utc is not None and target_backfill_start_utc < first_start_utc:
                        since_utc = max(
                            min(target_backfill_start_utc, incremental_since),
                            max_lookback_utc,
                        )
                    else:
                        since_utc = incremental_since
                        existing_rows = None

                snapshot = await self.client.async_fetch_contract_snapshot(
                    contract,
                    since_utc,
                    include_alerts=True,
                )
                snapshots[contract.contract_id] = snapshot
                imported_counts[contract.contract_id] = await self._async_sync_external_statistics(
                    snapshot,
                    last_start_utc,
                    last_sum,
                    current_hour_utc,
                    existing_rows=existing_rows,
                )
                self._backfill_checked.add(contract.contract_id)

            self._remove_stale_devices(set(snapshots))

            return EmmasaCoordinatorData(
                contracts=snapshots,
                imported_statistics_counts=imported_counts,
                last_sync_utc=now_utc,
            )
        except (
            EmmasaMfaRequiredError,
            EmmasaPasswordChangeRequiredError,
            EmmasaAuthError,
        ) as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
            ) from err
        except (EmmasaConnectionError, EmmasaQueryError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
