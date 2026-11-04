"""Water heater platform for the NTI net integration.

The DHW setpoint is a single scalar on the wire (``POST plantData/{gw}/dhwTemp``
with ``{"new": 53.0, "old": 53.0}``).  Sending the comfort/reduced pair that
zone setpoints use returns HTTP 500, so this platform never does that.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import NtiNetError
from .const import DhwMode
from .coordinator import NtiNetCoordinator, NtiNetPlant
from .entity import NtiNetEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a water heater entity for appliances with a cylinder."""
    coordinator: NtiNetCoordinator = entry.runtime_data
    async_add_entities(
        NtiNetWaterHeater(coordinator, plant)
        for plant in coordinator.data.values()
        if plant.has_dhw
    )


class NtiNetWaterHeater(NtiNetEntity, WaterHeaterEntity):
    """Domestic hot water cylinder of one gateway."""

    _attr_name = "Hot water"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
    )

    def __init__(self, coordinator: NtiNetCoordinator, plant: NtiNetPlant) -> None:
        """Initialise the water heater entity."""
        super().__init__(coordinator, plant, "water_heater")

    @property
    def current_temperature(self) -> float | None:
        """Return the measured cylinder temperature."""
        plant = self.plant
        return plant.dhw_temp if plant else None

    @property
    def target_temperature(self) -> float | None:
        """Return the configured cylinder setpoint."""
        plant = self.plant
        return plant.dhw_setpoint if plant else None

    @property
    def min_temp(self) -> float:
        """Return the lowest accepted cylinder setpoint."""
        plant = self.plant
        return plant.dhw_min if plant else 40.0

    @property
    def max_temp(self) -> float:
        """Return the highest accepted cylinder setpoint."""
        plant = self.plant
        return plant.dhw_max if plant else 65.0

    @property
    def target_temperature_step(self) -> float:
        """Return the cylinder setpoint increment."""
        plant = self.plant
        return plant.dhw_step if plant else 1.0

    @property
    def operation_list(self) -> list[str]:
        """Return the DHW modes this appliance advertises."""
        plant = self.plant
        allowed = plant.allowed_dhw_modes if plant else []
        values = allowed or [
            DhwMode.DISABLED,
            DhwMode.TIME_BASED,
            DhwMode.ALWAYS_ACTIVE,
        ]
        return [DhwMode.NAMES[value] for value in values if value in DhwMode.NAMES]

    @property
    def current_operation(self) -> str | None:
        """Return the active DHW mode."""
        plant = self.plant
        if plant is None or plant.dhw_mode is None:
            return None
        return DhwMode.NAMES.get(plant.dhw_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Apply a new cylinder setpoint."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        plant = self.plant
        if temperature is None or plant is None:
            return
        try:
            await self.coordinator.client.async_set_dhw_temperature(
                self._plant_id,
                new=float(temperature),
                old=plant.dhw_setpoint,
            )
        except NtiNetError as err:
            raise HomeAssistantError(
                f"Could not set hot water temperature: {err}"
            ) from err
        await self.coordinator.async_request_refresh()

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Apply a new DHW mode."""
        plant = self.plant
        if plant is None:
            return
        new_mode = DhwMode.VALUES.get(operation_mode)
        if new_mode is None:
            return
        try:
            await self.coordinator.client.async_set_dhw_mode(
                self._plant_id, new=new_mode, old=plant.dhw_mode
            )
        except NtiNetError as err:
            raise HomeAssistantError(f"Could not set hot water mode: {err}") from err
        await self.coordinator.async_request_refresh()
