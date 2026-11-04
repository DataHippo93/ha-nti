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

Entities are grouped under one device per gateway, showing the model, firmware
version and serial number.

Only what the appliance actually reports is created. A gateway with no cylinder
gets no water heater, a zone without a room probe shows no room temperature, and
hidden virtual zones are skipped.

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

## Known limitations

* This is a cloud integration. If the vendor's service is down, the entities go
  unavailable.
* Writes use the vendor's optimistic-concurrency envelope (`new`/`old`). If the
  appliance changes state between a read and a write, the write is rejected and
  the next poll re-syncs.
* Some routes exist only for other appliance families (BSB controllers, heat
  pumps, Velis water heaters). They are documented but untested here.
* Time programs, holiday scheduling and reports are not exposed yet.

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
