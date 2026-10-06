# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0
"""
Common utilities for ATC DDS applications.
"""

import datetime
import json
import logging
import math
import os
import random
import time
import uuid

import rti.connextdds as dds

# Override with the DOMAIN_ID / DOMAIN_TAG environment variables (for example in
# .env.local) to run separate demos side by side on the same network.
DOMAIN_ID = int(os.environ.get("DOMAIN_ID", "0"))
DOMAIN_TAG = os.environ.get("DOMAIN_TAG", "airtraffic-USA")
DOMAIN_TAG_PROPERTY = "dds.domain_participant.domain_tag"
QOS_FILE = None  # Set by app via --qos-file arg before calling load_qos_provider()
QOS_LIB = "AirTrafficControl_QosLib"


def load_scenario(config_path: str) -> dict:
    """Load the scenario JSON from disk."""
    with open(config_path) as f:
        return json.load(f)


_load_scenario = load_scenario


def load_airport_coords(config_path: str) -> dict[str, tuple[float, float]]:
    """Load airport (code → (lat, lon)) mapping from scenario config JSON."""
    data = _load_scenario(config_path)
    return {
        a["code"]: (a["latitude"], a["longitude"])
        for a in data["airports"]
    }


def load_center_boundaries(config_path: str) -> dict[str, list[list[float]]]:
    """Load center boundary polygons as dict[center_id → list of [lat, lon]]."""
    data = _load_scenario(config_path)
    return {c["id"]: c["boundary"] for c in data["centers"]}


def load_tracon_for_airport(config_path: str) -> dict[str, str]:
    """Load mapping from airport code → TRACON id."""
    data = _load_scenario(config_path)
    return {a["code"]: a["serving_tracon"] for a in data["airports"] if "serving_tracon" in a}


# ── Per-entity config lookups ──────────────────────────────────────────


def load_airport_config(airport_code: str, config_path: str) -> dict:
    """Look up a single airport's full config entry by code.

    Returns dict with keys: code, name, latitude, longitude, runways, serving_tracon.
    Raises KeyError if not found.
    """
    data = _load_scenario(config_path)
    for a in data["airports"]:
        if a["code"] == airport_code:
            return a
    raise KeyError(f"Airport '{airport_code}' not found in scenario config")


def load_tracon_config(tracon_id: str, config_path: str) -> dict:
    """Look up a TRACON config entry by ID.

    Returns dict with keys from the config (id, serving_center, ...)
    plus a derived 'airports' list of airport codes served by this TRACON.
    Raises KeyError if not found.
    """
    data = _load_scenario(config_path)
    for t in data.get("tracons", []):
        if t["id"] == tracon_id:
            # Derive served airports by reverse-lookup
            t["airports"] = [
                a["code"] for a in data["airports"]
                if a.get("serving_tracon") == tracon_id
            ]
            return t
    raise KeyError(f"TRACON '{tracon_id}' not found in scenario config")


def load_center_config(center_id: str, config_path: str) -> dict:
    """Look up a center config entry by ID.

    Returns dict with keys: id, boundary, min_altitude_ft, max_altitude_ft.
    Raises KeyError if not found.
    """
    data = _load_scenario(config_path)
    for c in data["centers"]:
        if c["id"] == center_id:
            return c
    raise KeyError(f"Center '{center_id}' not found in scenario config")


def load_aircraft_config(tail_number: str, config_path: str) -> dict | None:
    """Look up an aircraft (airframe) config entry by tail number.

    Returns dict with keys: tail_number, schedule (legs, each with its own
    flight callsign), and optionally after_schedule and sim_turnaround_min.
    Returns None if not found (aircraft may be ad-hoc).
    """
    data = _load_scenario(config_path)
    for ac in data.get("aircraft", []):
        if ac["tail_number"] == tail_number:
            return ac
    return None


def load_scenario_info(config_path: str) -> dict:
    """Return scenario metadata + all entity IDs as a flat dict.

    Keys:
        scenario, duration_seconds,
        airports (list), tracons (list), centers (list), aircraft (list)
    """
    data = _load_scenario(config_path)
    return {
        "scenario": data.get("scenario", "unnamed"),
        "duration_seconds": data.get("duration_seconds", 120),
        "airports": [a["code"] for a in data.get("airports", [])],
        "tracons": [t["id"] for t in data.get("tracons", [])],
        "centers": [c["id"] for c in data.get("centers", [])],
        "aircraft": [ac["tail_number"] for ac in data.get("aircraft", [])],
    }


def point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test. Polygon is list of [lat, lon]."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[j]
        if ((lat_i > lat) != (lat_j > lat)) and \
           (lon < (lon_j - lon_i) * (lat - lat_i) / (lat_j - lat_i) + lon_i):
            inside = not inside
        j = i
    return inside


def polygon_bbox(polygon: list[list[float]]) -> tuple[float, float, float, float]:
    """Return (min_lat, max_lat, min_lon, max_lon) for a polygon of [lat, lon] points."""
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    return min(lats), max(lats), min(lons), max(lons)


def find_center_for_position(
    lat: float, lon: float, center_boundaries: dict[str, list[list[float]]], exclude: str = ""
) -> str | None:
    """Find which center contains the given position. Optionally exclude one center."""
    for cid, boundary in center_boundaries.items():
        if cid == exclude:
            continue
        if point_in_polygon(lat, lon, boundary):
            return cid
    return None


def distance_nm(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in nautical miles (Haversine)."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * 3440.065 * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Initial bearing (degrees) from point 1 to point 2."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(rlat2)
    y = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


# ── Simulated time ──────────────────────────────────────────────────────
#
# Naming rule: every time-related name starts with sim_ (the simulated
# world's clock) or wall_ (the real, wall-clock time).  All times in the
# scenario file are simulated times, written as ISO 8601 UTC
# (e.g. "2026-06-15T14:00Z").  See docs/SCENARIO.md.

SIM_CLOCK_PROP = "sim_clock"
MIN_SIM_SPEED, MAX_SIM_SPEED = 0.1, 50.0


def _clamp_sim_speed(speed: float) -> float:
    return max(MIN_SIM_SPEED, min(MAX_SIM_SPEED, float(speed)))


def parse_utc(text: str) -> datetime.datetime:
    """Parse an ISO 8601 UTC timestamp such as "2026-06-15T14:00Z"."""
    value = datetime.datetime.fromisoformat(text.strip())
    if value.tzinfo is None:
        raise ValueError(f"time '{text}' must be UTC (end it with 'Z')")
    return value.astimezone(datetime.timezone.utc)


def format_utc(value: datetime.datetime) -> str:
    """Format a datetime as ISO 8601 UTC with a trailing 'Z'."""
    return value.astimezone(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def wall_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def add_wall_start_time_arg(parser) -> None:
    """Add the --wall-start-time option shared by every application."""
    parser.add_argument(
        "--wall-start-time", type=parse_utc, default=None,
        help="Real UTC time at which the scenario's sim_start_time happened "
             "(ISO 8601, e.g. 2026-10-05T21:14:03Z). Launchers pass the same "
             "value to every app. Default: this app's own start time.",
    )


def initial_sim_speed(config_path: str) -> float:
    """Read initial_sim_speed from the scenario file."""
    return _clamp_sim_speed(_load_scenario(config_path).get("initial_sim_speed", 1.0))


def scenario_rng(config_path: str, *owner: str) -> random.Random:
    """Random generator for one owner (e.g. "weather" or "airport", "KJFK").

    With a scenario "seed", each owner gets its own reproducible stream, so
    adding or removing scripted events never changes another owner's random
    values.  Without a seed, every run differs.
    """
    seed = _load_scenario(config_path).get("seed")
    if seed is None:
        return random.Random()
    return random.Random(":".join([str(seed), *owner]))


MIN_CONTROL_WALL_PERIOD_S = 1.0  # never faster than 1 Hz wall: bounds CPU


def control_wall_period(sim_period_s: float, sim_speed: float) -> float:
    """Wall-clock period of a control loop that runs every sim_period_s of
    simulated time (for example a radar sweep), at the current speed."""
    return max(MIN_CONTROL_WALL_PERIOD_S, sim_period_s / max(sim_speed, MIN_SIM_SPEED))


class SimClock:
    """The shared simulated clock.

    The clock is a reference point plus a rate: at real time wall_ref_time
    the simulated time was sim_ref_time, and it advances at sim_speed.

        sim_now = sim_ref_time + (wall_now - wall_ref_time) * sim_speed

    Until the dashboard sets the "sim_clock" participant property, every app
    uses the start as its reference (wall_start_time, sim_start_time,
    initial_sim_speed).  Apps only read the latest value; they never need
    the history of speed changes.
    """

    def __init__(self, participant: dds.DomainParticipant, config_path: str,
                 wall_start_time: datetime.datetime | None = None):
        scenario = _load_scenario(config_path)
        self._participant = participant
        self.wall_ref_time = wall_start_time or wall_now()
        self.sim_ref_time = parse_utc(scenario["sim_start_time"])
        self.sim_speed = initial_sim_speed(config_path)

    def update(self) -> None:
        """Adopt the newest sim_clock setting from discovered participants."""
        for sample in self._participant.participant_reader.read():
            if not sample.info.valid:
                continue
            try:
                value = sample.data.property.try_get(SIM_CLOCK_PROP)
                if value is None:
                    continue
                setting = json.loads(value)
                wall_ref = parse_utc(setting["wall_ref_time"])
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if wall_ref > self.wall_ref_time:
                self.wall_ref_time = wall_ref
                self.sim_ref_time = parse_utc(setting["sim_ref_time"])
                self.sim_speed = _clamp_sim_speed(setting["sim_speed"])

    def speed(self) -> float:
        self.update()
        return self.sim_speed

    def now(self) -> datetime.datetime:
        self.update()
        return self.sim_ref_time + (wall_now() - self.wall_ref_time) * self.sim_speed

    def set_speed(self, sim_speed: float) -> None:
        """Change the speed from now on and publish the new setting.

        Only the dashboard calls this.  Simulated time stays continuous.
        """
        sim_now = self.now()
        self.wall_ref_time = wall_now()
        self.sim_ref_time = sim_now
        self.sim_speed = _clamp_sim_speed(sim_speed)
        qos = self._participant.qos
        qos.property.set({SIM_CLOCK_PROP: json.dumps({
            "wall_ref_time": self.wall_ref_time.isoformat(),
            "sim_ref_time": self.sim_ref_time.isoformat(),
            "sim_speed": self.sim_speed,
        })}, propagate=True)
        self._participant.qos = qos


def now_ms() -> int:
    return int(time.time() * 1000)


def make_id(prefix: str = "") -> str:
    short = uuid.uuid4().hex[:12]
    return f"{prefix}{short}" if prefix else short


def setup_logging(name: str) -> logging.Logger:
    """Configure logging with *name* in the format string.

    Can be called again with a more specific name (e.g. ``"center"`` →
    ``"CTR-ZNY"``) — the root handler's formatter is replaced so that all
    subsequent log output uses the new tag.
    """
    fmt = f"%(asctime)s [{name}] %(levelname)s: %(message)s"
    datefmt = "%H:%M:%S"
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt)
    else:
        for h in root.handlers:
            h.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    return logger


def load_qos_provider(qos_file: str | None = None) -> dds.QosProvider:
    path = qos_file or QOS_FILE
    if not path:
        raise ValueError("QOS_FILE not set — pass --qos-file or set common.QOS_FILE")
    abs_path = os.path.abspath(path)
    # If NDDS_QOS_PROFILES already loaded this file into the default
    # provider (set by demo_start.sh), reuse it to avoid duplicate-profile
    # errors.  Otherwise create an explicit provider.
    ndds_profiles = os.environ.get("NDDS_QOS_PROFILES", "")
    if abs_path in ndds_profiles:
        return dds.QosProvider.default
    return dds.QosProvider(abs_path)


def create_participant(
    qos_provider: dds.QosProvider,
    domain_id: int = DOMAIN_ID,
    domain_tag: str = DOMAIN_TAG,
    dp_partitions: list[str] | None = None,
    participant_name: str | None = None,
    app_name: str | None = None,
) -> dds.DomainParticipant:
    participant_qos = qos_provider.participant_qos_from_profile(
        f"{QOS_LIB}::AtcParticipantProfile"
    )
    if domain_tag:
        participant_qos.property.set(
            {DOMAIN_TAG_PROPERTY: domain_tag},
            propagate=True,
        )
    if dp_partitions:
        participant_qos.partition.name = dp_partitions
    if participant_name:
        participant_qos.participant_name.name = participant_name
    if app_name:
        participant_qos.participant_name.role_name = app_name
    # Disable shared memory — use only UDPv4
    participant_qos.transport_builtin.mask = dds.TransportBuiltinMask.UDPv4
    # Allow longer CFT filter parameter strings (default 256 is too short for geo bboxes)
    participant_qos.resource_limits.contentfilter_property_max_length = 512
    return dds.DomainParticipant(domain_id, participant_qos)


def create_publisher(
    participant: dds.DomainParticipant,
    partitions: list[str] | None = None,
) -> dds.Publisher:
    if partitions:
        pub_qos = participant.default_publisher_qos
        pub_qos.partition.name = partitions
        return dds.Publisher(participant, pub_qos)
    return dds.Publisher(participant)


def create_subscriber(
    participant: dds.DomainParticipant,
    partitions: list[str] | None = None,
) -> dds.Subscriber:
    if partitions:
        sub_qos = participant.default_subscriber_qos
        sub_qos.partition.name = partitions
        return dds.Subscriber(participant, sub_qos)
    return dds.Subscriber(participant)


def writer_qos(qos_provider: dds.QosProvider, profile: str) -> dds.DataWriterQos:
    return qos_provider.datawriter_qos_from_profile(f"{QOS_LIB}::{profile}")


def reader_qos(qos_provider: dds.QosProvider, profile: str) -> dds.DataReaderQos:
    return qos_provider.datareader_qos_from_profile(f"{QOS_LIB}::{profile}")


# ── Sim-speed QoS scaling ──────────────────────────────────────────────
#
# The XML profiles define QoS time values for real-time (speed = 1).
# For accelerated simulations the wall-clock periods must shrink
# proportionally so that DDS deadlines, liveliness checks, etc.
# remain meaningful.
#


def _scale_duration(dur: dds.Duration, factor: float) -> dds.Duration:
    """Divide a Duration by *factor*; INFINITE and zero are returned as-is."""
    if dur == dds.Duration.infinite or dur == dds.Duration.zero:
        return dur
    return dds.Duration.from_seconds(dur.to_seconds() / factor)


def writer_qos_for_speed(
    qos_provider: dds.QosProvider, profile: str, sim_speed: float,
) -> dds.DataWriterQos:
    """Load writer QoS from *profile* and scale time policies by *sim_speed*.

    Divides deadline, liveliness, latency-budget, and lifespan durations
    so wall-clock periods remain correct at higher sim speeds.
    """
    qos = writer_qos(qos_provider, profile)
    if sim_speed <= 1.0:
        return qos
    qos.deadline.period = _scale_duration(qos.deadline.period, sim_speed)
    qos.liveliness.lease_duration = _scale_duration(
        qos.liveliness.lease_duration, sim_speed,
    )
    qos.latency_budget.duration = _scale_duration(
        qos.latency_budget.duration, sim_speed,
    )
    qos.lifespan.duration = _scale_duration(qos.lifespan.duration, sim_speed)
    return qos


def reader_qos_for_speed(
    qos_provider: dds.QosProvider, profile: str, sim_speed: float,
) -> dds.DataReaderQos:
    """Load reader QoS from *profile* and scale time policies by *sim_speed*."""
    qos = reader_qos(qos_provider, profile)
    if sim_speed <= 1.0:
        return qos
    qos.deadline.period = _scale_duration(qos.deadline.period, sim_speed)
    qos.liveliness.lease_duration = _scale_duration(
        qos.liveliness.lease_duration, sim_speed,
    )
    qos.latency_budget.duration = _scale_duration(
        qos.latency_budget.duration, sim_speed,
    )
    qos.time_based_filter.minimum_separation = _scale_duration(
        qos.time_based_filter.minimum_separation, sim_speed,
    )
    return qos
