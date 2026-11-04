"""Base entity for the NTI net integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import NtiNetCoordinator, NtiNetPlant


class NtiNetEntity(CoordinatorEntity[NtiNetCoordinator]):
    """Base class wiring one plant to the device registry."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NtiNetCoordinator,
        plant: NtiNetPlant,
        key: str,
    ) -> None:
        """Initialise the entity for one plant."""
        super().__init__(coordinator)
        self._plant_id = plant.gateway_id
        self._attr_unique_id = f"{plant.gateway_id}_{key}"

    @property
    def plant(self) -> NtiNetPlant | None:
        """Return the freshest plant snapshot."""
        return self.coordinator.data.get(self._plant_id)

    @property
    def available(self) -> bool:
        """Return whether the plant is present and reachable."""
        plant = self.plant
        if plant is None or not self.coordinator.last_update_success:
            return False
        return plant.is_online is not False

    @property
    def device_info(self) -> DeviceInfo:
        """Group every entity of one gateway under a single device."""
        plant = self.plant
        return DeviceInfo(
            identifiers={(DOMAIN, self._plant_id)},
            manufacturer=MANUFACTURER,
            name=plant.name if plant else self._plant_id,
            model=plant.model if plant else None,
            sw_version=plant.firmware if plant else None,
            serial_number=plant.serial or self._plant_id,
        )
