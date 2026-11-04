"""Config flow for the NTI net integration."""

from __future__ import annotations

import logging
from typing import Any, Mapping

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
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    NtiNetApiClient,
    NtiNetAuthError,
    NtiNetConnectionError,
    NtiNetError,
    NtiNetRateLimitError,
)
from .const import (
    CONF_API_URL,
    CONF_GAS_KWH_PER_M3,
    CONF_GAS_UNIT,
    CONF_SCAN_INTERVAL,
    DEFAULT_API_URL,
    DEFAULT_GAS_KWH_PER_M3,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GAS_UNIT_KWH,
    GAS_UNIT_M3,
    MAX_GAS_KWH_PER_M3,
    MIN_GAS_KWH_PER_M3,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


def _options_schema(current: int, gas_unit: str, kwh_per_m3: float) -> vol.Schema:
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
            ),
            vol.Required(CONF_GAS_UNIT, default=gas_unit): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=GAS_UNIT_KWH, label="kWh (energy)"
                        ),
                        SelectOptionDict(
                            value=GAS_UNIT_M3,
                            label="m³ (derived from energy)",
                        ),
                    ],
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_GAS_KWH_PER_M3, default=kwh_per_m3
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_GAS_KWH_PER_M3,
                    max=MAX_GAS_KWH_PER_M3,
                    step=0.01,
                    unit_of_measurement="kWh/m³",
                    mode=NumberSelectorMode.BOX,
                )
            ),
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

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the stored password stopped working.

        This must live on the same handler class as ``async_step_user``: Home
        Assistant registers exactly one config-flow handler per domain, so a
        second ``ConfigFlow`` subclass for the same domain silently replaces the
        first and the setup form stops appearing.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and verify the new password."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="reauth_failed")

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = NtiNetApiClient(
                session=session,
                username=entry.data[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                api_url=entry.data.get(CONF_API_URL, DEFAULT_API_URL),
            )
            try:
                await client.async_login()
            except NtiNetAuthError:
                errors["base"] = "invalid_auth"
            except NtiNetRateLimitError:
                errors["base"] = "rate_limited"
            except NtiNetConnectionError:
                errors["base"] = "cannot_connect"
            except NtiNetError:
                _LOGGER.exception("Unexpected error during NTI net reauth")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    )
                }
            ),
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )


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
        gas_unit = self.config_entry.options.get(CONF_GAS_UNIT, GAS_UNIT_KWH)
        kwh_per_m3 = self.config_entry.options.get(
            CONF_GAS_KWH_PER_M3, DEFAULT_GAS_KWH_PER_M3
        )
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(
                int(current), gas_unit, float(kwh_per_m3)
            ),
        )
