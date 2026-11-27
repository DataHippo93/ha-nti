"""Tests for the fault and service-reading sensors.

None of these touch the cloud.  They pin three things that matter in use:

* a fault read that fails reports "unknown", never "OK";
* a throttled account stops the poll instead of piling on more requests;
* a reading the cloud flags as an error is dropped, not shown.
"""
from __future__ import annotations

from typing import Any

import pytest

from custom_components.nti_net.api import NtiNetError, NtiNetRateLimitError
from custom_components.nti_net.binary_sensor import (
    GENERIC_ONLY_KEYS,
    PLANT_BINARY_SENSORS,
)
from custom_components.nti_net.const import FAULT_STATE_OK
from custom_components.nti_net.coordinator import (
    NtiNetCoordinator,
    NtiNetPlant,
    _item_values,
    _parse_fault,
)
from custom_components.nti_net.sensor import (
    DIAGNOSTIC_SENSORS,
    PLANT_ATTRIBUTE_MAP,
    NtiNetFaultSensor,
)

# The shapes below are the ones a live gwSysType 3 gateway answered on
# 2026-11-26 (values in SI because the client pins umsys=si).
DATA_ITEMS = [
    {"id": "ChFlowSetpointTemp", "zone": 0, "value": 62.0, "unit": "°C",
     "error": False, "invalid": False},
    {"id": "ChFlowTemp", "zone": 0, "value": 61.0, "unit": "°C",
     "error": False, "invalid": False},
    {"id": "HeatingCircuitPressure", "zone": 0, "value": 1.2, "unit": "bar",
     "error": False, "invalid": False},
    {"id": "AutomaticThermoregulation", "zone": 0, "value": 1.0,
     "error": False, "invalid": False},
]
MENU_ITEMS = [
    {"id": 124, "text": "CH return temperature", "value": 59.0, "unit": "°C",
     "error": False},
    {"id": 119, "text": "Signal Level", "value": 100.0, "unit": "%",
     "error": False},
]


class FakeClient:
    """Stand-in for the API client with scriptable answers."""

    def __init__(
        self,
        errors: Any = (),
        items: Any = (),
        menu: Any = (),
    ) -> None:
        self._errors = errors
        self._items = items
        self._menu = menu
        self.calls: list[str] = []

    async def _answer(self, name: str, value: Any) -> Any:
        self.calls.append(name)
        if isinstance(value, Exception):
            raise value
        return list(value)

    async def async_get_bus_errors(self, gateway_id: str) -> Any:
        return await self._answer("errors", self._errors)

    async def async_get_data_items(
        self, gateway_id: str, items: Any, features: Any
    ) -> Any:
        self.requested_items = items
        return await self._answer("items", self._items)

    async def async_get_menu_items(self, gateway_id: str, menu_ids: Any) -> Any:
        self.requested_menu = menu_ids
        return await self._answer("menu", self._menu)


def _coordinator(client: FakeClient) -> NtiNetCoordinator:
    """Build a coordinator without Home Assistant around a fake client."""
    coordinator = object.__new__(NtiNetCoordinator)
    coordinator.client = client
    return coordinator


def _plant() -> NtiNetPlant:
    return NtiNetPlant(gateway_id="TEST", name="Boiler", raw={"gw": "TEST"})


async def test_healthy_boiler_reads_ok_and_service_values() -> None:
    client = FakeClient(errors=[], items=DATA_ITEMS, menu=MENU_ITEMS)
    plant = _plant()
    await _coordinator(client)._async_apply_diagnostics(plant)

    assert plant.faults == []
    assert plant.has_fault is False
    assert plant.flow_setpoint == 62.0
    assert plant.flow_temp == 61.0
    assert plant.return_temp == 59.0
    assert plant.pressure == 1.2
    assert plant.signal_level == 100.0
    assert plant.auto_thermoreg is True
    assert client.calls == ["errors", "items", "menu"]
    assert {item["id"] for item in client.requested_items} == {
        "ChFlowSetpointTemp",
        "ChFlowTemp",
        "HeatingCircuitPressure",
        "AutomaticThermoregulation",
    }
    assert tuple(client.requested_menu) == (124, 119)


async def test_failed_fault_read_is_unknown_not_ok() -> None:
    """A fault list that could not be read must not look healthy."""
    client = FakeClient(errors=NtiNetError("boom"), items=DATA_ITEMS, menu=MENU_ITEMS)
    plant = _plant()
    await _coordinator(client)._async_apply_diagnostics(plant)

    assert plant.faults is None
    assert plant.has_fault is None
    assert plant.blocking_fault is None
    # The other reads still happen and still land.
    assert plant.flow_temp == 61.0
    assert plant.return_temp == 59.0


async def test_failed_service_reads_leave_values_unknown() -> None:
    client = FakeClient(
        errors=[], items=NtiNetError("500"), menu=NtiNetError("500")
    )
    plant = _plant()
    await _coordinator(client)._async_apply_diagnostics(plant)

    assert plant.faults == []
    assert plant.flow_temp is None
    assert plant.flow_setpoint is None
    assert plant.pressure is None
    assert plant.return_temp is None
    assert plant.signal_level is None
    assert plant.auto_thermoreg is None


@pytest.mark.parametrize("failing", ["errors", "items", "menu"])
async def test_throttling_stops_the_poll(failing: str) -> None:
    """A 429 on any of the extra reads must propagate, not be swallowed."""
    answers: dict[str, Any] = {"errors": [], "items": DATA_ITEMS, "menu": MENU_ITEMS}
    answers[failing] = NtiNetRateLimitError("blocked for 60 seconds", 60)
    client = FakeClient(**answers)

    with pytest.raises(NtiNetRateLimitError):
        await _coordinator(client)._async_apply_diagnostics(_plant())
    # Nothing after the throttled request is attempted.
    assert client.calls[-1] == failing


def test_parse_fault_reads_the_shared_remotethermo_shape() -> None:
    entry = {
        "gw": "TEST",
        "timestamp": "2026-11-26T18:02:11",
        "fault": 45,
        "code": "501",
        "errDex": "No flame detected",
        "res": "Reset",
        "blk": True,
    }
    fault = _parse_fault(entry)
    assert fault["code"] == "501"
    assert fault["description"] == "No flame detected"
    assert fault["blocking"] is True
    assert fault["reset"] == "Reset"
    assert fault["timestamp"] == "2026-11-26T18:02:11"
    assert fault["raw"] is entry


def test_parse_fault_survives_an_unexpected_shape() -> None:
    """Field names are a guess until a real fault is seen; never crash."""
    entry = {"somethingElse": 7, "message": "Low water pressure"}
    fault = _parse_fault(entry)
    assert fault["code"] is None
    assert fault["description"] == "Low water pressure"
    assert fault["blocking"] is None
    assert fault["raw"] == entry

    # A numeric-only fault still yields a usable code.
    assert _parse_fault({"fault": 108})["code"] == "108"


def test_item_values_drops_flagged_readings() -> None:
    items = [
        {"id": "ChFlowTemp", "value": 61.0, "error": False, "invalid": False},
        {"id": "HeatingCircuitPressure", "value": 0.0, "error": True},
        {"id": "ChFlowSetpointTemp", "value": -3277.0, "invalid": True},
        {"id": "NoValue"},
        {"value": 5.0},
    ]
    assert _item_values(items, "id") == {"ChFlowTemp": 61.0}


class _FakeCoordinator:
    """Just enough coordinator for an entity's value properties."""

    last_update_success = True

    def __init__(self, plant: NtiNetPlant) -> None:
        self.data = {plant.gateway_id: plant}


def _fault_sensor(plant: NtiNetPlant) -> NtiNetFaultSensor:
    sensor = object.__new__(NtiNetFaultSensor)
    sensor.coordinator = _FakeCoordinator(plant)
    sensor._plant_id = plant.gateway_id
    return sensor


def test_fault_sensor_states() -> None:
    plant = _plant()

    plant.faults = None
    sensor = _fault_sensor(plant)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}

    plant.faults = []
    assert sensor.native_value == FAULT_STATE_OK
    assert sensor.extra_state_attributes["count"] == 0

    plant.faults = [
        _parse_fault({"code": "501", "errDex": "No flame detected", "blk": True}),
        _parse_fault({"code": "108", "errDex": "Low pressure", "blk": False}),
    ]
    assert sensor.native_value == "501"
    attributes = sensor.extra_state_attributes
    assert attributes["count"] == 2
    assert attributes["description"] == "No flame detected"
    assert attributes["blocking"] is True
    assert plant.has_fault is True
    assert plant.blocking_fault is True

    # No code at all: fall back to the description rather than "OK".
    plant.faults = [_parse_fault({"message": "Something is wrong"})]
    assert sensor.native_value == "Something is wrong"


def test_every_diagnostic_sensor_maps_to_a_plant_attribute() -> None:
    plant = _plant()
    for description in DIAGNOSTIC_SENSORS:
        attribute = PLANT_ATTRIBUTE_MAP[description.key]
        assert hasattr(plant, attribute), description.key


def test_new_binary_sensors_read_real_plant_attributes() -> None:
    plant = _plant()
    keys = {description.key for description in PLANT_BINARY_SENSORS}
    assert GENERIC_ONLY_KEYS <= keys
    for key in GENERIC_ONLY_KEYS:
        assert hasattr(plant, key), key


# ---------------------------------------------------------------------------
# Full set-up through Home Assistant with the cloud stubbed out, so that the
# new entities are proven to load, register and report on the real platforms.
# ---------------------------------------------------------------------------
PLANT_DATA = {
    "gw": "TEST",
    "mod": {"allowedOptions": [0, 1, 5], "value": 1},
    "dMod": {"allowedOptions": [], "value": 0},
    "dComf": {"step": 1.0, "min": 36.0, "max": 60.0, "value": 60.0},
    "out": -1.0,
    "zones": {
        "1": {
            "mod": {"allowedOptions": [2, 3], "value": 3},
            "room": 0.0,
            "desired": 24.4,
            "comf": {"step": 0.5, "min": 10.0, "max": 30.0, "value": 24.5},
            "econ": {"step": 0.5, "min": 10.0, "max": 30.0, "value": 16.0},
            "heat": True,
            "der": {"temp": 0.0, "exp": "1970-01-01T00:00:00", "enab": False},
            "antiFreezeTemp": 5.0,
        }
    },
    "auto": True,
    "flame": True,
    "dhwDisabled": False,
    "hpOn": False,
    "dhwBoiler": False,
}
FEATURES = {
    "zones": [{"num": 1, "name": "HOME", "roomSens": False, "isHidden": False}],
    "hasMetering": False,
    "dhwModeChangeable": True,
}


async def _setup(hass, faults: list[dict]) -> None:
    from unittest.mock import AsyncMock, patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain="nti_net",
        data={"username": "user@example.com", "password": "secret"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)
    client = "custom_components.nti_net.api.NtiNetApiClient"
    with (
        patch(f"{client}.async_login", AsyncMock(return_value=True)),
        patch(
            f"{client}.async_get_plants",
            AsyncMock(
                return_value=[
                    {"gwId": "TEST", "plantName": "Boiler", "gwSysType": 3}
                ]
            ),
        ),
        patch(f"{client}.async_get_features", AsyncMock(return_value=FEATURES)),
        patch(
            f"{client}.async_get_gateway_info",
            AsyncMock(return_value={"isOnline": True}),
        ),
        patch(f"{client}.async_get_plant_data", AsyncMock(return_value=PLANT_DATA)),
        patch(f"{client}.async_get_bus_errors", AsyncMock(return_value=faults)),
        patch(f"{client}.async_get_data_items", AsyncMock(return_value=DATA_ITEMS)),
        patch(f"{client}.async_get_menu_items", AsyncMock(return_value=MENU_ITEMS)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_entities_load_on_a_healthy_boiler(hass) -> None:
    await _setup(hass, [])

    assert hass.states.get("sensor.boiler_fault_code").state == FAULT_STATE_OK
    assert hass.states.get("binary_sensor.boiler_fault").state == "off"
    assert (
        hass.states.get("binary_sensor.boiler_automatic_thermoregulation").state
        == "on"
    )
    assert float(hass.states.get("sensor.boiler_flow_temperature").state) == 61.0
    assert float(hass.states.get("sensor.boiler_flow_setpoint").state) == 62.0
    assert float(hass.states.get("sensor.boiler_return_temperature").state) == 59.0
    assert (
        float(hass.states.get("sensor.boiler_heating_circuit_pressure").state)
        == 1.2
    )
    assert float(hass.states.get("sensor.boiler_gateway_signal_level").state) == 100.0


async def test_entities_report_an_active_fault(hass) -> None:
    await _setup(
        hass, [{"code": "501", "errDex": "No flame detected", "blk": True}]
    )

    state = hass.states.get("sensor.boiler_fault_code")
    assert state.state == "501"
    assert state.attributes["description"] == "No flame detected"
    assert state.attributes["blocking"] is True
    assert state.attributes["count"] == 1
    assert hass.states.get("binary_sensor.boiler_fault").state == "on"
