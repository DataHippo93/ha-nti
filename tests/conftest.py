import socket
from unittest.mock import patch

import aiohttp
import pytest

NTI_HOST = "www.nti.remotethermo.com"
_real_getaddrinfo = socket.getaddrinfo


@pytest.fixture(autouse=True)
def _allow_nti_host(monkeypatch):
    """Let the live-cloud tests reach the NTI host.

    The HA test harness disables DNS and sockets by default. These are opt-in
    integration tests against the real service, so re-enable exactly the hosts
    that are needed: the NTI cloud, plus localhost for the test HTTP client.
    """
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: _real_getaddrinfo(host, *a, **k),
    )
    try:
        import pytest_socket

        pytest_socket.enable_socket()
        pytest_socket.socket_allow_hosts(
            [NTI_HOST, "127.0.0.1", "::1"], allow_unix_socket=True
        )
    except Exception:  # noqa: BLE001 - best effort
        pass
    yield


@pytest.fixture(autouse=True)
def _threaded_dns():
    holder = {}

    def _make(hass):
        if "r" not in holder:
            holder["r"] = aiohttp.ThreadedResolver()
        return holder["r"]

    with patch(
        "homeassistant.helpers.aiohttp_client._async_get_or_create_resolver",
        side_effect=_make,
    ):
        yield


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Load custom_components/ from the test config dir for every test."""
    yield
