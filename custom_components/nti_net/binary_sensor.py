"""Binary sensor platform for the NTI net integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import NtiNetCoordinator, NtiNetPlant, NtiNetZone
from .entity import NtiNetEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

PLANT_BINARY_SENSORS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="flame",
        translation_key="flame",
        device_class=BinarySensorDeviceClass.HEAT,
    ),
    BinarySensorEntityDescription(
        key="heating_pump",
        translation_key="heating_pump",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    BinarySensorEntityDescription(
        key="is_online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add plant and zone binary sensors."""
    coordinator: NtiNetCoordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for plant in coordinator.data.values():
        for description in PLANT_BINARY_SENSORS:
            entities.append(NtiNetPlantBinarySensor(coordinator, plant, description))
        for zone in plant.zones.values():
            entities.append(NtiNetZoneBinarySensor(coordinator, plant, zone))
    async_add_entities(entities)


class NtiNetPlantBinarySensor(NtiNetEntity, BinarySensorEntity):
    """A boolean flag taken from the plant envelope."""

    def __init__(
        self,
        coordinator: NtiNetCoordinator,
        plant: NtiNetPlant,
        description: BinarySensorEntityDescription,
    ) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator, plant, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the flag value."""
        plant = self.plant
        if plant is None:
            return None
        return getattr(plant, self.entity_description.key, None)


class NtiNetZoneBinarySensor(NtiNetEntity, BinarySensorEntity):
    """Report whether a zone is actively requesting heat."""

    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(
        self,
        coordinator: NtiNetCoordinator,
        plant: NtiNetPlant,
        zone: NtiNetZone,
    ) -> None:
        """Initialise the zone binary sensor."""
        super().__init__(coordinator, plant, f"zone_{zone.number}_heat_request")
        self._zone_number = zone.number

    @property
    def name(self) -> str:
        """Return the entity name."""
        zone = self._zone
        return f"{zone.name} heating" if zone else "Heating"

    @property
    def _zone(self) -> NtiNetZone | None:
        """Return the freshest snapshot of this zone."""
        plant = self.plant
        if plant is None:
            return None
        return plant.zones.get(self._zone_number)

    @property
    def is_on(self) -> bool | None:
        """Return whether the zone is calling for heat."""
        zone = self._zone
        return zone.heat_request if zone else None
