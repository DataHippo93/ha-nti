# NTI net for Home Assistant

A Home Assistant integration for NTI heating appliances controlled through the
**NTI net** cloud service (`nti.remotethermo.com`), the same backend the NTI net
mobile app uses. It also fits the wider Ariston/remotethermo family, whose
appliance firmware and cloud are shared.

The protocol was reverse engineered from the shipped iOS app and then verified
against a live account. See [PROTOCOL.md](PROTOCOL.md) for the full evidence
trail, including the field shapes and the status-code traps.

## What you get

For each gateway (appliance) on the account:

| Entity | Type | Notes |
| --- | --- | --- |
| Zone climate | `climate` | Current temperature, setpoint, heat/cool/auto, heat demand |
| Zone comfort setpoint | `number` | Written together with the reduced setpoint |
| Zone reduced setpoint | `number` | |
| Zone heat request | `binary_sensor` | Zone is calling for heat |
| Hot water | `water_heater` | Cylinder temperature, setpoint, DHW mode |
| Operating mode | `select` | Off / Summer / Winter / Heating only, limited to the modes the appliance advertises |
| Outside temperature | `sensor` | |
| Hot water temperature | `sensor` | |
| Burner flame | `binary_sensor` | |
| Heating pump | `binary_sensor` | |
| Online | `binary_sensor` | Gateway connectivity |
| Zone override | `binary_sensor` | Override in force, with its temperature and expiry |
| Hot water economy temperature | `sensor` | The cylinder's reduced setpoint |
| Gas consumption this month | `sensor` | Month to date, kWh; the one to add to the energy dashboard |
| Gas consumption yesterday | `sensor` | The last complete day, kWh |
| Gas consumption last month | `sensor` | Last completed calendar month, kWh |
| Gas heating last month | `sensor` | Space heating share of last month, kWh |
| Gas hot water last month | `sensor` | DHW share of last month, kWh |

Entities are grouped under one device per gateway, showing the model, firmware
version and serial number.

Only what the appliance actually reports is created. A gateway with no cylinder
gets no water heater, a zone without a room probe shows no room temperature,
and hidden virtual zones are skipped. The gas sensors appear only when the
account advertises metering (`hasMetering`).

### Energy dashboard

Only **Gas consumption this month** is a running total, so it is the one to
attach to the dashboard. The other four are per-period figures that legitimately
fall from one period to the next, so they carry no state class and are meant for
templates, cards and automations rather than long-term statistics.

The cloud stores **energy**, so the sensors report kWh by default. Several
utilities bill gas by volume instead, so **Settings → Devices & services → NTI
net → Configure** offers an **m³** display mode:

* **kWh** (default) — `energy` device class, no conversion, honest to the wire
  format.
* **m³** — `gas` device class, derived as `kWh ÷ energy content`. Set the
  energy content to the conversion factor printed on your own gas bill; the
  `10.42 kWh/m³` default is a typical North American value and is only
  approximate. Real factors move a few percent month to month with the gas's
  composition.

Because m³ is derived, a small error in the factor shifts every reading. The
factor is never applied to the stored kWh values, only at display time, so
changing it re-derives the volumes without refetching.

#### Cost

Add the price under **Settings → Dashboards → Energy → Gas consumption → Add
cost**, and match it to the unit you chose above:

| Unit | Rate for `$3.99/GJ` | How it was derived |
| --- | --- | --- |
| kWh | `0.01436` per kWh | `3.99 ÷ 277.778` |
| m³ | `0.1497` per m³ | `3.99 ÷ 277.778 × 10.42` |

Do not read the price from the integration: the cloud's `gasCost` and `elecCost`
are display preferences, not your billed rate.

Both figures are the **commodity** charge only. Delivery, distribution, the
carbon levy and admin fees are separate, so the dashboard will understate a real
bill — often by two to three times in Alberta. For a figure that matches your
invoice, divide your average total bill by that month's consumption instead.

Consumption is refreshed hourly rather than on every poll, because the report
route is heavier and the figures only change once a day. The running total
returns to zero on the first of the month, which Home Assistant's
`total_increasing` handling accounts for.

## Icon

The icon in `custom_components/nti_net/brand/` is original artwork (a flame on a
dark badge) rendered from `icon.svg` — not the vendor's logo. Home Assistant
serves it from `/api/brands/integration/nti_net/icon.png`; no brands CDN
submission is involved.

To regenerate the PNGs after editing the SVG:

```bash
rsvg-convert -w 256 -h 256 icon.svg -o icon.png
rsvg-convert -w 512 -h 512 icon.svg -o icon@2x.png
```

## Installation

### HACS

1. Add this repository as a custom repository of type *Integration*.
2. Install **NTI net**.
3. Restart Home Assistant.

### Manual

Copy `custom_components/nti_net` into your Home Assistant `config` directory and
restart.

## Configuration

1. **Settings → Devices & services → Add integration → NTI net**
2. Enter the e-mail and password you use in the NTI net app.

The integration validates the credentials by logging in and listing the
appliances on the account before creating the entry.

There is no cloud dependency beyond `aiohttp`, which Home Assistant already
ships, so nothing extra is installed.

## Polling and rate limits

The cloud throttles per account and answers `429 Requests are blocked for N
seconds` when pushed. Two clients polling the same account at once can lock
each other out.

* The default poll interval is **300 seconds**.
* It can be changed under **Settings → Devices & services → NTI net →
  Configure**. The minimum accepted value is **180 seconds**; the options form
  will not let you go below it.
* Do not run the NTI net mobile app and this integration against the same
  account simultaneously for long periods.
* On `429` the integration raises the retry delay from the response body, and
  if it is throttled during startup it retries later instead of failing.

Reads always pin the metric unit system (`umsys=si`), so every temperature in
Home Assistant is Celsius regardless of your account's locale. Unit conversion
to Fahrenheit is left to Home Assistant's own unit system support.

## Services

Two actions cover what the cloud cannot express as an entity.

**`nti_net.set_zone_override`** holds one zone at a fixed temperature until a
given time, ignoring its schedule — the app's "manual override" (the vendor
calls it *deroga*).

```yaml
action: nti_net.set_zone_override
target:
  entity_id: climate.boiler_zone_1
data:
  zone: 1
  temperature: 21.5
  until: "2026-11-05T22:00:00"
```

**`nti_net.clear_zone_override`** cancels it again:

```yaml
action: nti_net.clear_zone_override
target:
  entity_id: climate.boiler_zone_1
data:
  zone: 1
```

While an override is active, a `<zone> override` binary sensor turns on and
reports the held temperature and expiry as attributes.

**`nti_net.set_holiday`** turns holiday mode on until a timestamp, or clears it
when `until` is omitted:

```yaml
action: nti_net.set_holiday
data:
  until: "2026-11-12T18:00:00"
```

Holiday state **cannot be read back** — no read route exposes it — so this is a
write-only action with no matching entity.

## Known limitations

* This is a cloud integration. If the vendor's service is down, the entities go
  unavailable.
* Writes use the vendor's optimistic-concurrency envelope (`new`/`old`). If the
  appliance changes state between a read and a write, the write is rejected and
  the next poll re-syncs.
* Some routes exist only for other appliance families (BSB controllers, heat
  pumps, Velis water heaters). They are documented but untested here.
* Time programs are not exposed.
* Only **gas** consumption is exposed. The same report also carries electricity
  members, but a boiler gateway reports zero for them, so a sensor would sit at
  `0` forever.
* Holiday mode is write-only: the cloud applies it but never reports it back.

## Removing

Remove the integration from **Settings → Devices & services**. Nothing is
stored outside the config entry.

## Development

The protocol notes in [PROTOCOL.md](PROTOCOL.md) cover the wire format.

The test suite talks to the real cloud, so it needs credentials:

```bash
python -m venv .venv
.venv/bin/pip install homeassistant pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```

Credentials are read from `NTI_USERNAME`/`NTI_PASSWORD`, or from a `.env` file
at the repository root. Without them the live tests **skip** and only the form
tests run.

## Disclaimer

Not affiliated with NTI or Ariston. The cloud API is undocumented and may
change without notice.

## Licence

MIT — see [LICENSE](LICENSE).


## Fork additions (DataHippo93/ha-nti)

This fork adds read-only fault and service entities per gateway:

- **Fault code** sensor: the active fault code, `fault` if the cloud gives no
  code, or `OK`. Attributes carry the description, whether it is a lockout,
  and the cloud entry. It reads `unknown`, never `OK`, when the fault list
  cannot be fetched or does not look like a fault list.
- **Fault** binary sensor (problem class), for alerts.
- **Flow temperature**, **Flow setpoint**, **Return temperature**,
  **Heating circuit pressure** and **Gateway signal level** sensors.
- **Automatic thermoregulation** binary sensor.

The cloud keeps no fault history, so the Fault code sensor's history in Home
Assistant is the log. These cost three extra requests per poll. If the cloud
throttles the account they are skipped until the block expires, and the
heating and hot water entities keep working.

- **Hot water** switch: domestic hot water on (always active) or off. Its
  state comes from the `DhwMode` data item, because on a combi boiler the
  aggregated read reports mode 0 whatever the real mode is. The same item now
  corrects the water heater entity on such boilers. The switch reads `unknown`
  and refuses to write when the current mode has not been read.
