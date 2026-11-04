"""The options flow must expose the gas unit without breaking the interval."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nti_net.const import (
    CONF_GAS_KWH_PER_M3,
    CONF_GAS_UNIT,
    CONF_SCAN_INTERVAL,
    DEFAULT_GAS_KWH_PER_M3,
    DOMAIN,
    GAS_UNIT_M3,
)

from helpers import load_credentials


async def test_options_form_offers_gas_unit(hass: HomeAssistant) -> None:
    """The form must show the unit choice and the conversion factor."""
    usr, pwd = load_credentials()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": usr, "password": pwd},
        unique_id=usr.lower(),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"].value == "form"
    schema = flow["data_schema"]
    keys = {str(k) for k in schema.schema}
    assert CONF_SCAN_INTERVAL in keys
    assert CONF_GAS_UNIT in keys
    assert CONF_GAS_KWH_PER_M3 in keys

    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        user_input={
            CONF_SCAN_INTERVAL: 300,
            CONF_GAS_UNIT: GAS_UNIT_M3,
            CONF_GAS_KWH_PER_M3: 10.36,
        },
    )
    assert result["type"].value == "create_entry"
    assert entry.options[CONF_GAS_UNIT] == GAS_UNIT_M3
    assert entry.options[CONF_GAS_KWH_PER_M3] == 10.36

    # The reload must bring the sensors back in m³.
    await hass.async_block_till_done()
    states = [
        s for s in hass.states.async_all("sensor")
        if "gas" in s.entity_id and "boiler" in s.entity_id
    ]
    assert states, "gas sensors disappeared after the options reload"
    for state in states:
        assert state.attributes["unit_of_measurement"] == "m³", state.entity_id
        assert state.attributes["device_class"] == "gas", state.entity_id
    print("\nm³ sensors:", {s.entity_id: s.state for s in states})


async def test_default_gas_unit_is_kwh(hass: HomeAssistant) -> None:
    """With no options set, the sensors stay in kWh."""
    usr, pwd = load_credentials()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": usr, "password": pwd},
        unique_id=usr.lower(),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.options.get(CONF_GAS_UNIT) is None
    states = [
        s for s in hass.states.async_all("sensor")
        if "gas" in s.entity_id and "boiler" in s.entity_id
    ]
    assert states
    for state in states:
        assert state.attributes["unit_of_measurement"] == "kWh", state.entity_id
    assert DEFAULT_GAS_KWH_PER_M3 > 0
