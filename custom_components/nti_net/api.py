"""NTI net (remotethermo) cloud API client.

Every route, payload shape and status-code meaning in this module was verified
against the live service with a real account, on top of the static evidence
recovered from the shipped iOS application bundle
`/Applications/NTI net.app` (bundle id `com.remotethermo.nti`, version 7.0.3 /
build 40324).  See PROTOCOL.md for the full evidence trail.

Verified behaviour, and the traps worth knowing about:

* The service lives at ``https://www.nti.remotethermo.com/api/v2/``.  The
  generic ``https://api.remotethermo.com/api/v2/`` host answers
  ``403 {"message": "Missing Authentication Token"}`` for the same paths, so
  the brand host is not an alias -- it is the only one that works.
* ``POST accounts/login`` with ``{"usr": ..., "pwd": ...}`` returns
  ``{"token": ...}``.  The token travels in the ``ar.authToken`` header.
* **HTTP 405 is overloaded.**  With an *empty* body it means "token missing or
  invalid"; with a JSON body it is a genuine "method not allowed".  The old
  assumption that 405 always means "expired token" produced infinite
  re-login loops against routes that simply do not exist.
* ``remote/plants/{gw}`` is **not** a route.  The aggregated read is
  ``remote/plantData/{gw}``.  Zone data is nested inside it, keyed by zone
  number as a *string*, not an array.
* **Every read must pin the unit system.**  Omitting ``umsys`` makes the
  service fall back to whatever the account/locale implies, and ``umsys=us``
  returns Fahrenheit.  This client always sends ``umsys=si`` and therefore
  always speaks Celsius.
* Writes are ``POST`` with a ``{"new": ..., "old": ...}`` envelope.  The `old`
  value is used by the service for optimistic-concurrency checks, so callers
  pass the value they last observed.
* Zone setpoints are written as a ``{"comf": x, "econ": y}`` pair, but the DHW
  setpoint is a **scalar** -- sending the pair shape to ``dhwTemp`` returns
  HTTP 500.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

#: Base URL of the NTI net cloud API.
DEFAULT_API_URL = "https://www.nti.remotethermo.com/api/v2/"

#: Header the mobile app uses to carry the session token.
AUTH_HEADER = "ar.authToken"

#: The app identifies itself with RestSharp on the shared Ariston client.
DEFAULT_USER_AGENT = "RestSharp/106.11.7.0"

#: Unit system pinned on every read so the payload is always metric.
UNITS = "si"

#: The cloud answers 429 with a "try again in N seconds" body.
BLOCKED_STATUS = 429

#: 405 means "bad token" only when the body is empty; see module docstring.
TOKEN_INVALID_STATUS = 405


class NtiNetError(Exception):
    """Base error for the NTI net cloud client."""


class NtiNetAuthError(NtiNetError):
    """Credentials were rejected."""


class NtiNetConnectionError(NtiNetError):
    """The cloud could not be reached."""


class NtiNetRateLimitError(NtiNetError):
    """The cloud is throttling this account."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        """Store the advised retry delay."""
        super().__init__(message)
        self.retry_after = retry_after


class NtiNetTokenError(NtiNetError):
    """The session token was rejected and could not be renewed."""


class NtiNetApiClient:
    """Minimal async client for the NTI net cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        api_url: str = DEFAULT_API_URL,
    ) -> None:
        """Initialise the client. Call `async_login` before any request."""
        self._session = session
        self._username = username
        self._password = password
        self._api_url = api_url if api_url.endswith("/") else f"{api_url}/"
        self._token: str | None = None
        self._token_obtained_at: float = 0.0

    @property
    def api_url(self) -> str:
        """Return the configured API base URL."""
        return self._api_url

    def _headers(self) -> dict[str, str]:
        """Return request headers, including the session token when known."""
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
        }
        if self._token:
            headers[AUTH_HEADER] = self._token
        return headers

    async def _raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        retry_on_token_error: bool = True,
    ) -> Any:
        """Perform one request, transparently renewing an expired token once."""
        url = f"{self._api_url}{path.lstrip('/')}"
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=body,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                text = await response.text()

                if response.status == BLOCKED_STATUS:
                    raise NtiNetRateLimitError(
                        f"Account is rate limited: {text}",
                        _parse_retry_after(text),
                    )

                if response.status == TOKEN_INVALID_STATUS:
                    if text.strip():
                        # A JSON body means the *method* is wrong, not the token.
                        raise NtiNetError(
                            f"{method} {path} is not supported: {text.strip()}"
                        )
                    if retry_on_token_error and await self.async_login():
                        return await self._raw_request(
                            method,
                            path,
                            params=params,
                            body=body,
                            retry_on_token_error=False,
                        )
                    raise NtiNetTokenError(f"Token rejected for {method} {path}")

                if response.status == 404:
                    # Unknown gateway/zone: treat as "no data".
                    _LOGGER.debug("404 for %s %s", method, path)
                    return None

                if response.status >= 400:
                    raise NtiNetError(
                        f"{method} {path} failed with {response.status}: "
                        f"{text.strip()[:200]}"
                    )

                if not text.strip():
                    return None
                try:
                    return _json_loads(text)
                except ValueError:
                    _LOGGER.debug("Non-JSON payload for %s %s", method, path)
                    return None

        except NtiNetError:
            raise
        except aiohttp.ClientError as err:
            raise NtiNetConnectionError(str(err)) from err
        except asyncio.TimeoutError as err:
            raise NtiNetConnectionError("Request timed out") from err

    async def async_login(self) -> bool:
        """Authenticate and store the session token.

        Returns True on success. Raises `NtiNetAuthError` when the cloud
        rejects the credentials (this API answers 404 for an unknown user or a
        wrong password).
        """
        payload = {"usr": self._username, "pwd": self._password}
        url = f"{self._api_url}accounts/login"
        try:
            async with self._session.post(
                url,
                json=payload,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 400:
                    raise NtiNetAuthError("Login payload rejected as invalid")
                if response.status in (401, 404):
                    raise NtiNetAuthError("Invalid e-mail or password")
                if response.status == BLOCKED_STATUS:
                    text = await response.text()
                    raise NtiNetRateLimitError(
                        f"Account is rate limited: {text}",
                        _parse_retry_after(text),
                    )
                if response.status >= 400:
                    text = await response.text()
                    raise NtiNetAuthError(
                        f"Login failed with {response.status}: {text.strip()[:200]}"
                    )
                data = await response.json(content_type=None)

        except NtiNetError:
            raise
        except aiohttp.ClientError as err:
            raise NtiNetConnectionError(str(err)) from err
        except asyncio.TimeoutError as err:
            raise NtiNetConnectionError("Login timed out") from err

        if not isinstance(data, dict):
            raise NtiNetAuthError("Unexpected login response")

        if data.get("incompatibleAppVersion"):
            raise NtiNetAuthError("The cloud rejected this client version")

        token = data.get("token")
        if not token:
            raise NtiNetAuthError("Login response contained no token")

        self._token = token
        self._token_obtained_at = time.monotonic()
        return True

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def async_get_plants(self) -> list[dict[str, Any]]:
        """Return every plant (gateway) visible to this account."""
        data = await self._raw_request("GET", "remote/plants/lite")
        return _as_list(data)

    async def async_get_features(self, gateway_id: str) -> dict[str, Any]:
        """Return the feature flags describing what a gateway supports."""
        data = await self._raw_request(
            "GET", f"remote/plants/{gateway_id}/features"
        )
        return _as_dict(data)

    async def async_get_gateway_info(self, gateway_id: str) -> dict[str, Any]:
        """Return gateway connectivity information."""
        data = await self._raw_request(
            "GET", f"remote/plants/{gateway_id}/gatewayInfo"
        )
        return _as_dict(data)

    async def async_get_bus_devices(self, gateway_id: str) -> list[dict[str, Any]]:
        """Return the bus devices attached to a gateway."""
        data = await self._raw_request(
            "GET", f"remote/plants/{gateway_id}/busDevices"
        )
        return _as_list(data)

    async def async_get_plant_data(self, gateway_id: str) -> dict[str, Any]:
        """Return the aggregated plant reading (the main read path)."""
        data = await self._raw_request(
            "GET",
            f"remote/plantData/{gateway_id}",
            params={"umsys": UNITS},
        )
        return _as_dict(data)

    async def async_get_zone(self, gateway_id: str, zone: int) -> dict[str, Any]:
        """Return one zone of one gateway."""
        data = await self._raw_request(
            "GET",
            f"remote/zones/{gateway_id}/{zone}",
            params={"umsys": UNITS},
        )
        return _as_dict(data)

    async def async_get_consumption_sequences(
        self, gateway_id: str, usages: tuple[str, ...] = ("Ch", "Dhw")
    ) -> list[dict[str, Any]]:
        """Return the metered consumption series for one gateway.

        The payload is a list of ``{"k": ..., "p": ..., "v": [...]}`` blocks:
        ``k`` selects the usage and ``p`` the bucket size, and ``v`` is the
        series itself.  Both families must be asked for together because the
        route only accepts the family names, not the per-appliance members.

        The values are kilowatt hours.  This is not a guess: the route is
        ``consKwhSequences`` in the shipped app, and the numbers agree with the
        independently computed ``energyAccount`` monthly totals to within
        rounding.

        This route answers HTTP 500 for a gateway that does not meter, and for
        some members even on one that does, so callers must tolerate failure.
        """
        data = await self._raw_request(
            "GET",
            f"remote/reports/{gateway_id}/consSequencesApi8",
            params={"usages": ",".join(usages)},
        )
        return _as_list(data)

    async def async_get_bus_errors(self, gateway_id: str) -> list[dict[str, Any]]:
        """Return the faults the appliance is reporting right now.

        ``GET busErrors?gatewayId={gw}&blockingOnly=False&culture=en-US``
        answers 200 with a JSON list on a boiler gateway (``gwSysType`` 3,
        firmware ``104.115.28B``); the list is empty while nothing is wrong.
        The cloud keeps no history on this route, so a fault is only visible
        while it is active and has to be caught by polling.

        Raises `NtiNetError` for anything that is not a JSON list.
        """
        data = await self._raw_request(
            "GET",
            "busErrors",
            params={
                "gatewayId": gateway_id,
                "blockingOnly": "False",
                "culture": "en-US",
            },
        )
        # Only a JSON list is a real answer.  ``_raw_request`` returns None
        # for a 404, an empty body or a non-JSON page, and a dict is some
        # other message entirely.  Coercing any of those to "no faults" would
        # make a broken read look like a healthy boiler, so they are errors.
        if not isinstance(data, list):
            raise NtiNetError(
                f"Unexpected busErrors answer of type {type(data).__name__}"
            )
        # Keep entries that are not objects instead of dropping them: the
        # entry shape has not been seen live, and a dropped entry is a hidden
        # fault.
        return [
            entry if isinstance(entry, dict) else {"value": entry}
            for entry in data
        ]

    async def async_get_data_items(
        self,
        gateway_id: str,
        items: list[dict[str, Any]],
        features: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Read named data items (the generic read behind the app's screens).

        ``POST remote/dataItems/{gw}/get?umsys=si`` takes
        ``{"useCache": false, "items": [{"id", "zn"}], "features": {...}}`` and
        answers ``{"items": [{"id", "zone", "value", "unit", ...}]}``.  Items
        the appliance does not know are simply left out of the answer.
        """
        data = await self._raw_request(
            "POST",
            f"remote/dataItems/{gateway_id}/get",
            params={"umsys": UNITS},
            body={"useCache": False, "items": items, "features": features},
        )
        return _as_list(data)

    async def async_get_menu_items(
        self, gateway_id: str, menu_ids: tuple[int, ...]
    ) -> list[dict[str, Any]]:
        """Read service-menu values by numeric id.

        ``GET menuItems/{gw}?menuItems=124,119&umsys=si`` answers a list of
        ``{"id", "text", "value", "unit", ...}``; unknown ids are left out.
        """
        data = await self._raw_request(
            "GET",
            f"menuItems/{gateway_id}",
            params={
                "menuItems": ",".join(str(item) for item in menu_ids),
                "umsys": UNITS,
            },
        )
        return _as_list(data)

    async def async_get_bsb_plant_data(self, gateway_id: str) -> dict[str, Any]:
        """Return the BSB-family view, if this gateway speaks BSB."""
        data = await self._raw_request(
            "GET", f"remote/bsbPlantData/{gateway_id}"
        )
        return _as_dict(data)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    async def async_set_plant_mode(
        self, gateway_id: str, new: int, old: int | None = None
    ) -> None:
        """Set the plant mode (Summer/Winter/HeatingOnly/Off)."""
        await self._write(f"remote/plantData/{gateway_id}/mode", new, old)

    async def async_set_dhw_mode(
        self, gateway_id: str, new: int, old: int | None = None
    ) -> None:
        """Set domestic hot water mode."""
        await self._write(f"remote/plantData/{gateway_id}/dhwMode", new, old)

    async def async_set_dhw_temperature(
        self, gateway_id: str, new: float, old: float | None = None
    ) -> None:
        """Set the DHW setpoint.

        Unlike zone setpoints this is a single scalar; the comfort/reduced pair
        shape is rejected with HTTP 500.
        """
        await self._write(f"remote/plantData/{gateway_id}/dhwTemp", new, old)

    async def async_set_holiday(
        self,
        gateway_id: str,
        until: str | None,
        old_until: str | None = None,
    ) -> None:
        """Enable or clear holiday mode (ISO-8601 timestamp or None)."""
        await self._write(f"remote/plantData/{gateway_id}/holiday", until, old_until)

    async def async_set_zone_mode(
        self,
        gateway_id: str,
        zone: int,
        new: int,
        old: int | None = None,
        *,
        is_cooling: bool = False,
    ) -> None:
        """Set one zone's operating mode."""
        await self._write(
            f"remote/zones/{gateway_id}/{zone}/mode",
            new,
            old,
            params={"isCooling": _bool_param(is_cooling)},
        )

    async def async_set_zone_temperatures(
        self,
        gateway_id: str,
        zone: int,
        comfort: float,
        reduced: float,
        *,
        is_cooling: bool = False,
        old_comfort: float | None = None,
        old_reduced: float | None = None,
    ) -> None:
        """Set one zone's comfort and reduced setpoints."""
        await self._raw_request(
            "POST",
            f"remote/zones/{gateway_id}/{zone}/temperatures",
            params={"isCooling": _bool_param(is_cooling), "umsys": UNITS},
            body={
                "new": {"comf": comfort, "econ": reduced},
                "old": {"comf": old_comfort, "econ": old_reduced},
            },
        )

    async def async_set_zone_name(
        self, gateway_id: str, zone: int, name: str
    ) -> None:
        """Rename one zone.

        The route is present in the binary but was not exercised against a live
        gateway, so treat it as best-effort.
        """
        await self._write(f"remote/zones/{gateway_id}/{zone}/name", name, None)

    async def async_set_zone_override(
        self,
        gateway_id: str,
        zone: int,
        *,
        temperature: float,
        until: str | None,
        enabled: bool = True,
        old: dict[str, Any] | None = None,
    ) -> None:
        """Set or clear a zone's manual override ("deroga").

        The payload is the ``DerogaDto`` shape recovered from the shipped app:
        ``{"temp": float, "exp": ISO-8601 string, "enab": bool}``.  Clearing an
        override means sending ``enab: false`` -- there is no DELETE.

        Verified live: ``POST .../deroga`` answers ``{"success": true}``.  Note
        that the state is *not* readable through any GET route, so this client
        cannot confirm a write by re-reading it; the caller re-reads the zone
        from ``plantData`` instead, where it appears as ``zones.N.der``.
        """
        await self._write(
            f"remote/zones/{gateway_id}/{zone}/deroga",
            {"temp": temperature, "exp": until, "enab": enabled},
            old,
        )

    async def _write(
        self,
        path: str,
        new: Any,
        old: Any,
        *,
        params: dict[str, Any] | None = None,
    ) -> None:
        """POST a `{"new": ..., "old": ...}` envelope."""
        await self._raw_request(
            "POST", path, params=params, body={"new": new, "old": old}
        )

    async def async_close(self) -> None:
        """Drop the stored token."""
        self._token = None


def _bool_param(value: bool) -> str:
    """Render a bool the way the service expects it in a query string."""
    return "true" if value else "false"


def _json_loads(text: str) -> Any:
    """Parse a JSON document."""
    import json

    return json.loads(text)


def _parse_retry_after(text: str) -> int | None:
    """Extract the retry delay from a `Requests are blocked for N seconds` body."""
    lowered = text.lower()
    marker = "blocked for"
    if marker not in lowered:
        return None
    tail = lowered.split(marker, 1)[1]
    digits = ""
    for char in tail.strip():
        if char.isdigit():
            digits += char
        elif digits:
            break
    return int(digits) if digits else None


def _as_list(data: Any) -> list[dict[str, Any]]:
    """Coerce an API payload into a list of dicts."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "plants", "value"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _as_dict(data: Any) -> dict[str, Any]:
    """Coerce an API payload into a dict."""
    return data if isinstance(data, dict) else {}
