"""Verify the device registry entry for a live plant."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from helpers import load_credentials

DOMAIN = "nti_net"


async def test_device_registered(hass: HomeAssistant):
    """The gateway should appear as one device with the plant's own details."""
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
    assert coordinator.data, "account reported no plants"

    reg = dr.async_get(hass)
    for gateway_id, plant in coordinator.data.items():
        device = reg.async_get_device_by_identifier((DOMAIN, gateway_id), entry.entry_id)
        assert device is not None, f"no device registered for {gateway_id}"

        print(
            f"\nDEVICE: {device.name} | manufacturer: {device.manufacturer}"
            f" | model: {device.model} | sw: {device.sw_version}"
            f" | sn: {device.serial_number}"
        )

        assert device.manufacturer == "NTI"
        # The device must mirror what the cloud reported for this gateway,
        # not values hard-coded in the test.
        assert device.sw_version == plant.firmware
        assert device.serial_number == plant.serial
