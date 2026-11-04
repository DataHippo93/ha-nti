"""Verify Home Assistant serves the integration's brand icon."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component


async def test_brand_icon_served(hass: HomeAssistant, hass_client):
    """The brands endpoint should return our PNG, not a CDN placeholder.

    This only needs the integration to be discoverable by the loader; it does
    not need an account or a config entry.
    """
    assert await async_setup_component(hass, "brands", {})
    await hass.async_block_till_done()

    client = await hass_client()
    for name, expected_size in (("icon.png", 256), ("icon@2x.png", 512)):
        resp = await client.get(f"/api/brands/integration/nti_net/{name}")
        body = await resp.read()
        print(f"\n{name}: status={resp.status} type={resp.content_type} bytes={len(body)}")
        assert resp.status == 200, f"{name} not served"
        assert resp.content_type == "image/png"
        assert body[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        width = int.from_bytes(body[16:20], "big")
        height = int.from_bytes(body[20:24], "big")
        print(f"  dimensions: {width}x{height}")
        assert (width, height) == (expected_size, expected_size)
