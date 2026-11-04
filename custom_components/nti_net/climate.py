"""Climate platform for the NTI net integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import NtiNetError
from .const import ZoneMode
from .coordinator import NtiNetCoordinator, NtiNetPlant, NtiNetZone
from .entity import NtiNetEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

HA_TO_ZONE_MODE = {
    HVACMode.OFF: ZoneMode.OFF,
    HVACMode.HEAT: ZoneMode.MANUAL,
    HVACMode.AUTO: ZoneMode.TIME_PROGRAM,
}

ZONE_TO_HA_MODE = {
    ZoneMode.OFF: HVACMode.OFF,
    ZoneMode.MANUAL_NIGHT: HVACMode.AUTO,
    ZoneMode.MANUAL: HVACMode.HEAT,
    ZoneMode.TIME_PROGRAM: HVACMode.AUTO,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add one climate entity per zone per gateway."""
    coordinator: NtiNetCoordinator = entry.runtime_data
    async_add_entities(
        NtiNetClimate(coordinator, plant, zone)
        for plant in coordinator.data.values()
        for zone in plant.zones.values()
    )


class NtiNetClimate(NtiNetEntity, ClimateEntity):
    """A heating zone exposed as a climate entity."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _enable_turn_on_off_backwards_compatibility = False

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return the features this zone accepts.

        A standard boiler zone advertises ``allowedOptions [2, 3]`` -- manual
        and time program -- so there is no "off" state to switch to.  Turning
        the zone off means changing the *plant* mode instead, which lives on
        the plant-mode select entity.
        """
        features = ClimateEntityFeature.TARGET_TEMPERATURE
        zone = self._zone
        if zone is not None and (
            not zone.allowed_modes or ZoneMode.OFF in zone.allowed_modes
        ):
            features |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        return features

    def __init__(
        self,
        coordinator: NtiNetCoordinator,
        plant: NtiNetPlant,
        zone: NtiNetZone,
    ) -> None:
        """Initialise the climate entity for one zone."""
        super().__init__(coordinator, plant, f"zone_{zone.number}_climate")
        self._zone_number = zone.number
        self._attr_name = zone.name

    @property
    def _zone(self) -> NtiNetZone | None:
        """Return the freshest snapshot of this zone."""
        plant = self.plant
        if plant is None:
            return None
        return plant.zones.get(self._zone_number)

    @property
    def current_temperature(self) -> float | None:
        """Return the measured room temperature."""
        zone = self._zone
        return zone.room_temp if zone else None

    @property
    def target_temperature(self) -> float | None:
        """Return the active setpoint."""
        zone = self._zone
        return zone.target_temp if zone else None

    @property
    def target_temperature_step(self) -> float:
        """Return the setpoint increment the controller accepts."""
        zone = self._zone
        return zone.step if zone else 0.5

    @property
    def min_temp(self) -> float:
        """Return the lowest selectable setpoint."""
        zone = self._zone
        return zone.min_temp if zone else 10.0

    @property
    def max_temp(self) -> float:
        """Return the highest selectable setpoint."""
        zone = self._zone
        return zone.max_temp if zone else 30.0

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        zone = self._zone
        if zone is None or zone.mode is None:
            return HVACMode.OFF
        return ZONE_TO_HA_MODE.get(zone.mode, HVACMode.AUTO)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the modes this zone actually accepts."""
        zone = self._zone
        if zone is None or not zone.allowed_modes:
            return [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
        modes: list[HVACMode] = []
        for mode in (ZoneMode.OFF, ZoneMode.MANUAL, ZoneMode.TIME_PROGRAM):
            if mode in zone.allowed_modes:
                modes.append(ZONE_TO_HA_MODE[mode])
        return modes or [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]

    @property
    def hvac_action(self) -> HVACAction:
        """Return what the zone is doing right now."""
        zone = self._zone
        if zone is None or zone.mode == ZoneMode.OFF:
            return HVACAction.OFF
        if zone.heat_request:
            return HVACAction.HEATING
        return HVACAction.IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Apply a new setpoint.

        The service always writes the comfort and reduced setpoints together,
        so the untouched half is echoed back from the last reading.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        zone = self._zone
        if temperature is None or zone is None:
            return
        comfort = float(temperature)
        reduced = zone.reduced_temp
        if reduced is None:
            reduced = comfort
        try:
            await self.coordinator.client.async_set_zone_temperatures(
                self._plant_id,
                self._zone_number,
                comfort=comfort,
                reduced=float(reduced),
                old_comfort=zone.comfort_temp,
                old_reduced=zone.reduced_temp,
            )
        except NtiNetError as err:
            raise HomeAssistantError(f"Could not set zone temperature: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Apply a new HVAC mode."""
        zone = self._zone
        if zone is None:
            return
        new_mode = HA_TO_ZONE_MODE.get(hvac_mode)
        if new_mode is None:
            return
        if zone.allowed_modes and new_mode not in zone.allowed_modes:
            raise HomeAssistantError(
                f"Zone {self._zone_number} does not accept mode {hvac_mode}"
            )
        try:
            await self.coordinator.client.async_set_zone_mode(
                self._plant_id,
                self._zone_number,
                new=new_mode,
                old=zone.mode,
            )
        except NtiNetError as err:
            raise HomeAssistantError(f"Could not set zone mode: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        """Turn the zone on in manual mode."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Turn the zone off."""
        await self.async_set_hvac_mode(HVACMode.OFF)
