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

from custom_components.nti_net.api import NtiNetError, NtiNetRateLimitError
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
    """A stubbed cloud whose DHW mode can be written and read back.

    ``lag`` is how many reads after a write still answer the old mode, which
    is what the real gateway does for a few seconds.
    """

    def __init__(self, mode: Any, lag: int = 0) -> None:
        self.mode = mode
        self.lag = lag
        self._stale: Any = None
        self._stale_reads = 0
        self.writes: list[tuple[int, Any]] = []
        self.reads = 0
        self.fail_write: Exception | None = None
        self.fail_reads: Exception | None = None
        self.apply_writes = True

    async def data_items(self, gateway_id: str, items: Any, features: Any) -> Any:
        self.reads += 1
        if self.fail_reads is not None:
            raise self.fail_reads
        if self._stale_reads > 0:
            self._stale_reads -= 1
            return _items(self._stale)
        return _items(self.mode)

    async def set_mode(self, gateway_id: str, new: int, old: Any = None) -> None:
        if self.fail_write is not None:
            raise self.fail_write
        self.writes.append((new, old))
        if self.apply_writes:
            self._stale, self._stale_reads = self.mode, self.lag
            self.mode = float(new)


async def _setup(hass, cloud: _Cloud, features: dict | None = None, plant_data: dict | None = None):
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
        patch(f"{client}.async_get_features", AsyncMock(return_value=features or FEATURES)),
        patch(f"{client}.async_get_gateway_info", AsyncMock(return_value={"isOnline": True})),
        patch(f"{client}.async_get_plant_data", AsyncMock(return_value=plant_data or PLANT_DATA)),
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


async def _call(hass, service: str) -> None:
    await hass.services.async_call("switch", service, {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()


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
        await _call(hass, "turn_off")
        assert cloud.writes == [(DhwMode.DISABLED, 2)]
        assert hass.states.get(SWITCH).state == "off"

        await _call(hass, "turn_on")
        assert cloud.writes[-1] == (DhwMode.ALWAYS_ACTIVE, 0)
        assert hass.states.get(SWITCH).state == "on"

        # Already on: no write at all.
        await _call(hass, "turn_on")
        assert len(cloud.writes) == 2
    finally:
        _stop(patches)


async def test_command_is_not_dropped_when_held_state_is_stale(hass) -> None:
    """Hot water was turned off at the boiler; HA still believes it is on.

    turn_on must not be a no-op just because the held state says "on".
    """
    cloud = _Cloud(2.0)
    patches = await _setup(hass, cloud)
    try:
        assert hass.states.get(SWITCH).state == "on"
        cloud.mode = 0.0  # changed out of band, HA has not polled yet

        await _call(hass, "turn_on")
        # The fresh read saw 0, so the write went out with the true old value.
        assert cloud.writes == [(DhwMode.ALWAYS_ACTIVE, 0)]
        assert hass.states.get(SWITCH).state == "on"
    finally:
        _stop(patches)


async def test_stale_state_is_corrected_without_a_needless_write(hass) -> None:
    """HA believes "on", the boiler is already off, the user asks for off."""
    cloud = _Cloud(2.0)
    patches = await _setup(hass, cloud)
    try:
        cloud.mode = 0.0
        await _call(hass, "turn_off")
        assert cloud.writes == []
        assert hass.states.get(SWITCH).state == "off"
    finally:
        _stop(patches)


async def test_lagging_gateway_is_confirmed_on_the_second_read(hass) -> None:
    cloud = _Cloud(2.0, lag=1)
    patches = await _setup(hass, cloud)
    try:
        await _call(hass, "turn_off")
        assert cloud.writes == [(DhwMode.DISABLED, 2)]
        assert hass.states.get(SWITCH).state == "off"
    finally:
        _stop(patches)


async def test_write_that_never_takes_effect_is_reported(hass) -> None:
    """Acknowledged but the boiler keeps the old mode: error, true state."""
    cloud = _Cloud(2.0)
    cloud.apply_writes = False
    patches = await _setup(hass, cloud)
    try:
        with pytest.raises(HomeAssistantError):
            await _call(hass, "turn_off")
        assert cloud.writes == [(DhwMode.DISABLED, 2)]
        assert hass.states.get(SWITCH).state == "on"
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


async def test_unreadable_mode_is_unknown_and_refuses_to_write(hass) -> None:
    """Without a readable current mode nothing is written."""
    cloud = _Cloud(None)
    patches = await _setup(hass, cloud)
    try:
        assert hass.states.get(SWITCH).state == "unknown"
        # On a combi the water heater must not claim "off" either.
        assert hass.states.get("water_heater.boiler_hot_water").state == "unknown"
        with pytest.raises(HomeAssistantError):
            await _call(hass, "turn_off")
        assert cloud.writes == []
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "water_heater",
                "set_operation_mode",
                {"entity_id": "water_heater.boiler_hot_water", "operation_mode": "off"},
                blocking=True,
            )
        assert cloud.writes == []
    finally:
        _stop(patches)


@pytest.mark.parametrize(
    "failure", [NtiNetError("500"), NtiNetRateLimitError("blocked for 60 seconds", 60)]
)
async def test_failed_pre_read_changes_nothing(hass, failure) -> None:
    cloud = _Cloud(2.0)
    patches = await _setup(hass, cloud)
    try:
        cloud.fail_reads = failure
        with pytest.raises(HomeAssistantError):
            await _call(hass, "turn_off")
        assert cloud.writes == []
        assert hass.states.get(SWITCH).state == "on"
    finally:
        _stop(patches)


async def test_failed_write_leaves_the_switch_unchanged(hass) -> None:
    cloud = _Cloud(2.0)
    cloud.fail_write = NtiNetError("500")
    patches = await _setup(hass, cloud)
    try:
        with pytest.raises(HomeAssistantError):
            await _call(hass, "turn_off")
        assert hass.states.get(SWITCH).state == "on"
    finally:
        _stop(patches)


async def test_unconfirmable_write_reads_unknown(hass) -> None:
    """Write acknowledged, then every confirming read fails: never a guess."""
    cloud = _Cloud(2.0)
    patches = await _setup(hass, cloud)
    try:
        original = cloud.set_mode

        async def write_then_break(gateway_id, new, old=None):
            await original(gateway_id, new, old)
            cloud.fail_reads = NtiNetError("timeout")

        with patch(
            "custom_components.nti_net.api.NtiNetApiClient.async_set_dhw_mode",
            AsyncMock(side_effect=write_then_break),
        ):
            with pytest.raises(HomeAssistantError):
                await _call(hass, "turn_off")
        assert cloud.writes == [(DhwMode.DISABLED, 2)]
        assert hass.states.get(SWITCH).state == "unknown"
    finally:
        _stop(patches)


async def test_no_switch_on_a_cylinder_appliance(hass) -> None:
    """An appliance that advertises its own DHW modes keeps the water heater only."""
    data = dict(PLANT_DATA)
    data["dMod"] = {"allowedOptions": [0, 1, 2], "value": 1}
    data["dhwBoiler"] = True
    patches = await _setup(hass, _Cloud(1.0), plant_data=data)
    try:
        assert hass.states.get(SWITCH) is None
        assert hass.states.get("water_heater.boiler_hot_water").state == "eco"
    finally:
        _stop(patches)


@pytest.mark.parametrize("flag", [{"dhwBoilerPresent": False}, {"dhwHidden": True}])
async def test_no_switch_when_hot_water_is_absent_or_hidden(hass, flag) -> None:
    features = dict(FEATURES, **flag)
    patches = await _setup(hass, _Cloud(2.0), features=features)
    try:
        assert hass.states.get(SWITCH) is None
    finally:
        _stop(patches)


def _real_client(answer: Any):
    from custom_components.nti_net.api import NtiNetApiClient

    client = NtiNetApiClient(session=None, username="u", password="p")
    client._raw_request = AsyncMock(return_value=answer)
    return client


async def test_dhw_mode_write_request_and_ack() -> None:
    client = _real_client({"success": True})
    await client.async_set_dhw_mode("TEST", new=0, old=2)
    args, kwargs = client._raw_request.call_args
    assert args == ("POST", "remote/plantData/TEST/dhwMode")
    assert kwargs["body"] == {"new": 0, "old": 2}


@pytest.mark.parametrize("answer", [None, {"success": False}, {}, "ok", []])
async def test_dhw_mode_write_without_ack_raises(answer: Any) -> None:
    """A write the cloud did not acknowledge must not look applied."""
    with pytest.raises(NtiNetError):
        await _real_client(answer).async_set_dhw_mode("TEST", new=0, old=2)


# ---------------------------------------------------------------------------
# Second review round.
# ---------------------------------------------------------------------------
def test_bsb_mode_is_trusted() -> None:
    """A BSB plant reports its mode directly; it must stay trusted."""
    from custom_components.nti_net.coordinator import NtiNetPlant

    plant = NtiNetPlant(gateway_id="B", name="BSB", raw={"bsb": {"dhwMode": 2}})
    plant.dhw_mode = 2
    assert plant.dhw_mode_trusted is True
    assert plant.is_combi_dhw is False


async def test_bsb_water_heater_keeps_its_state_and_mode_write(hass) -> None:
    """Regression: the combi guard must not blank a BSB water heater."""
    cloud = _Cloud(None)
    entry = MockConfigEntry(
        domain="nti_net",
        data={"username": "user@example.com", "password": "secret"},
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)
    client = "custom_components.nti_net.api.NtiNetApiClient"
    bsb = {"mode": 1, "outTemp": 5.0, "flame": True, "dhwTemp": 50.0,
           "dhwComfTemp": 55.0, "dhwMode": 2, "zones": []}
    with (
        patch(f"{client}.async_login", AsyncMock(return_value=True)),
        patch(f"{client}.async_get_plants", AsyncMock(return_value=[{"gwId": "TEST", "plantName": "Boiler"}])),
        patch(f"{client}.async_get_features", AsyncMock(return_value={})),
        patch(f"{client}.async_get_gateway_info", AsyncMock(return_value={"isOnline": True})),
        patch(f"{client}.async_get_plant_data", AsyncMock(return_value={})),
        patch(f"{client}.async_get_bsb_plant_data", AsyncMock(return_value=bsb)),
        patch(f"{client}.async_set_dhw_mode", AsyncMock(side_effect=cloud.set_mode)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(SWITCH) is None
        assert hass.states.get("water_heater.boiler_hot_water").state == "comfort"
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": "water_heater.boiler_hot_water", "operation_mode": "off"},
            blocking=True,
        )
        assert cloud.writes == [(0, 2)]


def test_no_combi_verdict_without_a_detail_read() -> None:
    """A plant whose detail read failed must not be taken for a combi."""
    from custom_components.nti_net.coordinator import NtiNetPlant

    assert NtiNetPlant(gateway_id="X", name="Unknown").is_combi_dhw is False


async def test_poll_in_flight_does_not_put_the_old_mode_back() -> None:
    """A write acknowledged while a poll's read is in flight wins."""
    client = FakeClient(items=_items(2.0), menu=MENU_ITEMS)
    coordinator = _coordinator(client)
    original = client.async_get_data_items

    async def read_then_write(gateway_id, items, features):
        answer = await original(gateway_id, items, features)  # read sees 2
        coordinator.note_dhw_write("TEST", 0)                 # then off is acked
        return answer

    client.async_get_data_items = read_then_write
    plant = _plant()
    plant.allowed_dhw_modes = []
    await coordinator._async_apply_diagnostics(plant)
    assert plant.dhw_mode_item == 0
    assert plant.dhw_mode == 0

    # A later, ordinary poll reads the boiler normally again.
    client.async_get_data_items = original
    client._items = _items(0.0)
    later = _plant()
    await coordinator._async_apply_diagnostics(later)
    assert later.dhw_mode_item == 0


async def test_opposite_command_during_a_long_read_lag_is_not_dropped(hass) -> None:
    """Off is applied but reads keep saying on; on must still be sent."""
    cloud = _Cloud(2.0, lag=5)
    patches = await _setup(hass, cloud)
    try:
        with pytest.raises(HomeAssistantError):
            await _call(hass, "turn_off")  # applied, but not confirmed in time
        assert cloud.writes == [(DhwMode.DISABLED, 2)]

        # The pre-read still says 2.  The acknowledged 0 outranks it.
        cloud.lag = 0
        cloud._stale_reads = 1
        await _call(hass, "turn_on")
        assert cloud.writes[-1] == (DhwMode.ALWAYS_ACTIVE, 0)
        assert cloud.mode == 2.0
        assert hass.states.get(SWITCH).state == "on"
    finally:
        _stop(patches)


async def test_write_lost_in_transit_shows_what_the_boiler_says(hass) -> None:
    """The write times out after the boiler applied it."""
    from custom_components.nti_net.api import NtiNetConnectionError

    cloud = _Cloud(2.0)
    patches = await _setup(hass, cloud)
    try:
        original = cloud.set_mode

        async def applied_then_timeout(gateway_id, new, old=None):
            await original(gateway_id, new, old)
            raise NtiNetConnectionError("Request timed out")

        with patch(
            "custom_components.nti_net.api.NtiNetApiClient.async_set_dhw_mode",
            AsyncMock(side_effect=applied_then_timeout),
        ):
            with pytest.raises(HomeAssistantError):
                await _call(hass, "turn_off")
        assert hass.states.get(SWITCH).state == "off"  # the truth, not the old "on"
    finally:
        _stop(patches)


# ---------------------------------------------------------------------------
# Third review round: a recent write must never turn a command into a no-op.
# ---------------------------------------------------------------------------
async def test_retry_after_an_unapplied_write_is_sent_again(hass) -> None:
    """Off is acknowledged but the boiler ignores it; the retry must write."""
    cloud = _Cloud(2.0)
    cloud.apply_writes = False
    patches = await _setup(hass, cloud)
    try:
        with pytest.raises(HomeAssistantError):
            await _call(hass, "turn_off")
        assert cloud.writes == [(DhwMode.DISABLED, 2)]
        assert hass.states.get(SWITCH).state == "on"

        # The boiler now behaves.  The retry is the natural reaction to the
        # error above and must not be swallowed by the 60 s memory.
        cloud.apply_writes = True
        await _call(hass, "turn_off")
        assert len(cloud.writes) == 2
        assert cloud.writes[-1][0] == DhwMode.DISABLED
        assert cloud.mode == 0.0
        assert hass.states.get(SWITCH).state == "off"
    finally:
        _stop(patches)


async def test_switch_after_a_water_heater_change_is_not_dropped(hass) -> None:
    """Switch off, water heater back to comfort, switch off again."""
    cloud = _Cloud(2.0)
    patches = await _setup(hass, cloud)
    try:
        await _call(hass, "turn_off")
        assert cloud.mode == 0.0

        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": "water_heater.boiler_hot_water", "operation_mode": "comfort"},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert cloud.mode == 2.0

        await _call(hass, "turn_off")
        assert cloud.writes[-1] == (DhwMode.DISABLED, 2)
        assert cloud.mode == 0.0
        assert hass.states.get(SWITCH).state == "off"
    finally:
        _stop(patches)


async def test_external_change_right_after_a_write_is_not_dropped(hass) -> None:
    """HA turns it off, the vendor app turns it on, HA is asked for off again."""
    cloud = _Cloud(2.0)
    patches = await _setup(hass, cloud)
    try:
        await _call(hass, "turn_off")
        cloud.mode = 2.0  # changed in the app, seconds later
        await _call(hass, "turn_off")
        assert len(cloud.writes) == 2
        assert cloud.mode == 0.0
        assert hass.states.get(SWITCH).state == "off"
    finally:
        _stop(patches)
