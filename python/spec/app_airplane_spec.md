# Airplane Application — Specification

> This document fully specifies `app_airplane.py` so that an AI agent or
> developer can re-create it from scratch.  It assumes access to the IDL
> types (`air_traffic_types.idl`), the generated Python types
> (`air_traffic_types.py`), the QoS profiles (`air_traffic_qos.xml`), and
> the shared utilities (`common.py`).

---

## 1  Purpose

A single aircraft simulator that participates in the ATC DDS system.
Each instance represents one aircraft: it publishes its position at ~5 Hz,
subscribes to controller instructions filtered by tail number, files a
flight plan via Request/Reply at startup, requests a gate on arrival, and
publishes pilot acknowledgments for every instruction received.

Aircraft follow a waypoint route from origin to destination, transition
through realistic flight phases, burn fuel proportional to distance, and
respond to weather-deviation heading instructions from en-route centers.

---

## 2  Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| Middleware | RTI Connext DDS 7.7.0+ (`rti.connextdds`, `rti.rpc`) |
| Type generation | `rtiddsgen -language python air_traffic_types.idl` → `air_traffic_types.py` |
| QoS configuration | `air_traffic_qos.xml` — per-topic profiles |
| Math | `math` (trig, distance), `random` (jitter seeded by tail number and per-flight callsign, tail generation) |
| Logging | Python `logging` via `common.setup_logging()` |

No web framework, no Flask, no external libraries beyond RTI Connext Python.

---

## 3  Command-Line Interface

```
python app_airplane.py --config CONFIG --qos-file QOS_XML [options]
```

| Argument | Type | Default | Description |
|---|---|---|---|
| `--config` | str (required) | — | Path to `air_traffic_scenario.json` |
| `--qos-file` | str (required) | — | Path to `air_traffic_qos.xml` |
| `--tail-number` | str | auto-generated (ad-hoc only) | The aircraft to fly; its schedule is looked up in the scenario (e.g., `N738WN`). For an ad-hoc flight, the tail to use |
| `--callsign` | str | — | Ad-hoc one-leg flight: its callsign (with `--from` and `--to`) |
| `--from` | str | — | Ad-hoc one-leg flight: departure airport |
| `--to` | str | — | Ad-hoc one-leg flight: arrival airport |
| `--duration` | float | until stopped | Maximum run duration in wall-clock seconds |
| `--wall-start-time` | ISO 8601 UTC | own start time | Real time at which the scenario's `sim_start_time` happened; launchers pass the same value to every app (see `docs/SCENARIO.md`) |

At startup `main()`:

1. Parses arguments.
2. Sets `common.QOS_FILE` from `--qos-file`.
3. Loads airport coordinates from scenario config.
4. Looks up the aircraft config by tail number (`--tail-number`).
5. For an ad-hoc flight (`--callsign`, `--from`, `--to`, all required), uses
   `--tail-number` if given, otherwise an auto-generated N-number.
6. Re-initializes logger with tail number as tag.
7. Builds and validates the schedule: every leg needs a `callsign`, and
   consecutive legs must connect; `after_schedule` is `park` or `continue`.
8. Creates `AirplaneSimulator(...)` and calls `run(duration_s=args.duration)`.

---

## 4  `AirplaneSimulator` Class — State & Attributes

### 4.1  Identity

| Attribute | Type | Description |
|---|---|---|
| `tail_number` | `str` | Aircraft registration (from `--tail-number` or config) |
| `callsign` | `str` | Callsign of the current leg's flight (e.g., `SWA401`); changes with each leg |
| `origin` | `str` | Origin airport ICAO code |
| `destination` | `str` | Destination airport ICAO code |
| `config_path` | `str` | Path to scenario config JSON |
| `cruise_alt` | `float` | Cruise altitude in feet (default `35000.0`) |

### 4.2  Motion & Flight State

| Attribute | Type | Initial Value | Description |
|---|---|---|---|
| `lat` | `float` | Origin lat + jitter ±0.02° | Current latitude |
| `lon` | `float` | Origin lon + jitter ±0.02° | Current longitude |
| `alt` | `float` | `0.0` | Current altitude in feet |
| `heading` | `float` | Bearing to destination | Current heading in degrees |
| `ground_speed` | `float` | `0.0` | Ground speed in knots |
| `vertical_speed` | `float` | `0.0` | Vertical speed in feet/min |
| `phase` | `FlightPhase` | `PREFLIGHT` | Current flight phase enum |
| `fuel` | `float` | Route-proportional (see §5.2) | Fuel level 0–100% |
| `assigned_runway` | `str \| None` | `None` | Runway assigned by tower |
| `assigned_gate` | `str \| None` | `None` | Gate assigned by airport |

### 4.3  Navigation State

| Attribute | Type | Description |
|---|---|---|
| `waypoints` | `list[tuple]` | Route waypoints: `(name, lat, lon, alt)` |
| `current_wp_index` | `int` | Index of next waypoint to fly toward |
| `_total_route_nm` | `float` | Total route distance in nautical miles |
| `_wx_deviating` | `bool` | `True` when holding a weather-deviation heading |

### 4.4  DDS Entities

| Attribute | Type | Description |
|---|---|---|
| `participant` | `dds.DomainParticipant` | Single participant for this aircraft |
| `publisher` | `dds.Publisher` | Default publisher |
| `subscriber` | `dds.Subscriber` | Default subscriber |
| `pos_writer` | `dds.DataWriter` | Writes `AircraftPosition` |
| `ack_writer` | `dds.DataWriter` | Writes `PilotAcknowledgment` |
| `instr_reader` | `dds.DataReader` | Reads `ControllerInstruction` (CFT) |
| `wx_reader` | `dds.DataReader` | Reads `WeatherReport` (CFT) |
| `gate_requester` | `rti.rpc.Requester` | Pre-created gate assignment requester (reused) |

---

## 5  Initialisation (`__init__`)

### 5.1  Waypoint Generation (`_build_waypoints`)

1. Compute total route distance in nm.
2. Determine intermediate waypoint count: `max(1, min(6, int(dist_nm / 400)))`.
3. Build waypoint list:
   - `("DEPART", origin_lat, origin_lon, 0.0)`
   - `N` intermediate waypoints linearly interpolated along the route with ±0.3° lateral jitter.
   - `("ARRIVE", dest_lat, dest_lon, 0.0)`

### 5.2  Fuel Loading

Fuel is loaded proportional to route distance:

```
estimated_burn = total_route_nm × 0.04
fuel = clamp(estimated_burn + 20.0, min=40.0, max=100.0)
```

This ensures short flights depart light (~50%) and long flights load up to 100%, with everyone landing with ~15-20% reserve.

### 5.3  DDS Setup

**DP Partitions:**
- `OPS/FPS/*` — discovers Flight Plan Service
- `OPS/TERMINAL/*` — matches all TRACONs
- `OPS/ENROUTE/*` — matches all centers
- `OPS/AIRPORT/<origin>` — matches origin tower/airport
- `OPS/AIRPORT/<destination>` — matches destination tower/airport

**Writers:**

| Writer | Topic | QoS Profile |
|---|---|---|
| `pos_writer` | `AircraftPosition` | `AircraftPositionProfile` |
| `ack_writer` | `PilotAcknowledgment` | `PilotAcknowledgmentProfile` |

**Readers (CFT):**

| Reader | Topic | CFT Name | Filter | QoS Profile |
|---|---|---|---|---|
| `instr_reader` | `ControllerInstruction` | `MyInstructions_<tail>` | `tail_number = '<tail>'` | `ControllerInstructionProfile` |
| `wx_reader` | `WeatherReport` | `DestWeather_<dest>` | `airport_code = '<dest>'` | `WeatherReportProfile` |

**RPC Objects:**

| Object | Type | Service Name | QoS Profile | Lifecycle |
|---|---|---|---|---|
| `gate_requester` | `Requester` | `GateAssignmentService` | `GateAssignmentRequestReplyProfile` | Created once at `__init__`, reused at parking |

The gate requester is created at startup (not on-demand) per Connext best practice — this ensures
the requester's internal reader/writer are discovered by the airport's replier well before
the first request is sent.

---

## 6  Flight Plan Filing (`file_flight_plan`)

Called once at startup, before the main simulation loop.

1. Create a **new** `Requester` for `FlightPlanFilingService` (one-time use).
2. Call `wait_for_service(5s)`. If not found → log warning, proceed without filing.
3. Build a `FlightPlan` sample with all waypoints, status `FILED`.
4. `send_request()` → `receive_replies(10s)`.
5. Log acceptance/rejection from each valid reply.
6. On any exception → log warning and continue.

---

## 7  Gate Requests (`request_pushback`, `request_gate`)

The aircraft talks to the airport's ramp control (`GateAssignmentService`)
at both ends of every leg, using the pre-created `self.gate_requester`:

- `request_pushback()` before taxiing out: send a `GateRequest` with
  `kind = PUSHBACK` and `aerodrome_id = origin`, then clear
  `assigned_gate`. The airport releases the gate. The request is sent
  **without waiting** for discovery or for the reply, so the departure
  time depends only on the simulated clock and runs stay repeatable. On a
  first departure no gate is assigned yet, so nothing is lost if the
  airport is not yet discovered.
- `request_gate()` after reaching `PARKED`: first take any pending replies
  (the airport's `RELEASED` confirmations of earlier pushbacks, logged).
  Then `wait_for_service(5s)` (warning and return if not found), send a
  `GateRequest` with `kind = ASSIGN` and `aerodrome_id = destination`,
  `receive_replies(10s)`, and store `assigned_gate` from the reply.
- On any exception → log warning and continue.

---

## 8  Flight Phase State Machine (`advance_simulation`)

Each tick represents `0.2 × sim_speed` seconds of sim-time (5 Hz wall-clock).

### 8.1  Auto-Descent Trigger

During `CRUISE`, auto-triggers descent when distance to destination ≤ `(alt / 1000) × 3` nm:
- Set phase → `DESCENT`, vertical speed → −1500 fpm, ground speed → 350 kt.

### 8.2  Phase Transitions

| From | To | Trigger | Speed Changes |
|---|---|---|---|
| `PREFLIGHT` | `TAXI_OUT` | Immediate (first tick) | ground: 15 kt |
| `TAXI_OUT` | `TAKEOFF` | Next tick | ground: 150 kt, VS: +2500 fpm |
| `TAKEOFF` | `CLIMB` | alt ≥ 1,500 ft | ground: 350 kt |
| `CLIMB` | `CRUISE` | alt ≥ cruise_alt | ground: 450 kt, VS: 0 |
| `CRUISE` | `DESCENT` | auto-descent trigger (§8.1) | ground: 350 kt, VS: −1500 fpm |
| `DESCENT` | `APPROACH` | alt ≤ 3,000 ft | ground: 180 kt |
| `APPROACH` | `LANDING` | alt ≤ 200 ft | — |
| `LANDING` | `TAXI_IN` | Immediate | ground: 60 kt, VS: 0, alt: 0 |
| `TAXI_IN` | `PARKED` | Immediate | ground: 0 kt, VS: 0, snap to dest coords |

### 8.3  Position Advancement

When `ground_speed > 0` and not `PARKED`:
```
nm_per_tick = ground_speed / 3600 × TICK
lat += (nm_per_tick × cos(heading°)) / 60
lon += (nm_per_tick × sin(heading°)) / (60 × cos(lat°))
```

### 8.4  Waypoint Steering (`_steer_to_waypoint`)

Called every tick for airborne phases (not ground phases), skipped during weather deviation:
1. Compute distance to current waypoint.
2. If < 5 nm and not the last waypoint → advance to next, log passing.
3. Update heading to bearing toward current waypoint.

### 8.5  Fuel Burn

Skipped during `PREFLIGHT` and `PARKED`:
```
fuel = max(5.0, fuel − 0.001 × sim_speed)
```

Floor at 5% — aircraft never runs completely dry.

---

## 9  Instruction Processing (`process_instructions`)

Takes all pending samples from the CFT reader. For each:

1. **HEADING** (with `assigned_heading_degrees`): Set heading, enable `_wx_deviating = True`.
   Aircraft holds this heading indefinitely until a CLEARANCE is received.
2. **CLEARANCE** (with `clearance_text`): Delegates to `_handle_clearance()`.
3. **ALTITUDE** (with `assigned_altitude_feet`): Set vertical speed (+2000 or −1500 fpm),
   set phase to `DESCENT` if descending.
4. Always send a `PilotAcknowledgment` with status `WILCO`.

### 9.1  Clearance Handling (`_handle_clearance`)

Parses clearance text for `RESUME OWN NAV DIRECT <waypoint_name>`:
1. Extract waypoint name after `"DIRECT "`.
2. Search waypoint list for matching name.
3. If found → update `current_wp_index`, set `_wx_deviating = False`.
4. If not found → just set `_wx_deviating = False` (resume normal navigation).

---

## 10  Weather Checking (`check_weather`)

Takes all samples from the destination weather CFT reader. Logs weather conditions. Informational only — no behavioral change from weather reports.

---

## 11  Main Run Loop (`run`)

The aircraft flies its `schedule` (from the scenario entry, or a one-leg
`--from`/`--to`). Simulated time comes from `common.SimClock`. Consecutive
legs must connect (each departs where the previous arrived); this is checked
at startup. For each leg:

1. For later legs: stay parked at the gate (publishing position at 5 Hz)
   until the turnaround ends (`sim_turnaround_min` after the previous
   arrival), then `_start_leg()`: reset the flight state, update the
   participant partitions to the new airports and terminal areas, and set
   the destination-weather filter parameter to the new destination.
2. If the leg has a `sim_departure_time` still in the future, wait at the
   gate until the simulated clock reaches it.
3. `request_pushback()`, then `file_flight_plan()`.
4. Loop at 5 Hz (0.2s sleep) until the phase is `PARKED`, `shutdown_flag`,
   or `duration_s` elapsed: `advance_simulation()`, `publish_position()`,
   `process_instructions()`, `check_weather()`.
5. When `PARKED`, `request_gate()`.
6. Next leg: the next scheduled leg; or, after the last one, with
   `after_schedule: continue`, a leg from the current airport to a random
   other airport (`common.scenario_rng(config, "aircraft", tail)`, so it
   repeats with a scenario `seed`); with `park`, stay parked, publishing
   position at 5 Hz, until `shutdown_flag` or `duration_s` elapsed.

The start position comes from a random generator seeded by the tail number,
and each flight's waypoint offsets from one seeded by its callsign, so a
given flight's route is the same on every run. `continue` legs use the next
flight number (`SWA401` → `SWA402`).

---

## 12  Position Publishing (`publish_position`)

Constructs an `AircraftPosition` sample with all current state:
- Position: `GeoPosition(lat, lon, alt)`
- Speeds, heading, phase, origin, destination
- `fuel_level_percent`, `nav_status` (WEATHER_DEVIATION or NORMAL)
- `assigned_runway`, `assigned_gate`
- `timestamp` from `now_ms()`

Writes via `pos_writer.write(sample)`.

---

## 13  Tail Number Generation (`_random_tail_number`)

Generates a US N-number in two formats:
- **Digits only:** `N` + 1-5 digits (e.g., `N12345`)
- **Mixed:** `N` + 1-3 digits + 2 letters (e.g., `N738WN`)

Letters exclude I, O, Q (per FAA convention).

---

## 14  Signal Handling

Global `shutdown_flag` set by `SIGINT` and `SIGTERM` handlers. The main loop checks this flag each tick.

---

## 15  Thread Summary

| Thread | Description |
|---|---|
| Main | `run()` — simulation loop at 5 Hz |

Single-threaded application. No background threads.

---

## 16  File Structure

Single file: `app_airplane.py`

### Imports from `air_traffic_types.py` (generated)

`AircraftPosition`, `AcknowledgmentStatus`, `ControllerInstruction`, `FlightPlan`,
`FlightPlanRequest`, `FlightPlanResponse`, `FlightPlanStatus`, `FlightPhase`,
`GateAssignmentReply`, `GateRequest`, `GeoPosition`, `InstructionType`,
`NavStatus`, `PilotAcknowledgment`, `Waypoint`, `WeatherReport`

### Imports from `common.py`

`bearing_deg`, `create_participant`, `create_publisher`, `create_subscriber`,
`distance_nm`, `load_aircraft_config`, `load_airport_coords`, `load_qos_provider`,
`make_id`, `now_ms`, `SimClock`, `reader_qos`, `setup_logging`,
`writer_qos`
