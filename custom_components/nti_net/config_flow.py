"""Config flow for the NTI net integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .api import (
    NtiNetApiClient,
    NtiNetAuthError,
    NtiNetConnectionError,
    NtiNetError,
    NtiNetRateLimitError,
)
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_API_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _scan_interval_schema(current: int) -> vol.Schema:
    """Build the options schema, clamped to the minimum safe interval."""
    return vol.Schema(
        {
            vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_SCAN_INTERVAL,
                    max=3600,
                    step=30,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            )
        }
    )


class NtiNetConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for NTI net."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the account credentials and validate them."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(
                user_input[CONF_USERNAME].strip().lower()
            )
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = NtiNetApiClient(
                session=session,
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                api_url=DEFAULT_API_URL,
            )
            try:
                await client.async_login()
                plants = await client.async_get_plants()
            except NtiNetAuthError:
                errors["base"] = "invalid_auth"
            except NtiNetRateLimitError:
                errors["base"] = "rate_limited"
            except NtiNetConnectionError:
                errors["base"] = "cannot_connect"
            except NtiNetError:
                _LOGGER.exception("Unexpected error validating NTI net account")
                errors["base"] = "unknown"
            else:
                if not plants:
                    errors["base"] = "no_plants"
                else:
                    return self.async_create_entry(
                        title=user_input[CONF_USERNAME],
                        data=user_input,
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NtiNetOptionsFlow:
        """Return the options flow."""
        return NtiNetOptionsFlow()


class NtiNetOptionsFlow(OptionsFlow):
    """Let the user tune the poll interval.

    The cloud throttles per account and answers `429 Requests are blocked for N
    seconds`, so the interval cannot be set below `MIN_SCAN_INTERVAL`.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the options step."""
        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_scan_interval_schema(int(current)),
        )
