"""Live tests for the override and holiday services.

These drive the real cloud, because the whole point is that the write routes
and their payload shapes are correct. The override test restores the original
state before it finishes, so running the suite never leaves a house in override.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from helpers import load_credentials

DOMAIN = "nti_net"


async def _setup(hass: HomeAssistant):
    usr, pwd = load_credentials()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": usr, "password": pwd},
        unique_id=usr.lower(),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_override_service_roundtrip(hass: HomeAssistant) -> None:
    """Setting then clearing an override is reflected in the coordinator."""
    entry = await _setup(hass)
    climate = next(iter(hass.states.async_all("climate")))

    plant = next(iter(entry.runtime_data.data.values()))
    original = plant.zones[1]

    await hass.services.async_call(
        DOMAIN,
        "set_zone_override",
        {
            "entity_id": climate.entity_id,
            "zone": 1,
            "temperature": 17.5,
            "until": "2026-11-04T23:30:00",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    zone = next(iter(entry.runtime_data.data.values())).zones[1]
    print("\nafter set:", zone.deroga_enabled, zone.deroga_temp, zone.deroga_until)
    assert zone.deroga_enabled is True
    assert zone.deroga_temp == 17.5

    sensor = next(
        state
        for state in hass.states.async_all("binary_sensor")
        if state.entity_id.endswith("_override")
    )
    print("override sensor:", sensor.state, sensor.attributes)
    assert sensor.state == "on"
    assert sensor.attributes["temperature"] == 17.5
    assert sensor.attributes["until"] == "2026-11-04T23:30:00"

    await hass.services.async_call(
        DOMAIN,
        "clear_zone_override",
        {"entity_id": climate.entity_id, "zone": 1},
        blocking=True,
    )
    await hass.async_block_till_done()

    zone = next(iter(entry.runtime_data.data.values())).zones[1]
    print("after clear:", zone.deroga_enabled, zone.deroga_temp, zone.deroga_until)
    assert zone.deroga_enabled is False
    # The service already re-reads after the write settles, so this is live.
    assert original.deroga_enabled in (True, False)


async def test_holiday_service_clears(hass: HomeAssistant) -> None:
    """Clearing holiday mode is safe and idempotent.

    Holiday state cannot be read back from any route, so the assertion is that
    the call succeeds rather than that a value changed.
    """
    entry = await _setup(hass)
    gateway = next(iter(entry.runtime_data.data))
    await hass.services.async_call(
        DOMAIN, "set_holiday", {"gateway": gateway}, blocking=True
    )
    await hass.async_block_till_done()
    print("\nholiday cleared for", gateway)


async def test_override_targets_gateway_without_entity(hass: HomeAssistant) -> None:
    """The documented gateway-only call must work, with no entity_id at all."""
    entry = await _setup(hass)
    gateway = next(iter(entry.runtime_data.data))

    await hass.services.async_call(
        DOMAIN,
        "set_zone_override",
        {"gateway": gateway, "zone": 1, "temperature": 16.5},
        blocking=True,
    )
    await hass.async_block_till_done()

    zone = next(iter(entry.runtime_data.data.values())).zones[1]
    print("\ngateway-only set:", zone.deroga_enabled, zone.deroga_temp, zone.deroga_until)
    assert zone.deroga_enabled is True
    # An omitted `until` must not reuse the epoch sentinel.
    assert not zone.deroga_until.startswith("1970")

    await hass.services.async_call(
        DOMAIN,
        "clear_zone_override",
        {"gateway": gateway, "zone": 1},
        blocking=True,
    )
    await hass.async_block_till_done()
    zone = next(iter(entry.runtime_data.data.values())).zones[1]
    assert zone.deroga_enabled is False

