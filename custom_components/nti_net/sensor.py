"""Sensor platform for the NTI net integration.

Every reading here maps to a field that the live ``remote/plantData/{gw}``
payload actually returns.  Values are Celsius because the client pins
``umsys=si``; the payload carries no pressure, flow or setpoint readings on a
standard boiler gateway, so none are invented.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import NtiNetCoordinator, NtiNetPlant
from .entity import NtiNetEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

PLANT_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="outside_temperature",
        translation_key="outside_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="dhw_temperature",
        translation_key="dhw_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="dhw_reduced_temperature",
        translation_key="dhw_reduced_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
)

PLANT_ATTRIBUTE_MAP: dict[str, str] = {
    "outside_temperature": "outside_temp",
    "dhw_temperature": "dhw_temp",
    "dhw_reduced_temperature": "dhw_reduced_temp",
}

#: Sensors that only make sense on an appliance with a hot water cylinder.
DHW_SENSOR_KEYS: frozenset[str] = frozenset(
    {"dhw_temperature", "dhw_reduced_temperature"}
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add plant-level sensors."""
    coordinator: NtiNetCoordinator = entry.runtime_data
    entities: list[NtiNetSensor] = []
    for plant in coordinator.data.values():
        for description in PLANT_SENSORS:
            if description.key in DHW_SENSOR_KEYS and not plant.has_dhw:
                continue
            entities.append(NtiNetSensor(coordinator, plant, description))
    async_add_entities(entities)


class NtiNetSensor(NtiNetEntity, SensorEntity):
    """A numeric reading taken from the plant envelope."""

    def __init__(
        self,
        coordinator: NtiNetCoordinator,
        plant: NtiNetPlant,
        description: SensorEntityDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, plant, description.key)
        self.entity_description = description
        self._attribute = PLANT_ATTRIBUTE_MAP[description.key]

    @property
    def native_value(self) -> float | None:
        """Return the current reading."""
        plant = self.plant
        if plant is None:
            return None
        return getattr(plant, self._attribute, None)
