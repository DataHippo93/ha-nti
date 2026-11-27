"""Switch platform for the NTI net integration.

One switch per gateway: domestic hot water on or off.  It exists for the case
the water heater entity handles badly, a combi boiler used as a backup hot
water source, where the only question is "is the boiler making hot water or
not".

The state comes from the ``DhwMode`` data item, never from the aggregated
read: on a combi that read reports mode 0 whatever the real mode is, and a
switch that says "off" while the boiler is heating water is worse than none.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import NtiNetError
from .const import WRITE_SETTLE_SECONDS, DhwMode
from .coordinator import NtiNetCoordinator, NtiNetPlant
from .entity import NtiNetEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a hot water switch for gateways that expose the DHW mode item."""
    coordinator: NtiNetCoordinator = entry.runtime_data
    async_add_entities(
        NtiNetHotWaterSwitch(coordinator, plant)
        for plant in coordinator.data.values()
        if "bsb" not in plant.raw and plant.dhw_mode_changeable
    )


class NtiNetHotWaterSwitch(NtiNetEntity, SwitchEntity):
    """Turn domestic hot water production on or off."""

    _attr_translation_key = "hot_water"
    _attr_icon = "mdi:water-boiler"

    def __init__(self, coordinator: NtiNetCoordinator, plant: NtiNetPlant) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, plant, "hot_water_switch")

    @property
    def is_on(self) -> bool | None:
        """Return whether hot water is enabled, or None when not known."""
        plant = self.plant
        if plant is None or plant.dhw_mode_item is None:
            return None
        return plant.dhw_mode_item != DhwMode.DISABLED

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the underlying mode (off / eco / comfort)."""
        plant = self.plant
        if plant is None or plant.dhw_mode_item is None:
            return {}
        return {"mode": DhwMode.NAMES.get(plant.dhw_mode_item, plant.dhw_mode_item)}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable hot water (always active)."""
        await self._async_set_mode(DhwMode.ALWAYS_ACTIVE)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable hot water."""
        await self._async_set_mode(DhwMode.DISABLED)

    async def _async_set_mode(self, new_mode: int) -> None:
        """Write the mode, show it at once, then confirm it from the cloud."""
        plant = self.plant
        if plant is None:
            raise HomeAssistantError("The boiler is not available")
        if plant.dhw_mode_item is None:
            # The write carries the last observed value for the cloud's
            # concurrency check.  Without a known value it would be a guess.
            raise HomeAssistantError(
                "The current hot water mode is not known yet; try again after "
                "the next update"
            )
        if plant.dhw_mode_item == new_mode:
            return
        try:
            await self.coordinator.client.async_set_dhw_mode(
                self._plant_id, new=new_mode, old=plant.dhw_mode_item
            )
        except NtiNetError as err:
            raise HomeAssistantError(f"Could not switch hot water: {err}") from err

        # The gateway takes a few seconds to reflect a write, so an immediate
        # re-read would flip the switch back.  Show the new state now and
        # confirm it once the gateway has caught up.
        plant.dhw_mode_item = new_mode
        if not plant.allowed_dhw_modes:
            plant.dhw_mode = new_mode
        self.coordinator.async_update_listeners()
        await asyncio.sleep(WRITE_SETTLE_SECONDS)
        await self.coordinator.async_request_refresh()
