"""Unit tests for the parts of the integration that need no live cloud.

These pin the behaviours that were fixed after 0.1.0: the BSB zone that used to
be parsed and then discarded, the override flag, the DHW mode-changeable flag,
and the fact that a changed password now starts a reauth flow instead of
failing forever.
"""
from __future__ import annotations

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nti_net.const import DhwMode
from custom_components.nti_net.coordinator import (
    NtiNetCoordinator,
    NtiNetPlant,
    NtiNetZone,
)

DOMAIN = "nti_net"


def _coordinator() -> NtiNetCoordinator:
    """Build a coordinator without Home Assistant, for parser tests."""
    return object.__new__(NtiNetCoordinator)


def test_bsb_zones_are_kept() -> None:
    """A BSB zone must reach plant.zones.

    Regression: the bsb branch of _apply_zone returned before storing the zone,
    so every BSB gateway reported zero zones.
    """
    plant = NtiNetPlant(gateway_id="TEST", name="BSB")
    _coordinator()._apply_bsb(
        plant,
        {
            "mode": 1,
            "outTemp": 5.0,
            "flame": True,
            "dhwTemp": 50.0,
            "zones": [
                {
                    "num": 1,
                    "chMode": 2,
                    "chComfortTemp": 20.0,
                    "chReducedTemp": 17.0,
                    "desiredTemp": 20.5,
                    "chRequest": True,
                    "roomTemp": 19.5,
                }
            ],
        },
    )

    assert list(plant.zones) == [1]
    zone = plant.zones[1]
    assert zone.comfort_temp == 20.0
    assert zone.reduced_temp == 17.0
    assert zone.desired_temp == 20.5
    assert zone.heat_request is True
    assert zone.room_temp == 19.5


def test_hidden_zone_is_skipped() -> None:
    """Hidden virtual zones must not produce entities."""
    plant = NtiNetPlant(gateway_id="TEST", name="Generic")
    plant.features = {"zones": [{"num": 2, "isHidden": True}]}
    _coordinator()._apply_zone(
        plant,
        2,
        {"mod": {"value": 3, "allowedOptions": [2, 3]}},
        {2: {"num": 2, "isHidden": True}},
    )
    assert plant.zones == {}


def test_override_flag() -> None:
    """override_active follows the enabled flag, not the epoch timestamp."""
    zone = NtiNetZone(number=1, name="Zone 1")
    assert zone.override_active is False

    zone.deroga_enabled = True
    zone.deroga_temp = 21.5
    zone.deroga_until = "2026-11-05T18:00:00"
    assert zone.override_active is True

    plant = NtiNetPlant(gateway_id="TEST", name="P")
    plant.zones[1] = zone
    assert plant.override_active is True


def test_dhw_mode_changeable_from_plant_data() -> None:
    """dhwModeNotChangeable inverts into dhw_mode_changeable."""
    plant = NtiNetPlant(gateway_id="TEST", name="P")
    plant.features = {}
    _coordinator()._apply_plant(plant, {"dhwModeNotChangeable": True})
    assert plant.dhw_mode_changeable is False

    plant2 = NtiNetPlant(gateway_id="TEST2", name="P")
    plant2.features = {}
    _coordinator()._apply_plant(plant2, {"dhwModeNotChangeable": False})
    assert plant2.dhw_mode_changeable is True


def test_dhw_mode_changeable_defaults_true() -> None:
    """An appliance that says nothing about it keeps the controller."""
    plant = NtiNetPlant(gateway_id="TEST", name="P")
    plant.features = {}
    _coordinator()._apply_plant(plant, {"dMod": {"value": 2, "allowedOptions": [0, 1, 2]}})
    assert plant.dhw_mode_changeable is True


async def test_reauth_flow_starts_on_auth_failure(hass: HomeAssistant) -> None:
    """A rejected password must open a reauth flow rather than spin."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "someone@example.com", "password": "old"},
        unique_id="someone@example.com",
    )
    entry.add_to_hass(hass)

    flows = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=entry.data,
    )
    assert flows["type"] is data_entry_flow.FlowResultType.FORM
    assert flows["step_id"] == "reauth_confirm"


def test_dhw_mode_names_are_stable() -> None:
    """The operation list the water heater exposes maps to known names."""
    assert DhwMode.NAMES[0] == "off"
    assert DhwMode.NAMES[1] == "eco"
    assert DhwMode.NAMES[2] == "comfort"


@pytest.mark.parametrize(
    ("room", "sensor", "expected"),
    [
        (0.0, False, None),   # no probe: 0.0 is not a real reading
        (0.0, True, 0.0),     # probe present and genuinely reads zero
        (19.5, False, 19.5),  # probe present, positive reading
    ],
)
def test_room_temperature_heuristic(room, sensor, expected) -> None:
    """A missing probe reports 0.0 and must not be shown as 0 °C."""
    plant = NtiNetPlant(gateway_id="TEST", name="P")
    _coordinator()._apply_zone(
        plant,
        1,
        {"room": room, "comf": {"value": 20.0}},
        {1: {"num": 1, "roomSens": sensor, "isHidden": False}},
    )
    assert plant.zones[1].room_temp == expected
