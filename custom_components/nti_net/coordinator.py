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
import time
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import NtiNetApiClient, NtiNetAuthError, NtiNetError, NtiNetRateLimitError
from .const import (
    DATA_ITEM_AUTO_THERMOREG,
    DATA_ITEM_DHW_MODE,
    DATA_ITEM_FLOW_SETPOINT,
    DATA_ITEM_FLOW_TEMP,
    DATA_ITEM_PRESSURE,
    DEFAULT_SCAN_INTERVAL,
    DIAGNOSTIC_DATA_ITEMS,
    DIAGNOSTIC_MENU_ITEMS,
    DOMAIN,
    GAS_USAGE_DHW,
    GAS_USAGE_HEATING,
    MENU_ITEM_RETURN_TEMP,
    MENU_ITEM_SIGNAL_LEVEL,
    MIN_SCAN_INTERVAL,
    SEQUENCE_PERIOD_DAILY,
    SEQUENCE_PERIOD_MONTHLY,
)

_LOGGER = logging.getLogger(__name__)

#: Consumption changes slowly, so it is refreshed on its own slower cadence.
#: Polling the report route every scan interval would cost an extra request per
#: gateway per poll for data that moves once a day.
CONSUMPTION_REFRESH_SECONDS: Final = 3600

#: How long to leave the extra reads alone after a 429 that names no delay.
DIAGNOSTICS_BACKOFF_SECONDS: Final = 300

#: Keys that identify the gateway or its owner.  They are stripped from the
#: fault payload kept on the sensor, which ends up in the recorder.
_FAULT_PRIVATE_KEYS: Final = frozenset(
    {"gw", "gwId", "gatewayId", "gwSerial", "sn", "plantId", "mac", "macAddress"}
)

#: A fault list longer than this is cut, to keep the attributes small.
MAX_FAULTS: Final = 10


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
    # Metered gas, all in kWh (the cloud reports this route in kWh)
    gas_last_month: float | None = None
    gas_heating_last_month: float | None = None
    gas_dhw_last_month: float | None = None
    gas_this_month: float | None = None
    gas_this_month_heating: float | None = None
    gas_this_month_dhw: float | None = None
    gas_yesterday: float | None = None
    # Diagnostics read from the data-item and service-menu routes
    flow_temp: float | None = None
    flow_setpoint: float | None = None
    return_temp: float | None = None
    pressure: float | None = None
    signal_level: float | None = None
    auto_thermoreg: bool | None = None
    #: Hot water mode read from the ``DhwMode`` data item.  ``None`` means it
    #: was not read this poll, so the hot water switch reports "unknown"
    #: rather than trusting the aggregated read (see ``DATA_ITEM_DHW_MODE``).
    dhw_mode_item: int | None = None
    #: Active faults.  ``None`` means "could not be read", which is not the
    #: same as an empty list ("read fine, nothing wrong").
    faults: list[dict[str, Any]] | None = None
    # Children
    zones: dict[int, NtiNetZone] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_fault(self) -> bool | None:
        """Return whether a fault is active, or None when that is unknown."""
        if self.faults is None:
            return None
        return bool(self.faults)

    @property
    def blocking_fault(self) -> bool | None:
        """Return whether an active fault is a lockout that needs a reset."""
        if self.faults is None:
            return None
        return any(fault.get("blocking") is True for fault in self.faults)

    @property
    def has_metering(self) -> bool:
        """Return whether the appliance reports consumption at all."""
        return _as_bool(self.features.get("hasMetering"), default=False) is True

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
        self._consumption_cache: dict[str, dict[str, float | None]] = {}
        self._diagnostics_blocked_until: dict[str, float] = {}
        self._fault_read_failing: set[str] = set()
        self._consumption_fetched_at: dict[str, float] = {}

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
                await self._async_apply_consumption(plant)
                return
            plant.is_online = False
            return

        plant.raw = data
        self._apply_plant(plant, data)
        await self._async_apply_diagnostics(plant)
        await self._async_apply_consumption(plant)

    async def _async_apply_diagnostics(self, plant: NtiNetPlant) -> None:
        """Read the faults and the service readings for one gateway.

        These are three extra requests on top of the main read, and none of
        them may fail the poll: the climate and hot water entities come from
        the main read and must stay usable whatever happens here.

        Throttling is handled the way the consumption read handles it.  The
        remaining requests of this poll are skipped, and the diagnostics are
        left alone until the delay the cloud asked for has passed, so an
        account that is already blocked is not sent more requests.

        A fault read that fails or is skipped leaves ``faults`` as ``None`` so
        the fault sensors report "unknown".  Reporting "OK" there would hide
        the very thing these sensors exist to catch.
        """
        gateway_id = plant.gateway_id
        blocked = _state(self, "_diagnostics_blocked_until", dict)
        failing = _state(self, "_fault_read_failing", set)

        if time.monotonic() < blocked.get(gateway_id, 0.0):
            return

        try:
            plant.faults = [
                _parse_fault(entry)
                for entry in await self.client.async_get_bus_errors(gateway_id)
            ]
            failing.discard(gateway_id)
        except NtiNetRateLimitError as err:
            self._defer_diagnostics(gateway_id, err)
            return
        except NtiNetError as err:
            plant.faults = None
            # Say so once, loudly: a fault sensor stuck on "unknown" with only
            # a debug line behind it is easy to miss.
            if gateway_id not in failing:
                failing.add(gateway_id)
                _LOGGER.warning(
                    "Cannot read faults for %s, the fault sensors will be "
                    "unknown until this recovers: %s",
                    plant.name,
                    err,
                )

        try:
            items = await self.client.async_get_data_items(
                gateway_id,
                [{"id": name, "zn": 0} for name in DIAGNOSTIC_DATA_ITEMS],
                plant.features,
            )
        except NtiNetRateLimitError as err:
            self._defer_diagnostics(gateway_id, err)
            return
        except NtiNetError as err:
            _LOGGER.debug("No data items for %s: %s", gateway_id, err)
            items = []
        values = _item_values(items, "id")
        plant.flow_setpoint = _as_float(values.get(DATA_ITEM_FLOW_SETPOINT))
        plant.flow_temp = _as_float(values.get(DATA_ITEM_FLOW_TEMP))
        plant.pressure = _as_float(values.get(DATA_ITEM_PRESSURE))
        plant.auto_thermoreg = _as_bool(values.get(DATA_ITEM_AUTO_THERMOREG))
        plant.dhw_mode_item = _as_int(values.get(DATA_ITEM_DHW_MODE))
        if plant.dhw_mode_item is not None and not plant.allowed_dhw_modes:
            # The aggregated read gave no usable mode (a combi reports an
            # empty option list and a constant 0), so the data item wins.
            plant.dhw_mode = plant.dhw_mode_item

        try:
            menu = await self.client.async_get_menu_items(
                gateway_id, DIAGNOSTIC_MENU_ITEMS
            )
        except NtiNetRateLimitError as err:
            self._defer_diagnostics(gateway_id, err)
            return
        except NtiNetError as err:
            _LOGGER.debug("No menu items for %s: %s", gateway_id, err)
            menu = []
        menu_values = _item_values(menu, "id")
        plant.return_temp = _as_float(menu_values.get(MENU_ITEM_RETURN_TEMP))
        plant.signal_level = _as_float(menu_values.get(MENU_ITEM_SIGNAL_LEVEL))

    def _defer_diagnostics(
        self, gateway_id: str, err: NtiNetRateLimitError
    ) -> None:
        """Hold the extra reads off until the cloud's block has expired."""
        delay = err.retry_after or DIAGNOSTICS_BACKOFF_SECONDS
        _state(self, "_diagnostics_blocked_until", dict)[gateway_id] = (
            time.monotonic() + delay
        )
        _LOGGER.debug(
            "Diagnostics for %s deferred, throttled for %ss", gateway_id, delay
        )

    async def _async_apply_consumption(self, plant: NtiNetPlant) -> None:
        """Fill in the metered gas readings, if this gateway meters.

        The report route is expensive and slow-moving, so it is only read when
        the appliance advertises metering, and at most once per
        ``CONSUMPTION_REFRESH_SECONDS``.  A failure here must never fail the
        poll: the rest of the device is still perfectly usable, and this is an
        extra request on top of the main read.
        """
        if not plant.has_metering:
            return
        now = time.monotonic()
        last = self._consumption_fetched_at.get(plant.gateway_id)
        if last is not None and now - last < CONSUMPTION_REFRESH_SECONDS:
            self._restore_consumption(plant)
            return

        try:
            blocks = await self.client.async_get_consumption_sequences(
                plant.gateway_id
            )
        except NtiNetRateLimitError as err:
            # The account is throttled.  This route is secondary, so keep the
            # device usable with the last known figures and wait a full cycle
            # before asking again rather than adding to the throttling.
            _LOGGER.debug(
                "Consumption for %s deferred, throttled for %ss",
                plant.gateway_id,
                err.retry_after,
            )
            self._consumption_fetched_at[plant.gateway_id] = now
            self._restore_consumption(plant)
            return
        except NtiNetError as err:
            _LOGGER.debug(
                "No consumption data for %s: %s", plant.gateway_id, err
            )
            self._restore_consumption(plant)
            return

        readings = _parse_consumption(blocks)
        if readings:
            self._consumption_cache[plant.gateway_id] = readings
            self._consumption_fetched_at[plant.gateway_id] = now
            for attribute, value in readings.items():
                setattr(plant, attribute, value)

    def _restore_consumption(self, plant: NtiNetPlant) -> None:
        """Re-apply the last successful readings for one gateway."""
        previous = self._consumption_cache.get(plant.gateway_id)
        if previous is None:
            return
        for attribute, value in previous.items():
            setattr(plant, attribute, value)

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


def _item_values(items: list[dict[str, Any]], key: str) -> dict[Any, Any]:
    """Index data-item or menu-item answers by id, skipping unusable ones.

    Both routes flag a reading they could not take with ``error`` (and the
    data-item route also with ``invalid``); such a value is dropped so the
    sensor reports "unknown" instead of a stale or meaningless number.
    """
    result: dict[Any, Any] = {}
    for item in items:
        if item.get("error") is True or item.get("invalid") is True:
            continue
        identifier = item.get(key)
        if identifier is None or "value" not in item:
            continue
        result[identifier] = item["value"]
    return result


def _state(coordinator: Any, name: str, factory: Any) -> Any:
    """Return a per-coordinator bookkeeping container, creating it on demand.

    The parser tests build a coordinator without running ``__init__``, so the
    containers cannot be assumed to exist.
    """
    value = getattr(coordinator, name, None)
    if value is None:
        value = factory()
        setattr(coordinator, name, value)
    return value


def _parse_fault(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalise one ``busErrors`` entry.

    The route was only ever observed empty on the reference gateway, so the
    field names are taken from the shared Ariston/remotethermo payload
    (``code``, ``fault``, ``errDex``, ``blk``, ``res``, ``timestamp``) and read
    defensively.  The entry is also kept under ``raw``, minus the keys that
    identify the gateway, so that a field this guess misses is still visible
    on the sensor the first time a real fault arrives.
    """
    code = entry.get("code")
    if code in (None, ""):
        code = entry.get("fault")
    if code in (None, "") and isinstance(entry.get("value"), (str, int)):
        # An entry that was not an object at all: the value is all there is.
        code = entry.get("value")
    description = (
        entry.get("errDex")
        or entry.get("description")
        or entry.get("text")
        or entry.get("message")
    )
    return {
        "code": str(code) if code not in (None, "") else None,
        "description": _as_str(description),
        "blocking": _as_bool(entry.get("blk")),
        "reset": _as_str(entry.get("res")),
        "timestamp": _as_str(entry.get("timestamp")),
        "raw": {
            key: value
            for key, value in entry.items()
            if key not in _FAULT_PRIVATE_KEYS
        },
    }


def _parse_consumption(
    blocks: list[dict[str, Any]],
) -> dict[str, float | None]:
    """Turn ``consSequencesApi8`` blocks into gas readings, in kWh.

    Only gas is extracted.  The electricity members exist but a boiler gateway
    reports zero for them, and a sensor that is always zero is worse than no
    sensor at all.

    The two period lengths that matter:

    * ``p = 4`` is **monthly**, one entry per calendar month, oldest first.
      The last completed month's entry is the running total the app shows in
      its energy breakdown, and it matches ``energyAccount``'s ``gas`` to
      within rounding.
    * ``p = 3`` is **daily**, one entry per day, oldest first.  The last entry
      is yesterday.  Its trailing days sum to the monthly bucket, which is how
      the mapping was confirmed.

    Both are totals per bucket, not cumulative meters.  The month-to-date
    readings are therefore derived from the daily series, which is the only
    source that includes days of the current month.
    """
    daily: dict[int, list[float]] = {}
    monthly: dict[int, list[float]] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = _as_int(block.get("k"))
        period = _as_int(block.get("p"))
        values = block.get("v")
        if kind is None or period is None or not isinstance(values, list):
            continue
        cleaned = [
            number for item in values if (number := _as_float(item)) is not None
        ]
        if not cleaned:
            continue
        if period == SEQUENCE_PERIOD_DAILY:
            daily[kind] = cleaned
        elif period == SEQUENCE_PERIOD_MONTHLY:
            monthly[kind] = cleaned

    heating_month = _last(monthly.get(GAS_USAGE_HEATING))
    dhw_month = _last(monthly.get(GAS_USAGE_DHW))
    heating_day = _last(daily.get(GAS_USAGE_HEATING))
    dhw_day = _last(daily.get(GAS_USAGE_DHW))

    to_date = _month_to_date(daily)

    return {
        # Last completed calendar month, matching the app's energy screen.
        "gas_heating_last_month": heating_month,
        "gas_dhw_last_month": dhw_month,
        "gas_last_month": _add(heating_month, dhw_month),
        # Month to date, which is what a running sensor should expose.
        "gas_this_month_heating": to_date.get(GAS_USAGE_HEATING),
        "gas_this_month_dhw": to_date.get(GAS_USAGE_DHW),
        "gas_this_month": _add(
            to_date.get(GAS_USAGE_HEATING), to_date.get(GAS_USAGE_DHW)
        ),
        # The most recent complete day (yesterday).
        "gas_yesterday": _add(heating_day, dhw_day),
    }


def _last(values: list[float] | None) -> float | None:
    """Return the newest entry of a series, rounded."""
    if not values:
        return None
    return round(values[-1], 3)


def _add(first: float | None, second: float | None) -> float | None:
    """Add two optional readings."""
    if first is None and second is None:
        return None
    return round((first or 0.0) + (second or 0.0), 3)


def _month_to_date(daily: dict[int, list[float]]) -> dict[int, float]:
    """Sum the part of each daily series that belongs to the current month.

    The daily series ends yesterday and reaches back into the previous month,
    so the split is taken from today's date rather than a fixed offset: the
    last ``day - 1`` entries are the current month's completed days.

    On the first of the month no day of the current month has completed yet,
    which is a genuine zero rather than missing data, so the series that are
    present report ``0.0``.
    """
    days = dt_util.now().date().day - 1
    if days <= 0:
        # The first of the month: nothing has accrued in it yet.  Reporting
        # zero keeps the running total from flickering to `unknown` and back.
        return {kind: 0.0 for kind in daily}
    result: dict[int, float] = {}
    for kind, values in daily.items():
        if len(values) < days:
            continue
        result[kind] = round(sum(values[-days:]), 3)
    return result


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
