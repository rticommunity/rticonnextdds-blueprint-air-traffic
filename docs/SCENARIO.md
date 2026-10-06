# Scenario File Reference

[`air_traffic_scenario.json`](../air_traffic_scenario.json) describes the
simulated world: airports, airspace, aircraft and their schedules, weather,
and the simulated clock. Every application reads the same file through its
required `--config` argument, so they all agree on the scenario. The
applications only read this file; nothing writes to it at runtime.

A scenario can be fully scripted at the start (scheduled departures and
forecast storms), then continue with realistic random behavior. With a
`seed`, the random parts also repeat exactly from run to run.

## Naming rule: `sim_` and `wall_`

Every time-related name starts with:

- `sim_`: the simulated world's clock (for example `sim_start_time`).
- `wall_`: the real, wall-clock time (for example `--wall-start-time`).

All times in the scenario file are simulated times.

## Time format

Clock times are ISO 8601 in UTC, ending in `Z`, for example
`2026-06-15T14:05Z` or `2026-06-15T14:05:30Z`. A full date makes schedules
that cross midnight unambiguous.

## Top-level fields

| Field | Required | Meaning |
|---|---|---|
| `scenario` | yes | Display name |
| `duration_seconds` | yes | How long the launcher runs the demo, in wall seconds |
| `sim_start_time` | yes | Simulated time at which the simulation begins |
| `initial_sim_speed` | yes | Simulated seconds per real second at start (0.1–50). The dashboard can change it while running |
| `seed` | no | Makes all random behavior repeat exactly. Omit it to get a different run each time |
| `sim_turnaround_min` | no | Default minimum time at the gate between an aircraft's legs, in simulated minutes (default 45) |
| `airports`, `tracons`, `centers` | yes | Airport data, terminal and en-route airspace |
| `weather` | no | Forecast and unforecast convective weather (see below) |
| `aircraft` | yes | Aircraft and their schedules (see below) |

## The shared simulated clock

All applications compute the same simulated time from a reference point plus
a rate:

```
sim_now = sim_ref_time + (wall_now − wall_ref_time) × sim_speed
```

"At real time `wall_ref_time`, simulated time was `sim_ref_time`, and it
advances at `sim_speed`."

- **At start**, the reference is `--wall-start-time` (the real time at
  which `sim_start_time` happened), `sim_start_time`, and
  `initial_sim_speed`.
- **`--wall-start-time`** is a command-line argument of every application.
  `scripts/demo_start.sh` takes the current time once and passes the same
  value to every application it starts, so they all share one clock even
  though they start seconds apart. It prints the value, so an application
  started later can join the same run:
  `./scripts/demo_start.sh airplane --tail-number N338AA --wall-start-time <value>`.
  Without it, an application uses its own start time.
- **When the operator changes the speed**, the dashboard publishes the new
  setting (`wall_ref_time`, `sim_ref_time`, `sim_speed`) in its
  `sim_clock` DDS participant property, which Connext distributes through
  discovery. Applications only read the latest value, so one that joins late
  computes the correct time without needing the history of earlier changes.

Example, with `sim_start_time` 14:00Z and `initial_sim_speed` 10, launched
at real time 09:00:00:

| Real time | What happens | Simulated time |
|---|---|---|
| 09:00:00 | All applications start with `--wall-start-time 09:00:00` | 14:00 |
| 09:01:00 | 60 s × 10 have elapsed | 14:10 |
| 09:01:00 | The operator sets speed 2. The dashboard publishes `wall_ref_time` 09:01:00, `sim_ref_time` 14:10, `sim_speed` 2 | 14:10 |
| 09:02:00 | 14:10 + 60 s × 2 | 14:12 |

The dashboard shows the current simulated time next to its other counters.

## Aircraft

```json
{ "tail_number": "N738WN",
  "schedule": [
    { "callsign": "SWA400", "from": "KDFW", "to": "KATL",
      "sim_departure_time": "2026-06-15T14:20Z" },
    { "callsign": "SWA401", "from": "KATL", "to": "KDFW",
      "sim_departure_time": "2026-06-15T18:30Z" } ],
  "after_schedule": "park" }
```

| Field | Meaning |
|---|---|
| `tail_number` | The aircraft (airframe). It never changes, and it is the key of every aircraft topic |
| `schedule` | The legs (flights) the aircraft flies, in order. Each leg must depart from the airport where the previous one arrived |
| `schedule[].callsign` | Required. The flight's callsign (airline code + flight number, e.g. `SWA401`). Like a real airline schedule, it normally changes with each leg |
| `schedule[].from`, `schedule[].to` | Departure and arrival airport codes |
| `schedule[].sim_departure_time` | Optional. The earliest departure; the aircraft waits at the gate until then. Without it, the first leg departs as soon as the aircraft starts, and later legs as soon as the turnaround ends |
| `sim_turnaround_min` | Optional. Minimum time at the gate between legs, in simulated minutes (default: the scenario's `sim_turnaround_min`, or 45) |
| `after_schedule` | What happens after the last leg. `park` (the default): stay at the arrival gate until the simulation ends. `continue`: keep flying, choosing each next destination at random (repeatable with a `seed`), with the next flight number (`SWA401` → `SWA402`) |

Each aircraft flies its own schedule; no other application tells it when to
depart. A later leg departs at its `sim_departure_time` or at the end of the
turnaround, whichever is later. Like a real airliner, the aircraft deals with
the airport's ramp control (its `GateAssignmentService`) at both ends of a
leg:

- **Before taxiing out**, it requests **pushback**, which releases its gate
  for the next arrival.
- **After landing**, it requests a **gate assignment**. Asking again while
  it already has a gate returns the same gate.

Air traffic controllers hand the aircraft from facility to facility on every
leg, as they would for any flight: each controller takes responsibility
again when it receives the aircraft, even if it handled the same tail number
on an earlier leg.

The dashboard's "add aircraft" form starts an extra aircraft (with a
generated tail number) flying a one-leg schedule that departs immediately.
From the command line: `./scripts/demo_start.sh airplane --callsign UAL900
--from KORD --to KATL`.

## Weather

The weather service publishes two kinds of convective cells, mirroring a
forecast and the storms that nobody forecast:

```json
"weather": {
  "sim_valid_until": "2026-06-15T15:00Z",
  "forecast_cells": [
    { "id": "WX-DEMO-1", "sim_appear_time": "2026-06-15T14:20Z",
      "on_airway": ["KJFK", "KLAX"], "fraction": 0.25,
      "radius_nm": 30, "severity": "SEVERE", "sim_duration_min": 120 } ],
  "unforecast": { "mode": "after_forecast", "sim_spawn_every_min": 5, "max_cells": 5 }
}
```

**Forecast cells** appear at their `sim_appear_time`:

| Field | Default | Meaning |
|---|---|---|
| `sim_appear_time` | (required) | When the cell appears |
| `on_airway` + `fraction` | | Place the cell a fraction of the way along the route between two airports (the same straight route aircraft fly), for example `0.25` |
| `lat`, `lon` | | Or place it at a fixed position |
| `radius_nm` | 20 | Cell radius. En-route centers deviate aircraft within 1.5 × the radius |
| `base_altitude_ft`, `top_altitude_ft` | 10000, 45000 | Vertical extent |
| `severity` | `SEVERE` | `MODERATE`, `SEVERE`, or `EXTREME` |
| `sim_duration_min` | 60 | Lifetime in simulated minutes |
| `heading_deg`, `speed_kt` | 0, 0 | Drift; stationary by default |
| `id` | generated | Cell ID |

**Unforecast cells** appear at random positions over the continental US:

| Field | Default | Meaning |
|---|---|---|
| `mode` | `always` | `always`: throughout the run. `after_forecast`: only after `sim_valid_until`. `off`: never |
| `sim_spawn_every_min` | 0.5 | Simulated minutes between new cells |
| `max_cells` | 5 | Maximum number of cells alive at once |

The dashboard can also add and remove cells by hand. Their lifetimes are
counted in simulated time.

## Randomness and `seed`

Random values come from the application that owns them: unforecast cells
from the weather service, and airport weather reports from each airport.
With a `seed`, each owner gets its own reproducible random stream, so
editing a scripted event never changes the random ones. Small cosmetic
variations are always the same: an aircraft's starting gate position is
derived from its tail number, and each flight's route offsets from its
callsign.

Applications still run as separate processes, so the exact timing of
messages varies slightly between runs. A seeded scenario is the same
scenario every time, not an identical replay.

## Deterministic test example

`scripts/smoke_test.sh` runs a copy of the scenario with
`initial_sim_speed` 50, a `seed`, `unforecast.mode` `off`, and one forecast
cell 25% of the way along the KJFK–KLAX route, so every check it makes
happens on every run.
