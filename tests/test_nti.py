"""Live end-to-end test of the NTI net integration against the real cloud."""
import json

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from helpers import load_credentials as _creds

DOMAIN = "nti_net"


async def test_setup_and_entities(hass: HomeAssistant):
    usr, pwd = _creds()
    entry = MockConfigEntry(domain=DOMAIN, data={"username": usr, "password": pwd}, unique_id=usr.lower())
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    print("\nENTRY STATE:", entry.state)
    assert entry.state is ConfigEntryState.LOADED
    coord = entry.runtime_data
    print("PLANTS:", {gw: (p.name, p.model, p.has_dhw) for gw, p in coord.data.items()})

    from homeassistant.helpers import entity_registry as er_mod
    reg = er_mod.async_get(hass)
    ents = [s for s in hass.states.async_all() if reg.async_get(s.entity_id) and reg.async_get(s.entity_id).platform == 'nti_net']
    for s in sorted(ents, key=lambda x: x.entity_id):
        print(f"  {s.entity_id} = {s.state}   {json.dumps({k: v for k, v in s.attributes.items() if k in ('friendly_name','current_temperature','temperature','hvac_modes','hvac_action','min_temp','max_temp','options','current_option','unit_of_measurement','operation_list','current_operation')}, default=str)}")

    assert ents, "no entities created"

    # Exercise a real write through the climate entity: echo the current setpoint back.
    climates = [s for s in ents if s.entity_id.startswith('climate.')]
    if climates:
        c = climates[0]
        target = c.attributes.get('temperature')
        print("setting climate", c.entity_id, "to", target)
        await hass.services.async_call('climate', 'set_temperature', {'entity_id': c.entity_id, 'temperature': target}, blocking=True)
        await hass.async_block_till_done()
        print("  after write:", hass.states.get(c.entity_id).state, hass.states.get(c.entity_id).attributes.get('temperature'))

    # Exercise the plant mode select, rewriting the current option.
    selects = [s for s in ents if s.entity_id.startswith('select.')]
    if selects:
        s0 = selects[0]
        cur = s0.state
        print("selecting", s0.entity_id, "->", cur, "options", s0.attributes.get('options'))
        await hass.services.async_call('select', 'select_option', {'entity_id': s0.entity_id, 'option': cur}, blocking=True)
        await hass.async_block_till_done()
        print("  after write:", hass.states.get(s0.entity_id).state)

    # Water heater, rewriting the current setpoint.
    whs = [s for s in ents if s.entity_id.startswith('water_heater.')]
    if whs:
        w = whs[0]
        t = w.attributes.get('temperature')
        print("setting water heater", w.entity_id, "to", t)
        await hass.services.async_call('water_heater', 'set_temperature', {'entity_id': w.entity_id, 'temperature': t}, blocking=True)
        await hass.async_block_till_done()
        print("  after write:", hass.states.get(w.entity_id).state, hass.states.get(w.entity_id).attributes.get('temperature'))

        op = w.attributes.get('operation_list')[0] if w.attributes.get('operation_list') else None
        if op:
            await hass.services.async_call('water_heater', 'set_operation_mode', {'entity_id': w.entity_id, 'operation_mode': w.state}, blocking=True)
            await hass.async_block_till_done()
            print("  mode after write:", hass.states.get(w.entity_id).state)
