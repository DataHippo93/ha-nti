"""Constants for the NTI net integration.

The enumerations below are the values the live service actually advertises in
its ``allowedOptions`` lists, confirmed against a gateway of ``gwSysType`` 3
plus the static evidence from the shipped iOS app (`com.remotethermo.nti`
7.0.3). See PROTOCOL.md.

Observed ``allowedOptions``:

* plant mode  -> ``[0, 1, 2, 5]``
* DHW mode    -> ``[0, 1, 2]``
* zone mode   -> ``[2, 3]``
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "nti_net"
MANUFACTURER: Final = "NTI"

CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_API_URL: Final = "api_url"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_GAS_UNIT: Final = "gas_unit"
CONF_GAS_KWH_PER_M3: Final = "gas_kwh_per_m3"

#: The meter reads output energy in kWh.  ``m³`` is offered as a display choice
#: because several utilities bill that way, but volume is not what the cloud
#: stores, so it is always derived from the kWh figure.
GAS_UNIT_KWH: Final = "kwh"
GAS_UNIT_M3: Final = "m3"

#: Energy content of natural gas, in kWh per cubic metre.
#:
#: 37.5 MJ/m³ ÷ 3.6 MJ/kWh ≈ 10.42.  This is a typical North American higher
#: heating value; real bills vary by a few percent with the gas's composition
#: and are quoted as a monthly conversion factor, so the user should copy the
#: figure from their own bill.  Never treat it as exact.
DEFAULT_GAS_KWH_PER_M3: Final = 10.42
MIN_GAS_KWH_PER_M3: Final = 5.0
MAX_GAS_KWH_PER_M3: Final = 20.0

ATTR_TEMPERATURE: Final = "temperature"
ATTR_UNTIL: Final = "until"

SERVICE_SET_OVERRIDE: Final = "set_zone_override"
SERVICE_CLEAR_OVERRIDE: Final = "clear_zone_override"
SERVICE_SET_HOLIDAY: Final = "set_holiday"

#: ``consSequencesApi8`` selects a series with these two ids:
#:
#: * ``k`` -- which usage the series is for.  The route only accepts the
#:   family filters ``Ch`` and ``Dhw``; the per-appliance members are then
#:   distinguished by ``k``.  ``Ch`` answers ``1`` (space heating total),
#:   ``7`` (space heating gas) and ``20`` (space heating electricity);
#:   ``Dhw`` answers ``2`` (DHW total), ``10`` (DHW gas) and ``21`` (DHW
#:   electricity).  ``ChBoilerGas`` and the heat-pump members answer HTTP 500
#:   on a boiler gateway, so the family filters are the way to ask.
#: * ``p`` -- the bucket size of the series: 2 = last 7 days, 3 = daily,
#:   4 = monthly.  ``p = 1`` (24 entries) is not decoded and is never read.
#:   The 24 values do not sum to a single day of the daily series, so it is
#:   not an hourly breakdown.
#:
#: The route is named ``consKwhSequences`` in the shipped binary, so the values
#: are **kWh** regardless of the account's display unit.
GAS_USAGE_HEATING: Final = 7
GAS_USAGE_DHW: Final = 10
SEQUENCE_PERIOD_DAILY: Final = 3
SEQUENCE_PERIOD_MONTHLY: Final = 4

#: How long the gateway takes to reflect a write in the aggregated read.
#: Measured against a live appliance: an immediate re-read still returns the old
#: value, so a short settle delay is needed before the confirming read.
WRITE_SETTLE_SECONDS: Final = 5

#: Horizon used when a caller sets an override without an expiry.
DEFAULT_OVERRIDE_HOURS: Final = 2

DEFAULT_API_URL: Final = "https://www.nti.remotethermo.com/api/v2/"

#: Cloud refuses fast polling; the vendor app is throttled per account.
DEFAULT_SCAN_INTERVAL: Final = 300
MIN_SCAN_INTERVAL: Final = 180


class PlantMode:
    """Plant (boiler) operating modes, as sent by the cloud."""

    SUMMER: Final = 0
    WINTER: Final = 1
    HEATING_ONLY: Final = 2
    COOLING: Final = 3
    COOLING_ONLY: Final = 4
    OFF: Final = 5
    HOLIDAY: Final = 6

    NAMES: Final = {
        SUMMER: "summer",
        WINTER: "winter",
        HEATING_ONLY: "heating_only",
        COOLING: "cooling",
        COOLING_ONLY: "cooling_only",
        OFF: "off",
        HOLIDAY: "holiday",
    }
    VALUES: Final = {name: value for value, name in NAMES.items()}


class ZoneMode:
    """Zone operating modes.

    Only ``2`` (manual) and ``3`` (time program) are offered on a standard
    boiler gateway; ``0`` and ``1`` are kept for completeness because other
    appliance families advertise them.
    """

    OFF: Final = 0
    MANUAL_NIGHT: Final = 1
    MANUAL: Final = 2
    TIME_PROGRAM: Final = 3

    NAMES: Final = {
        OFF: "off",
        MANUAL_NIGHT: "reduced",
        MANUAL: "manual",
        TIME_PROGRAM: "auto",
    }
    VALUES: Final = {name: value for value, name in NAMES.items()}


class DhwMode:
    """Domestic hot water modes."""

    DISABLED: Final = 0
    TIME_BASED: Final = 1
    ALWAYS_ACTIVE: Final = 2

    NAMES: Final = {
        DISABLED: "off",
        TIME_BASED: "eco",
        ALWAYS_ACTIVE: "comfort",
    }
    VALUES: Final = {name: value for value, name in NAMES.items()}


#: Named data items read on every poll for the diagnostic sensors.  All four
#: are answered by a boiler gateway of ``gwSysType`` 3; see PROTOCOL.md.
DATA_ITEM_FLOW_SETPOINT: Final = "ChFlowSetpointTemp"
DATA_ITEM_FLOW_TEMP: Final = "ChFlowTemp"
DATA_ITEM_PRESSURE: Final = "HeatingCircuitPressure"
DATA_ITEM_AUTO_THERMOREG: Final = "AutomaticThermoregulation"
DIAGNOSTIC_DATA_ITEMS: Final = (
    DATA_ITEM_FLOW_SETPOINT,
    DATA_ITEM_FLOW_TEMP,
    DATA_ITEM_PRESSURE,
    DATA_ITEM_AUTO_THERMOREG,
)

#: Service-menu ids read alongside them.
MENU_ITEM_RETURN_TEMP: Final = 124
MENU_ITEM_SIGNAL_LEVEL: Final = 119
DIAGNOSTIC_MENU_ITEMS: Final = (MENU_ITEM_RETURN_TEMP, MENU_ITEM_SIGNAL_LEVEL)

#: State of the fault sensor while the appliance reports nothing wrong.
FAULT_STATE_OK: Final = "OK"
