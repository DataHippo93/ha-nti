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
        key="has_fault",
        translation_key="fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BinarySensorEntityDescription(
        key="auto_thermoreg",
        translation_key="auto_thermoreg",
        icon="mdi:thermostat-auto",
    ),
    BinarySensorEntityDescription(
        key="is_online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
)


#: Flags read from routes the BSB view does not have.
GENERIC_ONLY_KEYS: frozenset[str] = frozenset({"has_fault", "auto_thermoreg"})


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
            if description.key in GENERIC_ONLY_KEYS and "bsb" in plant.raw:
                continue
            entities.append(NtiNetPlantBinarySensor(coordinator, plant, description))
        for zone in plant.zones.values():
            entities.append(NtiNetZoneBinarySensor(coordinator, plant, zone))
            entities.append(NtiNetZoneOverrideSensor(coordinator, plant, zone))
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


class NtiNetZoneOverrideSensor(NtiNetEntity, BinarySensorEntity):
    """Report whether a zone's manual override ("deroga") is in force.

    The override is neither a mode nor a setpoint the climate entity can show:
    the cloud keeps it in ``zones.N.der`` alongside the normal setpoints.  It is
    also not part of the appliance's own mode list, so surfacing it as a binary
    sensor keeps the climate entity honest while still making the state visible.
    """

    _attr_translation_key = "zone_override"
    _attr_icon = "mdi:timer-sand"

    def __init__(
        self,
        coordinator: NtiNetCoordinator,
        plant: NtiNetPlant,
        zone: NtiNetZone,
    ) -> None:
        """Initialise the override sensor."""
        super().__init__(coordinator, plant, f"zone_{zone.number}_override")
        self._zone_number = zone.number

    @property
    def name(self) -> str:
        """Return the entity name, qualified by zone."""
        zone = self._zone
        return f"{zone.name} override" if zone else "Override"

    @property
    def _zone(self) -> NtiNetZone | None:
        """Return the freshest snapshot of this zone."""
        plant = self.plant
        if plant is None:
            return None
        return plant.zones.get(self._zone_number)

    @property
    def is_on(self) -> bool | None:
        """Return whether an override is active."""
        zone = self._zone
        return zone.override_active if zone else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the override's setpoint and expiry.

        A disabled override carries a 1970 epoch expiry, which is noise, so it
        is only reported while the override is actually on.
        """
        zone = self._zone
        if zone is None or not zone.override_active:
            return {}
        return {
            "temperature": zone.deroga_temp,
            "until": zone.deroga_until,
        }
