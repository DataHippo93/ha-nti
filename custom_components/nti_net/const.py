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
#: * ``p`` -- the bucket size of the series: 1 = last 24 hours, 2 = last 7
#:   days, 3 = daily, 4 = monthly.
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
