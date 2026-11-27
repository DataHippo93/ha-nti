"""Tests for the hot water switch and the combi DHW-mode fix.

Shapes are the ones a live combi gateway (gwSysType 3) answered on 2026-11-26:
the aggregated read said ``dMod = {"allowedOptions": [], "value": 0}`` while
the ``DhwMode`` data item said 2 (always active).  A write of
``{"new": 0, "old": 2}`` to ``plantData/{gw}/dhwMode`` answered
``{"success": true}`` and the item read 0 five seconds later.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nti_net.api import NtiNetError
from custom_components.nti_net.const import DhwMode

from test_faults import DATA_ITEMS, FEATURES, MENU_ITEMS, PLANT_DATA, FakeClient
from test_faults import _coordinator, _plant

SWITCH = "switch.boiler_hot_water"


def _items(mode: Any) -> list[dict[str, Any]]:
    items = [dict(item) for item in DATA_ITEMS]
    if mode is not None:
        items.append({"id": "DhwMode", "zone": 0, "value": mode, "error": False})
    return items


async def test_combi_mode_comes_from_the_data_item() -> None:
    """With an empty option list the data item overrides the constant 0."""
    plant = _plant()
    plant.dhw_mode = 0
    plant.allowed_dhw_modes = []
    await _coordinator(FakeClient(items=_items(2.0), menu=MENU_ITEMS))._async_apply_diagnostics(plant)
    assert plant.dhw_mode_item == 2
    assert plant.dhw_mode == 2


async def test_cylinder_mode_is_left_alone() -> None:
    """Where the aggregated read does advertise modes, it stays the source."""
    plant = _plant()
    plant.dhw_mode = 1
    plant.allowed_dhw_modes = [0, 1, 2]
    await _coordinator(FakeClient(items=_items(2.0), menu=MENU_ITEMS))._async_apply_diagnostics(plant)
    assert plant.dhw_mode_item == 2
    assert plant.dhw_mode == 1


async def test_missing_item_leaves_the_mode_unknown() -> None:
    plant = _plant()
    plant.dhw_mode = 0
    await _coordinator(FakeClient(items=_items(None), menu=MENU_ITEMS))._async_apply_diagnostics(plant)
    assert plant.dhw_mode_item is None
    assert plant.dhw_mode == 0


class _Cloud:
    """A stubbed cloud whose DHW mode can be written and read back."""

    def __init__(self, mode: Any) -> None:
        self.mode = mode
        self.writes: list[tuple[int, Any]] = []
        self.fail_write: Exception | None = None

    async def data_items(self, gateway_id: str, items: Any, features: Any) -> Any:
        return _items(self.mode)

    async def set_mode(self, gateway_id: str, new: int, old: Any = None) -> None:
        if self.fail_write is not None:
            raise self.fail_write
        self.writes.append((new, old))
        self.mode = float(new)


async def _setup(hass, cloud: _Cloud):
    entry = MockConfigEntry(
        domain="nti_net",
        data={"username": "user@example.com", "password": "secret"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)
    client = "custom_components.nti_net.api.NtiNetApiClient"
    patches = [
        patch(f"{client}.async_login", AsyncMock(return_value=True)),
        patch(
            f"{client}.async_get_plants",
            AsyncMock(return_value=[{"gwId": "TEST", "plantName": "Boiler", "gwSysType": 3}]),
        ),
        patch(f"{client}.async_get_features", AsyncMock(return_value=FEATURES)),
        patch(f"{client}.async_get_gateway_info", AsyncMock(return_value={"isOnline": True})),
        patch(f"{client}.async_get_plant_data", AsyncMock(return_value=PLANT_DATA)),
        patch(f"{client}.async_get_bus_errors", AsyncMock(return_value=[])),
        patch(f"{client}.async_get_data_items", AsyncMock(side_effect=cloud.data_items)),
        patch(f"{client}.async_get_menu_items", AsyncMock(return_value=MENU_ITEMS)),
        patch(f"{client}.async_set_dhw_mode", AsyncMock(side_effect=cloud.set_mode)),
        patch("custom_components.nti_net.switch.WRITE_SETTLE_SECONDS", 0),
    ]
    for item in patches:
        item.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return patches


def _stop(patches) -> None:
    for item in patches:
        item.stop()


async def test_switch_reflects_the_real_mode_and_fixes_the_water_heater(hass) -> None:
    patches = await _setup(hass, _Cloud(2.0))
    try:
        state = hass.states.get(SWITCH)
        assert state.state == "on"
        assert state.attributes["mode"] == "comfort"
        # The water heater used to read "off" here because of the constant 0.
        assert hass.states.get("water_heater.boiler_hot_water").state == "comfort"
    finally:
        _stop(patches)


async def test_switch_off_and_on_round_trip(hass) -> None:
    cloud = _Cloud(2.0)
    patches = await _setup(hass, cloud)
    try:
        await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
        await hass.async_block_till_done()
        assert cloud.writes == [(DhwMode.DISABLED, 2)]
        assert hass.states.get(SWITCH).state == "off"

        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
        await hass.async_block_till_done()
        assert cloud.writes[-1] == (DhwMode.ALWAYS_ACTIVE, 0)
        assert hass.states.get(SWITCH).state == "on"

        # Already on: no write at all.
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
        assert len(cloud.writes) == 2
    finally:
        _stop(patches)


async def test_time_based_mode_counts_as_on(hass) -> None:
    patches = await _setup(hass, _Cloud(1.0))
    try:
        state = hass.states.get(SWITCH)
        assert state.state == "on"
        assert state.attributes["mode"] == "eco"
    finally:
        _stop(patches)


async def test_unknown_mode_is_unknown_and_refuses_to_write(hass) -> None:
    """Without a known current mode the switch must not guess or write."""
    cloud = _Cloud(None)
    patches = await _setup(hass, cloud)
    try:
        assert hass.states.get(SWITCH).state == "unknown"
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
        assert cloud.writes == []
    finally:
        _stop(patches)


async def test_failed_write_leaves_the_switch_unchanged(hass) -> None:
    cloud = _Cloud(2.0)
    cloud.fail_write = NtiNetError("500")
    patches = await _setup(hass, cloud)
    try:
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
        assert hass.states.get(SWITCH).state == "on"
    finally:
        _stop(patches)
