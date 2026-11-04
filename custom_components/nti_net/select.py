"""Select platform for the NTI net integration.

Options are derived from the ``allowedOptions`` lists the service sends with
each reading, so the UI only offers modes the appliance actually accepts
(a standard boiler gateway advertises plant modes ``[0, 1, 2, 5]`` and DHW
modes ``[0, 1, 2]``).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import NtiNetError
from .const import PlantMode
from .coordinator import NtiNetCoordinator, NtiNetPlant
from .entity import NtiNetEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the plant mode selector."""
    coordinator: NtiNetCoordinator = entry.runtime_data
    async_add_entities(
        NtiNetPlantModeSelect(coordinator, plant)
        for plant in coordinator.data.values()
    )


class NtiNetPlantModeSelect(NtiNetEntity, SelectEntity):
    """Select the boiler operating mode (off/summer/winter/heating only)."""

    _attr_translation_key = "plant_mode"
    _fallback_options = (
        PlantMode.OFF,
        PlantMode.WINTER,
        PlantMode.SUMMER,
        PlantMode.HEATING_ONLY,
    )

    def __init__(self, coordinator: NtiNetCoordinator, plant: NtiNetPlant) -> None:
        """Initialise the plant mode selector."""
        super().__init__(coordinator, plant, "plant_mode")

    @property
    def options(self) -> list[str]:
        """Return the modes this appliance advertises."""
        plant = self.plant
        allowed = plant.allowed_modes if plant else []
        values = allowed or list(self._fallback_options)
        return [
            PlantMode.NAMES[value] for value in values if value in PlantMode.NAMES
        ]

    @property
    def current_option(self) -> str | None:
        """Return the active plant mode."""
        plant = self.plant
        if plant is None or plant.mode is None:
            return None
        return PlantMode.NAMES.get(plant.mode)

    async def async_select_option(self, option: str) -> None:
        """Apply a new plant mode."""
        plant = self.plant
        if plant is None:
            return
        new_mode = PlantMode.VALUES.get(option)
        if new_mode is None:
            return
        try:
            await self.coordinator.client.async_set_plant_mode(
                self._plant_id, new=new_mode, old=plant.mode
            )
        except NtiNetError as err:
            raise HomeAssistantError(f"Could not set plant mode: {err}") from err
        await self.coordinator.async_request_refresh()
