"""Sensor platform for EMMASA (Portal Ciudadano)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EmmasaConfigEntry
from .api import ContractAlert, ContractInfo, ContractSnapshot
from .const import ALERT_DAYTIME_ID, ALERT_NIGHTTIME_ID
from .coordinator import EmmasaDataUpdateCoordinator, statistic_id_for_contract
from .entity import EmmasaEntity

PARALLEL_UPDATES = 0


def _find_alert(
    snapshot: ContractSnapshot,
    alert_id: str,
    keyword: str,
) -> ContractAlert | None:
    """Locate a contract alert by stable UUID or case-insensitive name keyword."""
    for alert in snapshot.alerts:
        if alert.alert_id == alert_id:
            return alert
    low_kw = keyword.lower()
    for alert in snapshot.alerts:
        if low_kw in alert.alert_name.lower():
            return alert
    return None


def _compute_lag_hours(snapshot: ContractSnapshot) -> float | None:
    """Compute telemetry publication lag in hours from the latest reading."""
    if snapshot.last_reading_timestamp_utc is None:
        return None
    delta = datetime.now(UTC) - snapshot.last_reading_timestamp_utc
    return round(max(delta.total_seconds() / 3600.0, 0.0), 1)


@dataclass(frozen=True, kw_only=True)
class EmmasaSensorEntityDescription(SensorEntityDescription):
    """Describe an EMMASA sensor entity."""

    value_fn: Callable[[ContractSnapshot], float | datetime | None]
    attributes_fn: Callable[[ContractSnapshot], dict[str, Any] | None] | None = None


SENSOR_DESCRIPTIONS: tuple[EmmasaSensorEntityDescription, ...] = (
    EmmasaSensorEntityDescription(
        key="meter_reading",
        translation_key="meter_reading",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        value_fn=lambda s: (
            s.latest_meter_reading.volume_acc_m3 if s.latest_meter_reading is not None else None
        ),
        attributes_fn=lambda s: {
            "energy_dashboard_statistic_id": statistic_id_for_contract(s.contract),
            "reading_timestamp_utc": (
                s.latest_meter_reading.timestamp_utc.isoformat()
                if s.latest_meter_reading is not None
                else None
            ),
        },
    ),
    EmmasaSensorEntityDescription(
        key="current_period_consumption",
        translation_key="current_period_consumption",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=2,
        value_fn=lambda s: (
            s.current_period_consumption.volume_m3
            if s.current_period_consumption is not None
            else None
        ),
        attributes_fn=lambda s: (
            {
                "period_start_utc": (
                    s.current_period_consumption.since_utc.isoformat()
                    if s.current_period_consumption.since_utc is not None
                    else None
                )
            }
            if s.current_period_consumption is not None
            else None
        ),
    ),
    EmmasaSensorEntityDescription(
        key="yesterday_consumption",
        translation_key="yesterday_consumption",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=lambda s: s.yesterday_consumption_liters,
    ),
    EmmasaSensorEntityDescription(
        key="last_billed_consumption",
        translation_key="last_billed_consumption",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=2,
        value_fn=lambda s: (
            s.latest_billed_consumption.volume_m3
            if s.latest_billed_consumption is not None
            else None
        ),
        attributes_fn=lambda s: (
            {
                "period_code": s.latest_billed_consumption.period_code,
                "period_label": s.latest_billed_consumption.period_label,
            }
            if s.latest_billed_consumption is not None
            else None
        ),
    ),
    EmmasaSensorEntityDescription(
        key="last_invoice_amount",
        translation_key="last_invoice_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda s: s.latest_invoice.amount_eur if s.latest_invoice is not None else None,
        attributes_fn=lambda s: (
            {
                "period_code": s.latest_invoice.period_code,
                "invoice_date_utc": (
                    s.latest_invoice.invoice_date_utc.isoformat()
                    if s.latest_invoice.invoice_date_utc is not None
                    else None
                ),
            }
            if s.latest_invoice is not None
            else None
        ),
    ),
    EmmasaSensorEntityDescription(
        key="last_reading_timestamp",
        translation_key="last_reading_timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_reading_timestamp_utc,
    ),
    EmmasaSensorEntityDescription(
        key="telelectura_lag",
        translation_key="telelectura_lag",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_compute_lag_hours,
    ),
    EmmasaSensorEntityDescription(
        key="daytime_alert_limit",
        translation_key="daytime_alert_limit",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: (
            alert.user_limit_liters
            if (alert := _find_alert(s, ALERT_DAYTIME_ID, "diurn")) is not None
            else None
        ),
    ),
    EmmasaSensorEntityDescription(
        key="nighttime_alert_limit",
        translation_key="nighttime_alert_limit",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: (
            alert.user_limit_liters
            if (alert := _find_alert(s, ALERT_NIGHTTIME_ID, "nocturn")) is not None
            else None
        ),
    ),
)


class EmmasaSensorEntity(EmmasaEntity, SensorEntity):
    """Representation of an EMMASA sensor entity."""

    entity_description: EmmasaSensorEntityDescription

    def __init__(
        self,
        coordinator: EmmasaDataUpdateCoordinator,
        contract: ContractInfo,
        description: EmmasaSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, contract, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | datetime | None:
        """Return the sensor value."""
        snapshot = self.contract_snapshot
        if snapshot is None:
            return None
        return self.entity_description.value_fn(snapshot)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return supplementary non-PII metadata attributes."""
        snapshot = self.contract_snapshot
        if snapshot is None or self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(snapshot)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EmmasaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EMMASA sensors from a config entry."""
    coordinator = entry.runtime_data.coordinator
    known_contracts: set[str] = set()

    @callback
    def _async_add_new_contracts() -> None:
        new_entities: list[EmmasaSensorEntity] = []
        for contract_id, snapshot in coordinator.data.contracts.items():
            if contract_id in known_contracts:
                continue
            known_contracts.add(contract_id)
            new_entities.extend(
                EmmasaSensorEntity(coordinator, snapshot.contract, description)
                for description in SENSOR_DESCRIPTIONS
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_contracts()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_contracts))
