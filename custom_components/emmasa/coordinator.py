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
    async_add_external_statistics,
    get_last_statistics,
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

    async def _async_sync_external_statistics(
        self,
        snapshot: ContractSnapshot,
        last_start_utc: datetime | None,
        running_sum: float,
        current_hour_utc: datetime,
    ) -> int:
        """Import strictly newer completed hourly readings into HA external statistics."""
        new_readings = [
            reading
            for reading in snapshot.hourly_readings
            if reading.timestamp_utc < current_hour_utc
            and (last_start_utc is None or reading.timestamp_utc > last_start_utc)
        ]
        if not new_readings:
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
        current_sum = running_sum
        for reading in new_readings:
            current_sum = round(current_sum + reading.volume_m3, 6)
            stats.append(
                StatisticData(
                    start=reading.timestamp_utc,
                    state=reading.volume_m3,
                    sum=current_sum,
                )
            )

        async_add_external_statistics(self.hass, metadata, stats)
        _LOGGER.debug(
            "Imported %d hourly water statistics for contract %s (statistic_id=%s)",
            len(stats),
            snapshot.contract.name,
            statistic_id,
        )
        return len(stats)

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
            snapshots: dict[str, ContractSnapshot] = {}
            imported_counts: dict[str, int] = {}

            for contract in contracts:
                stat_id = statistic_id_for_contract(contract)
                last_start_utc, last_sum = await self._async_get_last_statistic(stat_id)
                if last_start_utc is None:
                    since_utc = current_hour_utc - timedelta(days=self._backfill_days)
                else:
                    min_window = current_hour_utc - timedelta(hours=_MIN_HOURLY_LOOKBACK_HOURS)
                    since_utc = max(
                        min(last_start_utc + timedelta(hours=1), min_window),
                        max_lookback_utc,
                    )

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
                )

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
