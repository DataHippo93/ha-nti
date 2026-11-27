"""Switch platform for the NTI net integration.

One switch per combi gateway: domestic hot water on or off.  It exists for the
case the water heater entity handles badly, a combi boiler used as a backup hot
water source, where the only question is "is the boiler making hot water or
not".

The state comes from the ``DhwMode`` data item, never from the aggregated
read: on a combi that read reports mode 0 whatever the real mode is, and a
switch that says "off" while the boiler is heating water is worse than none.

Appliances that advertise their own DHW modes (a cylinder with eco/comfort)
do not get this switch.  Turning one "on" would have to pick a mode for the
user, and the water heater entity already offers the real choice.
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
from .const import DATA_ITEM_DHW_MODE, WRITE_SETTLE_SECONDS, DhwMode
from .coordinator import NtiNetCoordinator, NtiNetPlant
from .entity import NtiNetEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

#: Confirming reads after a write.  The gateway usually reflects a write
#: within one settle delay; a second look covers a slow one.
CONFIRM_ATTEMPTS = 2


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a hot water switch for combi gateways."""
    coordinator: NtiNetCoordinator = entry.runtime_data
    async_add_entities(
        NtiNetHotWaterSwitch(coordinator, plant)
        for plant in coordinator.data.values()
        if plant.is_combi_dhw
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

    async def _async_read_mode(self) -> int:
        """Read the mode from the appliance right now.

        Raises `NtiNetError` when it cannot be read, including when the answer
        simply leaves the item out.
        """
        plant = self.plant
        features = plant.features if plant else {}
        items = await self.coordinator.client.async_get_data_items(
            self._plant_id, [{"id": DATA_ITEM_DHW_MODE, "zn": 0}], features
        )
        for item in items:
            if item.get("id") != DATA_ITEM_DHW_MODE:
                continue
            if item.get("error") is True or item.get("invalid") is True:
                break
            value = item.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                break
            return int(value)
        raise NtiNetError("The hot water mode was not in the answer")

    def _show(self, mode: int | None) -> None:
        """Put a mode on the current plant snapshot and tell the listeners.

        The snapshot is looked up again each time: a poll may have replaced
        it while a request was in flight.
        """
        plant = self.plant
        if plant is None:
            return
        plant.dhw_mode_item = mode
        if mode is not None and not plant.allowed_dhw_modes:
            plant.dhw_mode = mode
        self.coordinator.async_update_listeners()

    async def _async_set_mode(self, new_mode: int) -> None:
        """Switch the mode: read, write, show, confirm.

        The state Home Assistant holds can be a whole poll interval old, and
        the mode can be changed at the boiler or in the vendor app in the
        meantime.  So the decision to write, and the "old" value the cloud
        checks, both come from a fresh read, never from the held state.
        """
        if self.plant is None:
            raise HomeAssistantError("The boiler is not available")

        try:
            current = await self._async_read_mode()
        except NtiNetError as err:
            raise HomeAssistantError(
                f"Could not read the current hot water mode, nothing was "
                f"changed: {err}"
            ) from err

        if current == new_mode:
            # Already there (changed elsewhere, perhaps): just show the truth.
            self._show(current)
            return

        try:
            await self.coordinator.client.async_set_dhw_mode(
                self._plant_id, new=new_mode, old=current
            )
        except NtiNetError as err:
            self._show(current)
            raise HomeAssistantError(f"Could not switch hot water: {err}") from err

        # Acknowledged.  Show it now, then confirm from the appliance, which
        # takes a few seconds to reflect a write.
        self._show(new_mode)
        observed: int | None = None
        for _ in range(CONFIRM_ATTEMPTS):
            await asyncio.sleep(WRITE_SETTLE_SECONDS)
            try:
                observed = await self._async_read_mode()
            except NtiNetError as err:
                _LOGGER.debug("Confirming read failed for %s: %s", self._plant_id, err)
                observed = None
                continue
            if observed == new_mode:
                break

        if observed is None:
            # Acknowledged but not confirmed: say "unknown" rather than claim
            # either state.  The next poll settles it.
            self._show(None)
            raise HomeAssistantError(
                "The boiler accepted the change but it could not be "
                "confirmed; the switch will update at the next poll"
            )
        self._show(observed)
        if observed != new_mode:
            raise HomeAssistantError(
                "The boiler accepted the change but still reports the "
                "previous hot water mode"
            )
