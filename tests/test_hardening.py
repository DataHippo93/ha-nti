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


def _find(obj, needle, path="$"):
    """Return the first path where ``needle`` appears as a key or a value."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and needle in key:
                return f"{path}.<key {key}>"
            found = _find(value, needle, f"{path}.{key}")
            if found:
                return found
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found = _find(value, needle, f"{path}[{index}]")
            if found:
                return found
    elif isinstance(obj, str) and needle in obj:
        return path
    return None


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
    assert usr not in blob, f"account e-mail leaked into diagnostics: {_find(diag, usr)}"
    assert pwd not in blob, "password leaked into diagnostics"
    for gateway in entry.runtime_data.data:
        assert gateway not in blob, f"gateway id leaked: {_find(diag, gateway)}"
    for plant in diag["plants"].values():
        assert plant["gateway_id"] == "**REDACTED**"
        assert plant["serial"] == "**REDACTED**"
    assert diag["entry"]["data"]["password"] == "**REDACTED**"


async def test_gas_sensors_are_live(hass: HomeAssistant) -> None:
    """The gas sensors must be created and carry real values.

    The numbers are cross-checked against the cloud's own monthly breakdown,
    which is a separate code path (``energyAccount``), so a unit mix-up would
    show up as a factor of 3.6 or 10.
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

    coordinator = entry.runtime_data
    plant = next(iter(coordinator.data.values()))
    if not plant.has_metering:
        import pytest

        pytest.skip("this gateway does not advertise metering")

    states = {
        state.entity_id: state
        for state in hass.states.async_all("sensor")
        if state.entity_id.startswith("sensor.")
    }
    gas = {
        entity_id: state
        for entity_id, state in states.items()
        if "gas" in entity_id
    }
    print("\ngas sensors:", {k: v.state for k, v in gas.items()})
    assert len(gas) >= 5, gas

    for state in gas.values():
        assert state.state not in ("unknown", "unavailable"), state.entity_id
        assert state.attributes["unit_of_measurement"] == "kWh"
        assert state.attributes["device_class"] == "energy"
        # Home Assistant's energy device class only allows total/total_increasing,
        # so anything else here would log an "impossible state class" warning.
        assert state.attributes.get("state_class") in (
            None,
            "total",
            "total_increasing",
        ), state.entity_id

    month_total = states[
        next(e for e in gas if e.endswith("_gas_consumption_this_month"))
    ]
    assert month_total.attributes["state_class"] == "total_increasing"
    # Only the running month-to-date total may be a meter; the rest are
    # per-period figures that legitimately go down.
    for entity_id, state in gas.items():
        if entity_id == month_total.entity_id:
            continue
        assert state.attributes.get("state_class") is None, entity_id
    # Month to date cannot exceed the total of the last whole month by much.
    last_month = next(
        float(s.state) for e, s in gas.items() if e.endswith("_gas_consumption_last_month")
    )
    print("\nthis month:", month_total.state, "| last month:", last_month)
    assert float(month_total.state) < last_month * 3
