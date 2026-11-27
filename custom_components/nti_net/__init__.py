"""The NTI net integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import (
    NtiNetApiClient,
    NtiNetAuthError,
    NtiNetError,
    NtiNetRateLimitError,
)
from .const import (
    ATTR_TEMPERATURE,
    ATTR_UNTIL,
    CONF_API_URL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_API_URL,
    DEFAULT_OVERRIDE_HOURS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_CLEAR_OVERRIDE,
    SERVICE_SET_HOLIDAY,
    SERVICE_SET_OVERRIDE,
    WRITE_SETTLE_SECONDS,
)
from .coordinator import NtiNetCoordinator, NtiNetZone

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.WATER_HEATER,
]

type NtiNetConfigEntry = ConfigEntry[NtiNetCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: NtiNetConfigEntry) -> bool:
    """Set up NTI net from a config entry."""
    session = async_get_clientsession(hass)
    client = NtiNetApiClient(
        session=session,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        api_url=entry.data.get(CONF_API_URL, DEFAULT_API_URL),
    )

    try:
        await client.async_login()
    except NtiNetRateLimitError as err:
        raise ConfigEntryNotReady(
            f"NTI net is throttling this account, retry in {err.retry_after}s"
        ) from err
    except NtiNetAuthError as err:
        # Credentials no longer work: surface a repair-style reauth prompt
        # instead of retrying forever with a password the cloud rejects.
        entry.async_start_reauth(hass)
        raise ConfigEntryNotReady(f"Authentication failed: {err}") from err
    except NtiNetError as err:
        raise ConfigEntryNotReady(f"Cannot reach NTI net: {err}") from err

    coordinator = NtiNetCoordinator(
        hass,
        entry,
        client,
        scan_interval=entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        ),
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await _async_register_services(hass)
    return True


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register the account-wide action services once.

    These act on a gateway, not on an entity, because the cloud exposes no entity
    that reads them back: holiday mode cannot be observed at all, and a zone
    override is only visible as ``zones.N.der`` inside the aggregated read.
    Registering them for every config entry would double-register on a
    multi-account install, so only the first entry does it.
    """
    if hass.services.has_service(DOMAIN, SERVICE_SET_OVERRIDE):
        return

    async def _async_resolve_plants(
        call: ServiceCall,
    ) -> list[tuple[NtiNetConfigEntry, Any]]:
        """Return (entry, plant) pairs targeted by an entity id or gateway id."""
        gateway = call.data.get("gateway")
        targets: list[Any] = []
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coordinator = entry.runtime_data
            if gateway:
                plant = coordinator.data.get(gateway)
                if plant is not None:
                    targets.append((entry, plant))
            else:
                for entity_id in call.data[ATTR_ENTITY_ID]:
                    registry_entry = er.async_get(hass).async_get(entity_id)
                    if registry_entry is None or registry_entry.platform != DOMAIN:
                        raise HomeAssistantError(
                            f"{entity_id} is not an NTI net entity"
                        )
                    plant = coordinator.data.get(str(registry_entry.unique_id).split("_")[0])
                    if plant is not None and (entry, plant) not in targets:
                        targets.append((entry, plant))
        if not targets:
            raise HomeAssistantError("No matching NTI net gateway was found")
        return targets

    async def _async_refresh_after_write(coordinator: NtiNetCoordinator) -> None:
        """Refresh now, then once more shortly after.

        The gateway applies a write asynchronously: an immediate re-read still
        returns the previous value, which would leave an override sensor stale
        for a whole poll interval.  One extra read a few seconds later is
        bounded and costs a single request per user action.
        """
        await coordinator.async_request_refresh()
        await asyncio.sleep(WRITE_SETTLE_SECONDS)
        await coordinator.async_request_refresh()

    async def _async_set_override(call: ServiceCall) -> None:
        """Put a zone into a timed manual override."""
        zone_number = int(call.data["zone"])
        temperature = float(call.data[ATTR_TEMPERATURE])
        until = call.data.get(ATTR_UNTIL)
        for entry, plant in await _async_resolve_plants(call):
            zone = plant.zones.get(zone_number)
            if zone is None:
                raise HomeAssistantError(
                    f"Gateway {plant.gateway_id} has no zone {zone_number}"
                )
            expiry = until.isoformat() if until else _default_override_expiry(zone)
            try:
                await entry.runtime_data.client.async_set_zone_override(
                    plant.gateway_id,
                    zone_number,
                    temperature=temperature,
                    until=expiry,
                    old={
                        "temp": zone.deroga_temp,
                        "exp": zone.deroga_until,
                        "enab": zone.deroga_enabled,
                    },
                )
            except NtiNetError as err:
                raise HomeAssistantError(f"Could not set override: {err}") from err
            await _async_refresh_after_write(entry.runtime_data)

    async def _async_clear_override(call: ServiceCall) -> None:
        """Cancel a zone override."""
        zone_number = int(call.data["zone"])
        for entry, plant in await _async_resolve_plants(call):
            zone = plant.zones.get(zone_number)
            if zone is None:
                raise HomeAssistantError(
                    f"Gateway {plant.gateway_id} has no zone {zone_number}"
                )
            try:
                await entry.runtime_data.client.async_set_zone_override(
                    plant.gateway_id,
                    zone_number,
                    temperature=zone.deroga_temp or 0.0,
                    until=zone.deroga_until,
                    enabled=False,
                    old={
                        "temp": zone.deroga_temp,
                        "exp": zone.deroga_until,
                        "enab": zone.deroga_enabled,
                    },
                )
            except NtiNetError as err:
                raise HomeAssistantError(f"Could not clear override: {err}") from err
            await _async_refresh_after_write(entry.runtime_data)

    async def _async_set_holiday(call: ServiceCall) -> None:
        """Enable or clear holiday mode on a gateway."""
        until = call.data.get(ATTR_UNTIL)
        gateway = call.data.get("gateway")
        entries = [
            e for e in hass.config_entries.async_loaded_entries(DOMAIN)
            if not gateway or gateway in e.runtime_data.data
        ]
        if not entries:
            raise HomeAssistantError("No matching NTI net gateway was found")
        for entry in entries:
            gateways = [gateway] if gateway else list(entry.runtime_data.data)
            for gw in gateways:
                try:
                    await entry.runtime_data.client.async_set_holiday(
                        gw, until.isoformat() if until else None
                    )
                except NtiNetError as err:
                    raise HomeAssistantError(
                        f"Could not set holiday mode: {err}"
                    ) from err
            await _async_refresh_after_write(entry.runtime_data)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_OVERRIDE,
        _async_set_override,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
                vol.Required("zone"): cv.positive_int,
                vol.Required(ATTR_TEMPERATURE): vol.Coerce(float),
                vol.Optional(ATTR_UNTIL): cv.datetime,
                vol.Optional("gateway"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_OVERRIDE,
        _async_clear_override,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
                vol.Required("zone"): cv.positive_int,
                vol.Optional("gateway"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_HOLIDAY,
        _async_set_holiday,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_UNTIL): cv.datetime,
                vol.Optional("gateway"): cv.string,
            }
        ),
        supports_response=SupportsResponse.NONE,
    )


def _default_override_expiry(zone: NtiNetZone) -> str:
    """Return the expiry to send when the caller did not give one.

    A disabled override carries an epoch expiry, so echoing it would create an
    override that expires immediately. When no expiry is active, fall back to a
    sensible horizon that the user can still cancel explicitly.
    """
    if zone.deroga_enabled and zone.deroga_until:
        return zone.deroga_until
    return (dt_util.utcnow() + timedelta(hours=DEFAULT_OVERRIDE_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


async def _async_update_listener(
    hass: HomeAssistant, entry: NtiNetConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: NtiNetConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
