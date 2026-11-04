# NTI net cloud protocol

This documents the HTTP API that the NTI net mobile app talks to, as recovered
from the shipped app bundle and confirmed against the live service.

## Evidence sources

| Source | What it gave |
| --- | --- |
| `/Applications/NTI net.app/Wrapper/NTI net.app` (Mach-O arm64, 83.9 MB) | Route table, DTO field names, mode enums, auth header name |
| `re/strings/utf16.txt` | 25,575 unique UTF-16 strings (the .NET/Kotlin string table; the `LC_ENCRYPTION_INFO_64` record covers only the first 4096 bytes, so this data is readable) |
| `re/strings/binary.txt` | 118k ASCII strings |
| Live probing with a real account | Every route, payload shape and status code marked **verified** below |

App identity: bundle id `com.remotethermo.nti`, version 7.0.3 (build 40324),
iOS wrapper for an Apple Silicon build. The Android analogue is
`com.remotethermo.aristonnet`, and the shared Xcode project is `iosAriston`,
which is why so much of the payload vocabulary is Ariston's rather than NTI's.

## Base URL

```
https://www.nti.remotethermo.com/api/v2/
```

The generic host `https://api.remotethermo.com/api/v2/` answers
`403 {"message":"Missing Authentication Token"}` for every identical path.
It is **not** a working alias — the brand-specific host is the only one that
serves these routes.

`GET https://www.nti.remotethermo.com/api/v1/status` returns
`{"message":"test","aqua_blocked":false,"code":0}` and needs no auth, which is
a cheap reachability probe.

## Authentication

`POST accounts/login`

```json
{"usr": "user@example.com", "pwd": "..."}
```

Response:

```json
{"token": "<redacted>"}
```

The login DTO in the binary is `LoginRequestDto{usr, pwd, imp, notTrack}` and
the result is `LoginResultDto{token, act, incompatibleAppVersion}` (`imp` and
`notTrack` are optional telemetry flags; the `incompatibleAppVersion` flag, when
set, must be treated as a hard failure).

Every subsequent request carries the token in the **`ar.authToken`** header:

```
ar.authToken: <redacted>
```

Login status codes (verified):

| Status | Meaning |
| --- | --- |
| 200 | Success, body has `token` |
| 400 | Payload missing `usr`/`pwd` |
| 404 | Unknown user **or** wrong password |
| 500 | Malformed JSON body |

### Not used: OAuth / Cognito

The binary also contains `https://auth.remotethermo.com/oauth2/token` with a
client id/secret pair and a `client_credentials` grant that succeeds, yielding
scope `ariston/api ariston/midas.read ariston/status.read
ariston/notifications` (Cognito pool `eu-west-1_SotkrQPbg`). Those tokens are
**rejected** by the API Gateway (`403`), so this integration does not use that
flow. It is recorded here only so nobody else wastes time on it.

## Status code conventions

These are the traps in this API.

### 405 is overloaded

| Body | Meaning |
| --- | --- |
| **empty** | Token missing, invalid or expired |
| JSON `{"Message": "The requested resource does not support http method 'GET'."}` | Genuinely wrong method |

Assuming "405 always means expired token" causes an infinite re-login loop
against routes that simply do not exist. The client distinguishes on whether
the body is empty.

### 429 is rate limiting

`429 Requests are blocked for N seconds` — the value is parsed out and surfaced
as the retry delay. The vendor app is throttled per account, and running two
clients against one account at the same time causes lockouts. The integration
therefore polls no faster than `MIN_SCAN_INTERVAL` (180 s) and defaults to
300 s.

### 404

Unknown gateway or zone. Treated as "no data" rather than an error.

## Unit system

Every read must pin `umsys`:

| `umsys` | Result |
| --- | --- |
| `si` | Celsius |
| `us` | **Fahrenheit** (`dComf.min` becomes `104.0` instead of `40.0`) |
| omitted | Falls back to the account/locale default |

This integration always sends `umsys=si` and therefore always speaks Celsius.

## Routes

`{gw}` is the gateway id (`gwId`, a 12-character hex string), `{z}` a zone
number.

### Reads (all verified)

| Route | Returns |
| --- | --- |
| `GET remote/plants/lite` | Array of plant summaries |
| `GET remote/plants/{gw}/features` | Feature flags **and the per-zone list** |
| `GET remote/plants/{gw}/gatewayInfo` | `{"isOnline": true, "isOffline48H": false}` |
| `GET remote/plants/{gw}/busDevices` | Attached bus hardware |
| `GET remote/plantData/{gw}?umsys=si` | **The main aggregated read** |
| `GET remote/zones/{gw}/{z}?umsys=si` | One zone, same shape as embedded in `plantData` |
| `POST remote/dataItems/{gw}/get?umsys=si` | Generic named-item read |
| `POST remote/plants/{gw}/getConsumptionsSettings` | Energy display preferences |
| `GET remote/reports/{gw}/consSequencesApi8?usages=Ch,Dhw` | Metered consumption series |
| `GET remote/reports/{gw}/energyAccount` | Last month's gas/electric totals |

Routes recovered from the binary that were **not** confirmed against this
gateway:

`remote/bsbPlantData/{gw}`, `remote/bsbZones/{gw}/{z}`,
`remote/bsbHolidays/{gw}/{z}`, `remote/bsbTimeProgs/{gw}/{z}`,
`remote/timeProgs/{gw}/{z}`, `remote/zones/{gw}/{z}/deroga`,
`remote/zones/{gw}/{z}/fireplace`, `remote/zones/{gw}/{z}/pilot`,
`remote/zones/{gw}/nameOptions`,
`remote/zones/{gw}/virtualZonesVisibility`,
`remote/plants/{gw}/reset`, `remote/plants/{gw}/name`,
`remote/plants/{gw}/measurementSystem`,
`remote/plants/{gw}/consumptionsSettings`,
`remote/plants/{gw}/smartComfortPreferences`,
`remote/plants/{gw}/errorNotificationSettings`,
`remote/plants/{gw}/engagementNotificationSettings`,
`remote/actions/{gw}/mostRecent`,
`remote/reports/{gw}/{co2Report,dailyRoomTemp,prodSequencesApi8,reset}`,
`remote/velis/…`.

On the test gateway these BSB-family routes answered HTTP 500
(`{"Message":"An error has occurred."}`), which is what an appliance that
doesn't speak that protocol looks like. `remote/plants/{gw}` is **not a
route at all** — GET/POST/PUT all answer 405. An earlier reconstruction
assumed it existed; it does not. The aggregated read is `remote/plantData`.

### `remote/plants/lite` (verified)

```json
[
  {
    "umSys": 0,
    "gwId": "0123456789AB",
    "gwSerial": "123456A12345",
    "plantName": "Boiler",
    "location": {
      "country": "XX",
      "addr": "1 Example Street",
      "cityName": "Example City",
      "city": 1000000,
      "adm1Name": "Example Region",
      "adm1": 1000001,
      "adm2Name": "Example County",
      "adm2": 1000002,
      "postalCode": "A1A 1A1",
      "lat": 0.0,
      "lng": 0.0,
      "radius": 0.0
    },
    "gwSysType": 3,
    "utcOffset": -420,
    "gwLink": 1,
    "gwFwVer": "104.115.28B",
    "controlledByGuest": false,
    "mqttApiVersion": 9
  }
]
```

The `location` block is precise enough to be a privacy concern, so the
integration never stores or logs it. Values above are placeholders; the field
names and types are what the service actually returns.

### `remote/plants/{gw}/features` (verified)

```json
{
  "zones": [
    {"num": 1, "name": "", "roomSens": false, "geofenceDeroga": false, "isHidden": false},
    {"num": 2, "name": "", "roomSens": false, "geofenceDeroga": false, "isHidden": true},
    {"num": 3, "name": "", "roomSens": false, "geofenceDeroga": false, "isHidden": true}
  ],
  "hasBoiler": true,
  "dhwModeChangeable": true,
  "autoThermoReg": true,
  "hasMetering": true,
  "weatherProvider": 1,
  "hasDhwTimeProgTemperatures": 2,
  "virtualZones": true
}
```

Notes that matter:

* This is where zone names, `roomSens` and `isHidden` live. `plantData` does
  not repeat them, so a zone's name is only available here.
* `isHidden: true` marks virtual/unused zones; the integration skips them.
* `roomSens: false` means the zone has no room probe, and its `room` reading
  comes back as `0.0`.

### `remote/plantData/{gw}?umsys=si` (verified)

```json
{
  "gw": "0123456789AB",
  "mod":  {"allowedOptions": [0, 1, 2, 5], "value": 1},
  "dMod": {"allowedOptions": [0, 1, 2], "value": 2},
  "dComf": {"prevValue": 0.0, "step": 1.0, "min": 40.0, "max": 65.0, "value": 53.0},
  "out": 4.0,
  "zones": {
    "1": {
      "mod": {"allowedOptions": [2, 3], "value": 3},
      "room": 0.0,
      "desired": 19.0,
      "comf": {"prevValue": 0.0, "step": 0.5, "min": 10.0, "max": 30.0, "value": 19.0},
      "econ": {"prevValue": 0.0, "step": 0.5, "min": 10.0, "max": 30.0, "value": 16.0},
      "heat": false,
      "der": {"temp": 0.0, "exp": "1970-01-01T00:00:00", "enab": false},
      "antiFreezeTemp": 5.0,
      "heatReqAir": false,
      "pilot": false
    }
  },
  "auto": true,
  "flame": false,
  "dhwDisabled": false,
  "hpOn": false,
  "dhwStoreT": 55.0,
  "dhwBoiler": true,
  "dhwTProgComfortTemp": {"step": 1.0, "min": 40.0, "max": 65.0, "value": 53.0},
  "dhwTProgEconomyTemp": {"step": 1.0, "min": 10.0, "max": 53.0, "value": 30.0},
  "boilerVer": 1,
  "weather": 131,
  "flameForDhw": false
}
```

Field notes:

| Field | Meaning |
| --- | --- |
| `mod.value` / `mod.allowedOptions` | Plant mode and the modes it accepts |
| `dMod` | DHW mode |
| `dComf` | DHW comfort setpoint with served min/max/step |
| `dhwTProgEconomyTemp` | DHW economy setpoint |
| `dhwStoreT` | Measured cylinder temperature |
| `dhwBoiler` | Whether a cylinder is present |
| `out` | Outside temperature |
| `flame` | Burner lit |
| `hpOn` | Heating pump running |
| `zones` | **Object keyed by zone number as a string**, not an array |
| `zones.{n}.room` | Room temperature, `0.0` when there is no probe |
| `zones.{n}.desired` | Active setpoint |
| `zones.{n}.comf` / `econ` | Comfort and reduced setpoints, with min/max/step |
| `zones.{n}.heat` | Zone is requesting heat |
| `zones.{n}.der` | Manual override ("deroga"): temp, expiry, enabled |

Wrapping convention: control values are `{"value", "min", "max", "step"}` or
`{"value", "allowedOptions"}` objects; measurement scalars (`out`, `room`,
`desired`, `dhwStoreT`) are plain numbers.

There is **no** water pressure, CH flow temperature or CH setpoint in this
payload on this gateway family. Earlier drafts of the integration exposed
sensors for those based on speculative field names; they were removed rather
than left to report `unknown` forever.

## Energy and consumption

Two routes carry metered consumption. Neither is listed in the app's route
table in a way that suggests a query language, but both were confirmed against
the test gateway.

### `POST remote/plants/{gw}/getConsumptionsSettings` (verified)

Takes an empty JSON body and returns the account's display preferences:

```json
{"currency": 20, "gasType": 0, "gasEnergyUnit": 1, "elecCost": 0.2, "gasCost": 5.0}
```

* `gasEnergyUnit` is the **display** unit: `{kWh: 0, GigaJoule: 1, Therm: 2,
  MegaBtu: 3, Smc: 4, m3: 5, Liter: 6, Gallons: 7}`.
* It does **not** change what the sequences route returns. Do not convert by
  it — see below.
* The sibling route `POST remote/plants/{gw}/consumptionsSettings` answers HTTP
  500 for writing, so settings can be read but not written through these two
  names.

### `GET remote/reports/{gw}/consSequencesApi8?usages=Ch,Dhw` (verified)

`usages` accepts the **family** names only. The per-appliance members
(`ChBoilerGas`, `DhwBoilerGas`, `ChHpGas`, all `*Elec` members, `RoomTemp`,
`StandbyElec`, …) answer HTTP 500 on this gateway, as does a bogus name. The
families expand to every member they contain.

The reply is an array of blocks:

```json
[
  {"k": 7,  "p": 4, "v": [0.0, 0.0, 0.0, 0.0, 1293.43, 289.38, "..."]},
  {"k": 10, "p": 4, "v": [0.0, 0.0, 0.0, 0.0,  878.55, 1486.81, "..."]}
]
```

* `k` selects the usage:

  | Family | `k` | Meaning |
  | --- | --- | --- |
  | `Ch` | 1 | Space heating total |
  | `Ch` | 7 | **Space heating gas** |
  | `Ch` | 20 | Space heating electricity |
  | `Dhw` | 2 | DHW total |
  | `Dhw` | 10 | **DHW gas** |
  | `Dhw` | 21 | DHW electricity |

  `k=1` ≈ `k=7` + `k=20` and `k=2` ≈ `k=10` + `k=21`, i.e. the "total" member
  is the sum of the energy carriers.

* `p` selects the bucket size:

  | `p` | Series | Length | Order |
  | --- | --- | --- | --- |
  | 1 | Last 24 hours | 24 | hourly, oldest first |
  | 2 | Last 7 days | 7 | daily, oldest first |
  | 3 | Daily | 34 | daily, oldest first, **ends yesterday** |
  | 4 | Monthly | 22 | calendar month, oldest first |

  `p=3`'s trailing 7 entries equal `p=2` byte for byte, and its last 31
  entries sum to `p=4`'s last entry. That is the proof for both the period
  meaning and the month alignment.

* `v` is the series itself. The first entries of `p=4` are `0.0` because they
  predate the meter being active; they are not a bug and must not be filtered
  out as "missing".

The values are **kWh**. Three independent lines of evidence:

1. The route is bound in the shipped binary as
   `consKwhSequencesAuthToken:gatewayId:usages:usageApi8:completionHandler:`
   — the name says kWh, independently of the account's display unit.
2. `p=3`'s last 31 daily entries sum to `1786.589` and `p=4`'s last entry is
   `1786.602`: the daily series rolls up into the monthly bucket, so both are
   the same quantity in the same unit.
3. `p=4`'s last entry for `k=10` is `1786.602` and `energyAccount` reports
   `{"use": 2, "gas": 1787}`; `k=7`'s is `814.563` against `{"use": 1,
   "gas": 821}`. The two routes agree to rounding. `use: 1` is space heating
   and `use: 2` is DHW.

The magnitudes rule out the other candidates. Under a GigaJoule reading, the
`p=1` hourly DHW series would average 0.17 kWh per hour, and the last month's
`1786.6 GJ` would be 496 kWh — an implausibly small annual gas bill for a
boiler that also heats a cylinder. Under m³ the daily DHW figure would be
4.2 m³ (≈39 GJ/year) for hot water alone. kWh fits both the daily and monthly
figures.

### `GET remote/reports/{gw}/energyAccount` (verified)

```json
{"LastMonth": [{"use": 1, "gas": 821, "elect": 0}, {"use": 2, "gas": 1787, "elect": 0}]}
```

The route ignores `period`, `from` and `to` entirely — every variant returns
the same `LastMonth` object. It is useful as a cross-check because it is a
separate code path, but `consSequencesApi8` is the one that carries history.
`elect` is `0` on a boiler gateway.

`GET remote/reports/{gw}/co2Report` answers HTTP 599 (upstream gateway error)
on this plant, so no CO₂ reading is exposed.

## Writes

All writes are `POST` with a `{"new": ..., "old": ...}` envelope. `old` is the
value last observed and is used for the controller's optimistic-concurrency
check, so callers always send the value they read. A successful write returns
`{"success": true}`.

| Route | Body | Verified |
| --- | --- | --- |
| `remote/plantData/{gw}/mode` | `{"new": 1, "old": 1}` | yes |
| `remote/plantData/{gw}/dhwMode` | `{"new": 2, "old": 2}` | yes |
| `remote/plantData/{gw}/dhwTemp` | `{"new": 53.0, "old": 53.0}` | yes |
| `remote/plantData/{gw}/holiday` | `{"new": null, "old": null}` | yes |
| `remote/zones/{gw}/{z}/mode?isCooling=false` | `{"new": 3, "old": 3}` | yes |
| `remote/zones/{gw}/{z}/temperatures?isCooling=false` | `{"new": {"comf": 19.0, "econ": 16.0}, "old": {...}}` | yes |

### The DHW setpoint is a scalar

`dhwTemp` takes a **single number**. Sending the comfort/reduced pair that zone
setpoints use returns HTTP 500:

```
POST remote/plantData/{gw}/dhwTemp
{"new": {"comf": 53.0, "econ": 30.0}}   ->  500 {"Message":"An error has occurred."}
{"new": 53.0, "old": 53.0}             ->  200 {"success": true}
```

### `isCooling`

Zone `mode` and `temperatures` accept an optional `isCooling` query parameter
(`true`/`false`); both forms were verified to return 200, so the integration
always sends `false` explicitly for heating zones.

### Enumerations

Recovered from the binary and matching the live `allowedOptions`:

```
PlantMode:  Summer=0, Winter=1, HeatingOnly=2, Cooling=3, CoolingOnly=4, Off=5, Holiday=6
ZoneMode:   Off=0, ManualNight=1, Manual=2, TimeProgram=3
DhwMode:    Off=0, Eco=1, Comfort=2
```

Live `allowedOptions` on a `gwSysType` 3 boiler gateway: plant `[0, 1, 2, 5]`,
DHW `[0, 1, 2]`, zone `[2, 3]`.

Note that zone mode does **not** include `0` (Off) on this gateway, so a zone
cannot be switched off independently — that is a plant-level action. The
climate entity advertises `HVACMode.OFF` only when the zone actually allows
it.

## Behaviour of the reference gateway

Tested against a `gwSysType 3` gateway on firmware `104.115.28B`:

* one visible zone (`num 1`) plus two hidden virtual zones
* no room sensor (`roomSens: false`)
* cylinder present (`dhwBoiler: true`, `dhwStoreT 54 °C`, setpoint 53 °C)
* no cooling, no pilot, no fireplace, no metering
