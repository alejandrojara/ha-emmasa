"""Base entity class for EMMASA (Portal Ciudadano)."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ContractInfo, ContractSnapshot
from .const import DOMAIN, PORTAL_DASHBOARD_URL
from .coordinator import EmmasaDataUpdateCoordinator


class EmmasaEntity(CoordinatorEntity[EmmasaDataUpdateCoordinator]):
    """Base entity linked to an EMMASA water supply contract device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EmmasaDataUpdateCoordinator,
        contract: ContractInfo,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._contract_id = contract.contract_id
        self._attr_unique_id = f"{contract.contract_id}_{key}"
        model = (
            "Contador de Agua con Telelectura"
            if contract.allows_alert_configuration
            else "Contrato de Suministro de Agua"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, contract.contract_id)},
            name=f"EMMASA Contrato {contract.name}",
            manufacturer="EMMASA (Elliot Cloud)",
            model=model,
            serial_number=contract.name,
            configuration_url=PORTAL_DASHBOARD_URL,
        )

    @property
    def contract_snapshot(self) -> ContractSnapshot | None:
        """Return the latest snapshot for this entity's contract."""
        return self.coordinator.data.contracts.get(self._contract_id)

    @property
    def available(self) -> bool:
        """Return True when the coordinator is healthy and the contract exists."""
        return super().available and self._contract_id in self.coordinator.data.contracts
