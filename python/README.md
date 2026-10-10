# Python Implementation

Python implementation of the Air Traffic Control simulation using RTI Connext DDS 7.7.0.

## Prerequisites

- Python 3.10+
- An **RTI Connext DDS license file**; see the
  [top-level README](../README.md#connext-dds) for how to get a free one. A
  Connext installation isn't needed to run the apps, because the
  `rti.connext` package is installed from PyPI. Regenerating types does need
  a full RTI Connext DDS installation (for `rtiddsgen`); follow the
  instructions at [evaluation.rti.com](https://evaluation.rti.com).
- The virtual environment, set up from the repository root with
  `source setup.sourceme`. It installs [`requirements.txt`](requirements.txt).
- `RTI_LICENSE_FILE` and `CARTO_BASEMAP_API_KEY` set in the repository's
  ignored `.env.local` file. Create it by copying `.env.example` at the
  repository root. Use your own
  [CARTO basemap key](https://carto.com/basemaps/apikey/), which is free within
  CARTO's fair-use limit. Do not commit the key; it is visible to dashboard
  users in browser tile requests and should be restricted to the domains where
  it is used.

## Applications

| Application | Role | Count |
|---|---|---|
| `app_airplane.py` | Aircraft position reporting, flight plan filing, gate requests | 1 per aircraft |
| `app_airport.py` | Weather reports, runway status, gate assignment service | 1 per airport |
| `app_tower.py` | Terminal-area control: clearances, runway management, handoffs | 1 per airport |
| `app_tracon.py` | Terminal radar approach control: arrival sequencing, handoffs | 1 per TRACON |
| `app_center.py` | En-route control: separation, weather rerouting, sector handoffs | 1 per center |
| `app_flightplan_service.py` | Central flight plan validation and publishing | 1 |
| `app_weather_service.py` | Convective weather cell generation | 1 |
| `app_dashboard.py` | Web-based real-time map (Flask + Leaflet) | 1 |

Supporting files:

| File | Description |
|---|---|
| `common.py` | Shared utilities: DDS helpers, scenario loaders, geometry, the shared simulated clock (`SimClock`) |
| `air_traffic_types.py` | Generated types from IDL (DO NOT HAND-EDIT) |
| `requirements.txt` | Python dependencies |
| `spec/` | Per-application behavior specifications |

## Run the Demo

From the repository root:

```bash
./scripts/demo_start.sh
```

Open http://localhost:8050 for the real-time dashboard. To stop:

```bash
./scripts/demo_stop.sh
```

## Running Individual Apps

All apps require `--config` and `--qos-file` arguments, and accept
`--wall-start-time` so they share the running demo's simulated clock (the
value `demo_start.sh all` prints; see [`docs/SCENARIO.md`](../docs/SCENARIO.md)).
From this directory:

```bash
python app_airport.py \
    --config ../air_traffic_scenario.json \
    --qos-file ../air_traffic_qos.xml \
    --airport-code KJFK
```

`../scripts/demo_start.sh <app>` launches a single app with all arguments
pre-configured. Run it with `help` to see the options.

## Generate Types

The generated `air_traffic_types.py` is checked in, so this step is only needed
if you modify `air_traffic_types.idl`:

```bash
./types_generate.sh
```

This runs `rtiddsgen -language Python` on `../air_traffic_types.idl` and
generates `air_traffic_types.py`.
