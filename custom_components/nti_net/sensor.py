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
from homeassistant.const import UnitOfEnergy, UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_GAS_KWH_PER_M3,
    CONF_GAS_UNIT,
    DEFAULT_GAS_KWH_PER_M3,
    GAS_UNIT_KWH,
    GAS_UNIT_M3,
)
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
    "gas_consumption": "gas_this_month",
    "gas_consumption_yesterday": "gas_yesterday",
    "gas_last_month": "gas_last_month",
    "gas_heating_last_month": "gas_heating_last_month",
    "gas_dhw_last_month": "gas_dhw_last_month",
}

#: Gas sensors, created only on an appliance that advertises metering.
#:
#: The cloud reports consumption in kilowatt hours (the route is literally
#: ``consKwhSequences`` in the shipped app), so the device class is ``energy``
#: with a kWh unit rather than ``gas`` -- Home Assistant's gas device class only
#: accepts volume units.  Its energy dashboard accepts an ``energy`` sensor as a
#: gas source, so this still feeds the gas view.
GAS_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="gas_consumption",
        translation_key="gas_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # Month to date: it climbs while the month runs and drops back to zero
        # on the first, which is exactly the reset Home Assistant's
        # `total_increasing` handling is designed for.
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    # The rest are per-period totals: they go up and down from one period to
    # the next, so they are not meters.  Home Assistant's `energy` device class
    # only permits `total`/`total_increasing`, and claiming either would make
    # the statistics engine read a drop as a meter reset.  They therefore carry
    # no state class and are not offered as a dashboard source.
    SensorEntityDescription(
        key="gas_consumption_yesterday",
        translation_key="gas_consumption_yesterday",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="gas_last_month",
        translation_key="gas_last_month",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="gas_heating_last_month",
        translation_key="gas_heating_last_month",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
    SensorEntityDescription(
        key="gas_dhw_last_month",
        translation_key="gas_dhw_last_month",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    ),
)

#: The sensors whose unit follows the account's gas unit choice.
GAS_SENSOR_KEYS: frozenset[str] = frozenset(
    description.key for description in GAS_SENSORS
)

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
    gas_unit = entry.options.get(CONF_GAS_UNIT, GAS_UNIT_KWH)
    kwh_per_m3 = float(
        entry.options.get(CONF_GAS_KWH_PER_M3, DEFAULT_GAS_KWH_PER_M3)
    )
    entities: list[NtiNetSensor] = []
    for plant in coordinator.data.values():
        for description in PLANT_SENSORS:
            if description.key in DHW_SENSOR_KEYS and not plant.has_dhw:
                continue
            entities.append(NtiNetSensor(coordinator, plant, description))
        if plant.has_metering:
            for description in GAS_SENSORS:
                entities.append(
                    NtiNetSensor(
                        coordinator,
                        plant,
                        description,
                        gas_unit=gas_unit,
                        kwh_per_m3=kwh_per_m3,
                    )
                )
    async_add_entities(entities)


class NtiNetSensor(NtiNetEntity, SensorEntity):
    """A numeric reading taken from the plant envelope."""

    def __init__(
        self,
        coordinator: NtiNetCoordinator,
        plant: NtiNetPlant,
        description: SensorEntityDescription,
        gas_unit: str = GAS_UNIT_KWH,
        kwh_per_m3: float = DEFAULT_GAS_KWH_PER_M3,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, plant, description.key)
        self.entity_description = description
        self._attribute = PLANT_ATTRIBUTE_MAP[description.key]
        self._factor = 1.0
        if description.key in GAS_SENSOR_KEYS and gas_unit == GAS_UNIT_M3:
            # The cloud reports energy; a volume reading is always derived.
            self._factor = 1.0 / kwh_per_m3 if kwh_per_m3 else 1.0
            self._attr_device_class = SensorDeviceClass.GAS
            self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS

    @property
    def native_value(self) -> float | None:
        """Return the current reading, in the configured gas unit."""
        plant = self.plant
        if plant is None:
            return None
        value = getattr(plant, self._attribute, None)
        if value is None or self._factor == 1.0:
            return value
        # Round back to the precision the cloud reports, so the derived volume
        # does not imply more accuracy than the source has.
        return round(value * self._factor, 3)
