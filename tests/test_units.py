"""Unit tests for the parts of the integration that need no live cloud.

These pin the behaviours that were fixed after 0.1.0: the BSB zone that used to
be parsed and then discarded, the override flag, the DHW mode-changeable flag,
and the fact that a changed password now starts a reauth flow instead of
failing forever.
"""
from __future__ import annotations

import pytest
from freezegun import freeze_time
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nti_net.api import NtiNetError
from custom_components.nti_net.const import DhwMode
from custom_components.nti_net.coordinator import (
    _month_to_date,
    _parse_consumption,
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


def test_has_metering_flag() -> None:
    """Only a gateway that advertises metering gets gas sensors."""
    plant = NtiNetPlant(gateway_id="TEST", name="P")
    assert plant.has_metering is False

    plant.features = {"hasMetering": True}
    assert plant.has_metering is True

    plant.features = {"hasMetering": False}
    assert plant.has_metering is False


def test_consumption_parses_gas_from_both_families() -> None:
    """Gas comes from k=7 (heating) and k=10 (DHW), in kWh.

    Regression guard for the field mapping: the route only accepts the family
    names ``Ch``/``Dhw``, and the members are distinguished by ``k``.
    """
    blocks = [
        {"k": 1, "p": 4, "v": [0.0, 817.263]},
        {"k": 7, "p": 4, "v": [0.0, 814.563]},
        {"k": 20, "p": 4, "v": [0.0, 2.7]},
        {"k": 2, "p": 4, "v": [0.0, 1791.715]},
        {"k": 10, "p": 4, "v": [0.0, 1786.602]},
        {"k": 21, "p": 4, "v": [0.0, 5.113]},
    ]
    readings = _parse_consumption(blocks)
    assert readings["gas_heating_last_month"] == 814.56
    assert readings["gas_dhw_last_month"] == 1786.6
    assert readings["gas_last_month"] == 2601.16


def test_consumption_ignores_electricity_members() -> None:
    """The electricity series must not leak into a gas reading."""
    blocks = [
        {"k": 20, "p": 4, "v": [500.0]},
        {"k": 21, "p": 4, "v": [400.0]},
    ]
    readings = _parse_consumption(blocks)
    assert readings["gas_heating_last_month"] is None
    assert readings["gas_dhw_last_month"] is None
    assert readings["gas_last_month"] is None


def test_consumption_picks_period_four_as_monthly() -> None:
    """p=4 is the monthly bucket; p=1 must not be mistaken for it."""
    blocks = [
        {"k": 7, "p": 1, "v": [1.0, 2.0]},
        {"k": 7, "p": 4, "v": [10.0, 20.0, 30.0]},
    ]
    readings = _parse_consumption(blocks)
    assert readings["gas_heating_last_month"] == 30.0


def test_consumption_tolerates_missing_and_junk_blocks() -> None:
    """A partial or malformed payload must not raise."""
    assert _parse_consumption([])["gas_last_month"] is None
    assert _parse_consumption([{"k": 7}])["gas_last_month"] is None
    assert _parse_consumption([{"k": "x", "p": 4, "v": [1.0]}])["gas_last_month"] is None
    assert _parse_consumption([{"k": 7, "p": 4, "v": []}])["gas_last_month"] is None


def test_month_to_date_sums_only_the_current_month() -> None:
    """Month to date must not include days from the previous month.

    The daily series reaches back across the month boundary, so on the 4th of
    the month only the last three entries belong to it.
    """
    daily = {
        7: [100.0, 1.0, 2.0, 3.0],  # three current-month days after the first
        10: [200.0, 4.0, 5.0, 6.0],
    }
    with freeze_time("2026-11-04 12:00:00"):
        result = _month_to_date(daily)
    assert result[7] == 6.0
    assert result[10] == 15.0


def test_month_to_date_is_zero_on_the_first() -> None:
    """On the 1st there is no completed day, which is a real zero."""
    daily = {7: [100.0, 1.0], 10: [200.0, 2.0]}
    # Noon, so the local date is the same in every timezone Home Assistant's
    # test harness might run in.
    with freeze_time("2026-11-01 12:00:00"):
        result = _month_to_date(daily)
    assert result == {7: 0.0, 10: 0.0}


def test_month_to_date_skips_a_short_series() -> None:
    """A series shorter than the elapsed days contributes nothing."""
    daily = {7: [1.0], 10: [2.0, 3.0, 4.0, 5.0]}
    with freeze_time("2026-11-04 12:00:00"):
        result = _month_to_date(daily)
    assert 7 not in result
    assert result[10] == 12.0


async def test_consumption_is_cached_between_polls() -> None:
    """A second poll inside the window must reuse the readings, not refetch."""
    coordinator = _coordinator()
    coordinator._consumption_cache = {}
    coordinator._consumption_fetched_at = {}

    calls = 0

    class _Client:
        async def async_get_consumption_sequences(self, gateway_id):
            nonlocal calls
            calls += 1
            return [{"k": 10, "p": 4, "v": [1786.602]}]

    coordinator.client = _Client()

    plant = NtiNetPlant(gateway_id="TEST", name="P")
    plant.features = {"hasMetering": True}

    await coordinator._async_apply_consumption(plant)
    assert calls == 1
    assert plant.gas_dhw_last_month == 1786.6

    # A fresh plant object inside the window still gets the cached value, so a
    # transient failure cannot blank the sensor.
    again = NtiNetPlant(gateway_id="TEST", name="P")
    again.features = {"hasMetering": True}
    await coordinator._async_apply_consumption(again)
    assert calls == 1
    assert again.gas_dhw_last_month == 1786.6


async def test_consumption_failure_does_not_raise() -> None:
    """A failing report route must leave the rest of the device usable."""
    coordinator = _coordinator()
    coordinator._consumption_cache = {}
    coordinator._consumption_fetched_at = {}

    class _Client:
        async def async_get_consumption_sequences(self, gateway_id):
            raise NtiNetError("boom")

    coordinator.client = _Client()

    plant = NtiNetPlant(gateway_id="TEST", name="P")
    plant.features = {"hasMetering": True}
    await coordinator._async_apply_consumption(plant)
    assert plant.gas_last_month is None


async def test_consumption_is_skipped_without_metering() -> None:
    """A non-metering gateway must not call the report route at all."""
    coordinator = _coordinator()
    coordinator._consumption_cache = {}
    coordinator._consumption_fetched_at = {}

    class _Client:
        async def async_get_consumption_sequences(self, gateway_id):
            raise AssertionError("must not be called")

    coordinator.client = _Client()
    plant = NtiNetPlant(gateway_id="TEST", name="P")
    await coordinator._async_apply_consumption(plant)
