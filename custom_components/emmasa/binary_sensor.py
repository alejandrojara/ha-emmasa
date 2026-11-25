"""Binary sensor platform for EMMASA (Portal Ciudadano)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EmmasaConfigEntry
from .api import ContractInfo, ContractSnapshot
from .const import ALERT_DAYTIME_ID, ALERT_NIGHTTIME_ID
from .coordinator import EmmasaDataUpdateCoordinator
from .entity import EmmasaEntity
from .sensor import _find_alert

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EmmasaBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe an EMMASA binary sensor entity."""

    is_on_fn: Callable[[ContractSnapshot], bool | None]


BINARY_SENSOR_DESCRIPTIONS: tuple[EmmasaBinarySensorEntityDescription, ...] = (
    EmmasaBinarySensorEntityDescription(
        key="telelectura_available",
        translation_key="telelectura_available",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda s: bool(
            s.contract.allows_alert_configuration or s.last_reading_timestamp_utc is not None
        ),
    ),
    EmmasaBinarySensorEntityDescription(
        key="daytime_alert_enabled",
        translation_key="daytime_alert_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=lambda s: (
            alert.is_active
            if (alert := _find_alert(s, ALERT_DAYTIME_ID, "diurn")) is not None
            else None
        ),
    ),
    EmmasaBinarySensorEntityDescription(
        key="nighttime_alert_enabled",
        translation_key="nighttime_alert_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=lambda s: (
            alert.is_active
            if (alert := _find_alert(s, ALERT_NIGHTTIME_ID, "nocturn")) is not None
            else None
        ),
    ),
)


class EmmasaBinarySensorEntity(EmmasaEntity, BinarySensorEntity):
    """Representation of an EMMASA diagnostic binary sensor."""

    entity_description: EmmasaBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: EmmasaDataUpdateCoordinator,
        contract: ContractInfo,
        description: EmmasaBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, contract, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return True if the binary sensor is on."""
        snapshot = self.contract_snapshot
        if snapshot is None:
            return None
        return self.entity_description.is_on_fn(snapshot)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EmmasaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EMMASA binary sensors from a config entry."""
    coordinator = entry.runtime_data.coordinator
    known_contracts: set[str] = set()

    @callback
    def _async_add_new_contracts() -> None:
        new_entities: list[EmmasaBinarySensorEntity] = []
        for contract_id, snapshot in coordinator.data.contracts.items():
            if contract_id in known_contracts:
                continue
            known_contracts.add(contract_id)
            new_entities.extend(
                EmmasaBinarySensorEntity(coordinator, snapshot.contract, description)
                for description in BINARY_SENSOR_DESCRIPTIONS
            )
        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_contracts()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_contracts))
