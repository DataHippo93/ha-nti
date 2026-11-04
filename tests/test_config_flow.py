"""Config flow tests, including a live credential check."""
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "nti_net"


from helpers import load_credentials as _creds


async def test_form_shown(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    print("\nFORM:", result["errors"])


async def test_bad_credentials(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "nobody@example.com", "password": "wrong-password-xyz"}
    )
    print("\nBAD CREDS:", result["type"], result["errors"])
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_live_credentials(hass: HomeAssistant):
    usr, pwd = _creds()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": usr, "password": pwd})
    print("\nLIVE:", result["type"], result.get("title"), result.get("errors"))
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == usr


async def test_duplicate_aborts(hass: HomeAssistant):
    usr, pwd = _creds()
    first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    first = await hass.config_entries.flow.async_configure(first["flow_id"], {"username": usr, "password": pwd})
    assert first["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY

    second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    second = await hass.config_entries.flow.async_configure(second["flow_id"], {"username": usr, "password": pwd})
    print("\nDUP:", second["type"], second["reason"])
    assert second["type"] is data_entry_flow.FlowResultType.ABORT
    assert second["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant):
    usr, pwd = _creds()
    entry = MockConfigEntry(domain=DOMAIN, data={"username": usr, "password": pwd}, unique_id=usr.lower())
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    print("\nOPTIONS FORM:", result["type"], result["step_id"])
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"scan_interval": 600})
    print("OPTIONS SAVE:", result["type"], dict(entry.options) if False else "")
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options["scan_interval"] == 600
