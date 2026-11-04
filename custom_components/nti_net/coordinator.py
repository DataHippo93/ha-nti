"""Data update coordinator for the NTI net integration.

The cloud's aggregated read is ``GET remote/plantData/{gw}?umsys=si``.  Its
shape was verified against a live gateway (``gwSysType`` 3, firmware
``104.115.28B``)::

    {
      "gw": "0123456789AB",
      "mod":  {"allowedOptions": [0, 1, 2, 5], "value": 1},
      "dMod": {"allowedOptions": [0, 1, 2], "value": 2},
      "dComf": {"step": 1.0, "min": 40.0, "max": 65.0, "value": 53.0},
      "out": 4.0,
      "zones": {
        "1": {
          "mod":  {"allowedOptions": [2, 3], "value": 3},
          "room": 0.0,
          "desired": 19.0,
          "comf": {"step": 0.5, "min": 10.0, "max": 30.0, "value": 19.0},
          "econ": {"step": 0.5, "min": 10.0, "max": 30.0, "value": 16.0},
          "heat": false, "heatReqAir": false, "pilot": false,
          "der": {"temp": 0.0, "exp": "1970-01-01T00:00:00", "enab": false},
          "antiFreezeTemp": 5.0
        }
      },
      "auto": true, "flame": false, "dhwDisabled": false, "hpOn": false,
      "dhwStoreT": 55.0, "dhwBoiler": true,
      "dhwTProgComfortTemp": {"value": 53.0},
      "dhwTProgEconomyTemp": {"value": 30.0}
    }

Three details drive the code below:

* ``zones`` is a **dict keyed by the zone number as a string**, not an array.
* Most numbers are wrapped in ``{"value", "min", "max", "step"}`` objects.
  Only the raw scalars (``out``, ``dhwStoreT``, ``room``, ``desired``) are
  plain numbers.
* ``room`` reads ``0.0`` when the zone has no room sensor (the features
  payload flags this per zone with ``roomSens: false``), so a zero is reported
  as "unknown" rather than as a freezing room.

Everything is read in SI units because the client pins ``umsys=si`` on every
request; ``umsys=us`` would return Fahrenheit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NtiNetApiClient, NtiNetAuthError, NtiNetError, NtiNetRateLimitError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, MIN_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


@dataclass
class NtiNetZone:
    """One heating zone of one gateway."""

    number: int
    name: str
    room_temp: float | None = None
    desired_temp: float | None = None
    comfort_temp: float | None = None
    reduced_temp: float | None = None
    comfort_min: float = 10.0
    comfort_max: float = 30.0
    reduced_min: float = 10.0
    reduced_max: float = 30.0
    step: float = 0.5
    mode: int | None = None
    allowed_modes: list[int] = field(default_factory=list)
    heat_request: bool | None = None
    deroga_temp: float | None = None
    deroga_until: str | None = None
    deroga_enabled: bool = False
    anti_freeze_temp: float | None = None
    pilot: bool | None = None

    @property
    def target_temp(self) -> float | None:
        """Return the setpoint Home Assistant should show."""
        return self.desired_temp if self.desired_temp is not None else self.comfort_temp

    @property
    def override_active(self) -> bool:
        """Return whether a manual override is in force right now.

        The cloud reports a disabled override with an expiry of
        ``1970-01-01T00:00:00``, so the flag is what counts, not the timestamp.
        """
        return bool(self.deroga_enabled)

    @property
    def min_temp(self) -> float:
        """Return the lowest selectable setpoint."""
        return self.comfort_min

    @property
    def max_temp(self) -> float:
        """Return the highest selectable setpoint."""
        return self.comfort_max


@dataclass
class NtiNetPlant:
    """A gateway and everything read from it."""

    gateway_id: str
    name: str
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    # Plant level readings
    mode: int | None = None
    allowed_modes: list[int] = field(default_factory=list)
    outside_temp: float | None = None
    flame: bool | None = None
    heating_pump: bool | None = None
    dhw_temp: float | None = None
    dhw_setpoint: float | None = None
    dhw_reduced_temp: float | None = None
    dhw_mode_changeable: bool = True
    dhw_min: float = 40.0
    dhw_max: float = 65.0
    dhw_step: float = 1.0
    dhw_mode: int | None = None
    allowed_dhw_modes: list[int] = field(default_factory=list)
    dhw_disabled: bool | None = None
    is_online: bool | None = None
    # Children
    zones: dict[int, NtiNetZone] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_dhw(self) -> bool:
        """Return whether this appliance exposes domestic hot water."""
        if self.features.get("dhwBoilerPresent") is False:
            return False
        if self.features.get("dhwHidden") is True:
            return False
        return bool(self.raw.get("dhwBoiler")) or self.dhw_mode is not None

    @property
    def override_active(self) -> bool:
        """Return whether any zone has a manual override in force."""
        return any(zone.override_active for zone in self.zones.values())


class NtiNetCoordinator(DataUpdateCoordinator[dict[str, NtiNetPlant]]):
    """Poll every gateway on the account and expose flattened plant objects."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: NtiNetApiClient,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=max(scan_interval, MIN_SCAN_INTERVAL)),
            config_entry=entry,
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, NtiNetPlant]:
        """Fetch every plant, then the detail for each one."""
        try:
            plants = await self.client.async_get_plants()
        except NtiNetRateLimitError as err:
            raise UpdateFailed(
                f"NTI net is throttling this account; retry in {err.retry_after}s"
            ) from err
        except NtiNetAuthError as err:
            # The stored password is no longer accepted; ask the user for a new
            # one rather than polling forever with credentials that cannot work.
            self.config_entry.async_start_reauth(self.hass)
            raise UpdateFailed(f"NTI net rejected the stored credentials: {err}") from err
        except NtiNetError as err:
            raise UpdateFailed(f"Cannot read NTI net account: {err}") from err

        result: dict[str, NtiNetPlant] = {}

        for summary in plants:
            gateway_id = (
                summary.get("gwId")
                or summary.get("gatewayId")
                or summary.get("id")
            )
            if not gateway_id:
                _LOGGER.debug("Skipping plant without identifier: %s", summary)
                continue

            plant = NtiNetPlant(
                gateway_id=str(gateway_id),
                name=str(
                    summary.get("plantName")
                    or summary.get("name")
                    or f"NTI {gateway_id}"
                ),
                serial=summary.get("gwSerial"),
                firmware=summary.get("gwFwVer"),
                model=_system_type_name(summary.get("gwSysType")),
            )

            try:
                await self._async_enrich_plant(plant)
            except NtiNetRateLimitError as err:
                raise UpdateFailed(
                    f"NTI net is throttling this account; retry in {err.retry_after}s"
                ) from err
            except NtiNetError as err:
                # Keep the plant visible but mark it offline rather than
                # failing the whole account.
                _LOGGER.debug(
                    "Could not read plant %s: %s", plant.gateway_id, err
                )
                plant.is_online = False

            result[plant.gateway_id] = plant

        return result

    async def _async_enrich_plant(self, plant: NtiNetPlant) -> None:
        """Read the detail views for one gateway and flatten them."""
        gateway_id = plant.gateway_id

        plant.features = await self.client.async_get_features(gateway_id)

        info = await self.client.async_get_gateway_info(gateway_id)
        plant.is_online = _as_bool(info.get("isOnline"), default=True)

        data = await self.client.async_get_plant_data(gateway_id)
        if not data:
            # Boiler controllers that do not answer the generic view speak BSB.
            data = await self.client.async_get_bsb_plant_data(gateway_id)
            if data:
                plant.raw = {"bsb": data}
                self._apply_bsb(plant, data)
                return
            plant.is_online = False
            return

        plant.raw = data
        self._apply_plant(plant, data)

    def _apply_plant(self, plant: NtiNetPlant, data: dict[str, Any]) -> None:
        """Copy plant readings out of the generic ``plantData`` envelope."""
        mode = _wrapped(data.get("mod"))
        plant.mode = _as_int(mode.get("value"))
        plant.allowed_modes = _as_int_list(mode.get("allowedOptions"))

        dhw_mode = _wrapped(data.get("dMod"))
        plant.dhw_mode = _as_int(dhw_mode.get("value"))
        plant.allowed_dhw_modes = _as_int_list(dhw_mode.get("allowedOptions"))
        # The cloud says whether the mode may be changed at all; when it says
        # no, the water heater must not offer a mode selector. Some firmware
        # reports the inverse flag instead, so honour whichever is present.
        not_changeable = _as_bool(data.get("dhwModeNotChangeable"))
        changeable = _as_bool(plant.features.get("dhwModeChangeable"))
        if not_changeable is not None:
            plant.dhw_mode_changeable = not not_changeable
        elif changeable is not None:
            plant.dhw_mode_changeable = changeable

        comfort = _wrapped(data.get("dComf"))
        plant.dhw_setpoint = _as_float(comfort.get("value"))
        plant.dhw_min = _as_float(comfort.get("min"), 40.0) or 40.0
        plant.dhw_max = _as_float(comfort.get("max"), 65.0) or 65.0
        plant.dhw_step = _as_float(comfort.get("step"), 1.0) or 1.0

        economy = _wrapped(data.get("dhwTProgEconomyTemp"))
        plant.dhw_reduced_temp = _as_float(economy.get("value"))

        plant.outside_temp = _as_float(data.get("out"))
        plant.flame = _as_bool(data.get("flame"))
        plant.heating_pump = _as_bool(data.get("hpOn"))
        plant.dhw_temp = _as_float(data.get("dhwStoreT"))
        plant.dhw_disabled = _as_bool(data.get("dhwDisabled"))

        room_sensors = _zone_feature_map(plant.features)
        zones = data.get("zones")
        if isinstance(zones, dict):
            for key, raw_zone in zones.items():
                if not isinstance(raw_zone, dict):
                    continue
                number = _as_int(key)
                if number is None:
                    continue
                self._apply_zone(plant, number, raw_zone, room_sensors)

    def _apply_bsb(self, plant: NtiNetPlant, data: dict[str, Any]) -> None:
        """Flatten the BSB-family envelope (fallback for BSB controllers)."""
        plant.mode = _as_int(data.get("mode"))
        plant.outside_temp = _as_float(data.get("outTemp"))
        plant.flame = _as_bool(data.get("flame"))
        plant.heating_pump = _as_bool(data.get("hpOn"))
        plant.dhw_temp = _as_float(data.get("dhwTemp"))
        plant.dhw_setpoint = _as_float(data.get("dhwComfTemp"))
        plant.dhw_reduced_temp = _as_float(data.get("dhwReduTemp"))
        plant.dhw_mode = _as_int(data.get("dhwMode"))

        zones = data.get("zones")
        if isinstance(zones, list):
            for index, raw_zone in enumerate(zones, start=1):
                if not isinstance(raw_zone, dict):
                    continue
                number = _as_int(raw_zone.get("num")) or index
                self._apply_zone(plant, number, raw_zone, {}, bsb=True)

    def _apply_zone(
        self,
        plant: NtiNetPlant,
        number: int,
        raw_zone: dict[str, Any],
        room_sensors: dict[int, dict[str, Any]],
        *,
        bsb: bool = False,
    ) -> None:
        """Flatten one raw zone into ``NtiNetZone``."""
        features = room_sensors.get(number, {})
        if features.get("isHidden") is True:
            return

        mode = _wrapped(raw_zone.get("mod"))
        comfort = _wrapped(raw_zone.get("comf"))
        economy = _wrapped(raw_zone.get("econ"))
        deroga = _wrapped(raw_zone.get("der"))

        name = str(features.get("name") or raw_zone.get("name") or "")
        zone = NtiNetZone(
            number=number,
            name=name or f"Zone {number}",
            mode=_as_int(mode.get("value")) if not bsb else _as_int(raw_zone.get("chMode")),
            allowed_modes=_as_int_list(mode.get("allowedOptions")),
            comfort_temp=_as_float(comfort.get("value")),
            reduced_temp=_as_float(economy.get("value")),
            desired_temp=_as_float(raw_zone.get("desired")),
            heat_request=_as_bool(raw_zone.get("heat")),
            anti_freeze_temp=_as_float(raw_zone.get("antiFreezeTemp")),
            pilot=_as_bool(raw_zone.get("pilot")),
            deroga_temp=_as_float(deroga.get("temp")),
            deroga_until=_as_str(deroga.get("exp")),
            deroga_enabled=_as_bool(deroga.get("enab"), default=False) or False,
        )

        if bsb:
            zone.comfort_temp = _as_float(raw_zone.get("chComfortTemp"))
            zone.reduced_temp = _as_float(raw_zone.get("chReducedTemp"))
            zone.desired_temp = _as_float(raw_zone.get("desiredTemp"))
            zone.heat_request = _as_bool(raw_zone.get("chRequest"))
            zone.room_temp = _as_float(raw_zone.get("roomTemp"))
            # Store the zone before returning; an early return here used to
            # discard every BSB zone that had just been parsed.
            plant.zones[number] = zone
            return

        zone.comfort_min = _as_float(comfort.get("min"), 10.0) or 10.0
        zone.comfort_max = _as_float(comfort.get("max"), 30.0) or 30.0
        zone.reduced_min = _as_float(economy.get("min"), 10.0) or 10.0
        zone.reduced_max = _as_float(economy.get("max"), 30.0) or 30.0
        zone.step = _as_float(comfort.get("step"), 0.5) or 0.5

        room = _as_float(raw_zone.get("room"))
        # A zone with no room sensor reports 0.0; do not surface that as 0 °C.
        if room is not None and (room > 0.0 or features.get("roomSens") is True):
            zone.room_temp = room

        plant.zones[number] = zone


def _system_type_name(system_type: Any) -> str | None:
    """Render the numeric gateway system type.

    The binary exposes the ``gwSysType`` field but no enumeration of its
    values, so the raw number is reported rather than a guessed family name.
    """
    value = _as_int(system_type)
    return f"NTI appliance (type {value})" if value is not None else None


def _zone_feature_map(features: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Index the per-zone feature entries by zone number."""
    result: dict[int, dict[str, Any]] = {}
    zones = features.get("zones")
    if isinstance(zones, list):
        for entry in zones:
            if not isinstance(entry, dict):
                continue
            number = _as_int(entry.get("num"))
            if number is not None:
                result[number] = entry
    return result


def _wrapped(value: Any) -> dict[str, Any]:
    """Return a `{value,min,max,step}` wrapper, or an empty dict."""
    return value if isinstance(value, dict) else {}


def _as_float(value: Any, default: float | None = None) -> float | None:
    """Convert an API number to float, tolerating strings."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int | None:
    """Convert an API number to int, tolerating strings."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_int_list(value: Any) -> list[int]:
    """Convert an API list of numbers to a list of ints."""
    if not isinstance(value, list):
        return []
    return [number for item in value if (number := _as_int(item)) is not None]


def _as_str(value: Any) -> str | None:
    """Convert an API value to a non-empty string."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _as_bool(value: Any, default: bool | None = None) -> bool | None:
    """Convert an API flag to bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "on", "yes"):
            return True
        if lowered in ("false", "0", "off", "no"):
            return False
    return default
