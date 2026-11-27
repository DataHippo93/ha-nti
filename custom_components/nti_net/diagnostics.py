"""Diagnostics support for the NTI net integration.

Home Assistant exposes this through *Settings → Devices & Services → NTI net →
Download diagnostics*.  The dump is what makes a bug report actionable without
asking the user to re-run protocol probes by hand.

The redaction is deliberate: the account e-mail, the password and the gateway
serial identify a real household, so they are replaced with placeholders while
the payload shape (which is what matters for debugging) is preserved.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_API_URL
from .coordinator import NtiNetCoordinator

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    "title",
    "gw",
    "gwId",
    "gateway_id",
    "plantName",
    "gwSerial",
    "serial",
    "serial_number",
    "location",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one config entry."""
    coordinator: NtiNetCoordinator = entry.runtime_data

    plants: dict[str, Any] = {}
    for index, (gateway_id, plant) in enumerate(coordinator.data.items(), start=1):
        zones: dict[str, Any] = {}
        for number, zone in plant.zones.items():
            zones[str(number)] = {
                "name": zone.name,
                "mode": zone.mode,
                "allowed_modes": zone.allowed_modes,
                "room_temp": zone.room_temp,
                "desired_temp": zone.desired_temp,
                "comfort_temp": zone.comfort_temp,
                "reduced_temp": zone.reduced_temp,
                "comfort_min": zone.comfort_min,
                "comfort_max": zone.comfort_max,
                "step": zone.step,
                "heat_request": zone.heat_request,
                "override_enabled": zone.deroga_enabled,
                "override_temp": zone.deroga_temp,
                "override_until": zone.deroga_until,
            }
        # The gateway id is the dict key here, and redaction only rewrites
        # values, so use an opaque key instead of leaking it through the key.
        plants[f"plant_{index}"] = {
            "name": plant.name,
            "gateway_id": plant.gateway_id,
            "model": plant.model,
            "firmware": plant.firmware,
            "serial": plant.serial,
            "is_online": plant.is_online,
            "mode": plant.mode,
            "allowed_modes": plant.allowed_modes,
            "outside_temp": plant.outside_temp,
            "flame": plant.flame,
            "heating_pump": plant.heating_pump,
            "dhw_mode": plant.dhw_mode,
            "dhw_mode_changeable": plant.dhw_mode_changeable,
            "dhw_temp": plant.dhw_temp,
            "dhw_setpoint": plant.dhw_setpoint,
            "dhw_reduced_temp": plant.dhw_reduced_temp,
            "has_dhw": plant.has_dhw,
            "flow_temp": plant.flow_temp,
            "flow_setpoint": plant.flow_setpoint,
            "return_temp": plant.return_temp,
            "pressure": plant.pressure,
            "signal_level": plant.signal_level,
            "auto_thermoreg": plant.auto_thermoreg,
            "faults": plant.faults,
            "zones": zones,
            "features": async_redact_data(plant.features, TO_REDACT),
            "raw": async_redact_data(plant.raw, TO_REDACT),
        }

    return {
        "entry": async_redact_data(
            {
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
                "state": str(entry.state),
                "api_url": entry.data.get(CONF_API_URL),
            },
            TO_REDACT,
        ),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "last_exception": (
                str(coordinator.last_exception)
                if coordinator.last_exception
                else None
            ),
        },
        "plants": async_redact_data(plants, TO_REDACT),
    }
