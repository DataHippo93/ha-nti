"""Tests for the reauth flow, diagnostics redaction and new services.

These need live credentials because the integration cannot finish setup without
a real login; they skip cleanly when no account is configured.
"""
from __future__ import annotations

import json

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import TextSelectorType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from helpers import load_credentials

DOMAIN = "nti_net"


async def test_password_field_is_masked(hass: HomeAssistant) -> None:
    """The password box must not render as plain text."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    types = {
        str(key): value.config.get("type")
        for key, value in result["data_schema"].schema.items()
        if hasattr(value, "config")
    }
    print("\nSELECTOR TYPES:", types)
    assert types.get("password") == TextSelectorType.PASSWORD
    assert types.get("username") == TextSelectorType.EMAIL


async def test_services_registered(hass: HomeAssistant) -> None:
    """The override and holiday services appear once the entry is loaded."""
    usr, pwd = load_credentials()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": usr, "password": pwd},
        unique_id=usr.lower(),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    services = sorted(hass.services.async_services().get(DOMAIN, {}))
    print("\nSERVICES:", services)
    assert "set_zone_override" in services
    assert "clear_zone_override" in services
    assert "set_holiday" in services


async def test_diagnostics_contains_no_pii(hass: HomeAssistant) -> None:
    """The diagnostics dump must not expose the account or the gateway.

    Regression: the plants mapping was keyed by gateway id, and redaction only
    rewrites values, so the id survived in the key.
    """
    usr, pwd = load_credentials()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": usr, "password": pwd},
        unique_id=usr.lower(),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from custom_components.nti_net.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)
    blob = json.dumps(diag)
    print("\nplant keys:", list(diag["plants"]))
    assert usr not in blob, "account e-mail leaked into diagnostics"
    assert pwd not in blob, "password leaked into diagnostics"
    for plant in diag["plants"].values():
        assert plant["gateway_id"] == "**REDACTED**"
        assert plant["serial"] == "**REDACTED**"
    assert diag["entry"]["data"]["password"] == "**REDACTED**"
