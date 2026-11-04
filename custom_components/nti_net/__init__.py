"""The NTI net integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    NtiNetApiClient,
    NtiNetAuthError,
    NtiNetError,
    NtiNetRateLimitError,
)
from .const import (
    CONF_API_URL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_API_URL,
    DEFAULT_SCAN_INTERVAL,
)
from .coordinator import NtiNetCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
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
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: NtiNetConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: NtiNetConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
