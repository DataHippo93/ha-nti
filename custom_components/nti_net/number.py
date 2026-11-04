"""Number platform for the NTI net integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import NtiNetError
from .coordinator import NtiNetCoordinator, NtiNetPlant, NtiNetZone
from .entity import NtiNetEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add one comfort and one reduced setpoint per zone."""
    coordinator: NtiNetCoordinator = entry.runtime_data
    entities: list[NumberEntity] = []
    for plant in coordinator.data.values():
        for zone in plant.zones.values():
            entities.append(NtiNetZoneSetpoint(coordinator, plant, zone, "comfort"))
            entities.append(NtiNetZoneSetpoint(coordinator, plant, zone, "reduced"))
    async_add_entities(entities)


class NtiNetZoneSetpoint(NtiNetEntity, NumberEntity):
    """A zone setpoint that is written back as a comfort/reduced pair."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: NtiNetCoordinator,
        plant: NtiNetPlant,
        zone: NtiNetZone,
        kind: str,
    ) -> None:
        """Initialise one setpoint number entity."""
        super().__init__(coordinator, plant, f"zone_{zone.number}_{kind}_temp")
        self._zone_number = zone.number
        self._kind = kind

    @property
    def name(self) -> str:
        """Return the entity name, qualified by zone."""
        zone = self._zone
        label = "comfort" if self._kind == "comfort" else "reduced"
        return f"{zone.name} {label} temperature" if zone else label.capitalize()

    @property
    def _zone(self) -> NtiNetZone | None:
        """Return the freshest snapshot of this zone."""
        plant = self.plant
        if plant is None:
            return None
        return plant.zones.get(self._zone_number)

    @property
    def native_step(self) -> float:
        """Return the increment the controller accepts."""
        zone = self._zone
        return zone.step if zone else 0.5

    @property
    def native_min_value(self) -> float:
        """Return the lowest allowed setpoint."""
        zone = self._zone
        if zone is None:
            return 10.0
        return zone.reduced_min if self._kind == "reduced" else zone.comfort_min

    @property
    def native_max_value(self) -> float:
        """Return the highest allowed setpoint."""
        zone = self._zone
        if zone is None:
            return 30.0
        return zone.reduced_max if self._kind == "reduced" else zone.comfort_max

    @property
    def native_value(self) -> float | None:
        """Return the current value of this setpoint."""
        zone = self._zone
        if zone is None:
            return None
        return zone.comfort_temp if self._kind == "comfort" else zone.reduced_temp

    async def async_set_native_value(self, value: float) -> None:
        """Write the setpoint pair back to the cloud."""
        zone = self._zone
        if zone is None:
            return
        comfort = zone.comfort_temp if zone.comfort_temp is not None else value
        reduced = zone.reduced_temp if zone.reduced_temp is not None else value
        if self._kind == "comfort":
            comfort = value
        else:
            reduced = value
        try:
            await self.coordinator.client.async_set_zone_temperatures(
                self._plant_id,
                self._zone_number,
                comfort=float(comfort),
                reduced=float(reduced),
                old_comfort=zone.comfort_temp,
                old_reduced=zone.reduced_temp,
            )
        except NtiNetError as err:
            raise HomeAssistantError(
                f"Could not set {self._kind} setpoint: {err}"
            ) from err
        await self.coordinator.async_request_refresh()
