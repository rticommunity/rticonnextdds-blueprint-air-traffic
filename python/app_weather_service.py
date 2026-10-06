# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0
"""
Weather Service — Publishes ConvectiveCell (storm cells) for en-route weather hazards.

Mirrors the real-world Center Weather Service Unit (CWSU).  Publishes the
scenario's forecast cells at their scripted simulated times, spawns
unforecast (random) cells within CONUS, moves cells along their heading, and
disposes instances when cells dissipate.  See docs/SCENARIO.md ("weather").

Centers subscribe to ConvectiveCell to reroute aircraft around weather.
The Dashboard subscribes to visualise cells on the map.
"""

import argparse
import math
import os
import signal
import sys
import time


import rti.connextdds as dds
from air_traffic_types import NationalAirTrafficControl as ATC

ConvectiveCell = ATC.ConvectiveCell
ConvectiveSeverity = ATC.ConvectiveSeverity
from common import (
    SimClock,
    add_wall_start_time_arg,
    create_participant,
    create_publisher,
    load_airport_coords,
    load_qos_provider,
    load_scenario,
    make_id,
    now_ms,
    parse_utc,
    scenario_rng,
    setup_logging,
    writer_qos,
    writer_qos_for_speed,
)
import common

log = setup_logging("weather_service")

shutdown_flag = False


def signal_handler(_sig, _frame):
    global shutdown_flag
    shutdown_flag = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# CONUS bounding box for random cell spawning
SPAWN_LAT_MIN, SPAWN_LAT_MAX = 28.0, 45.0
SPAWN_LON_MIN, SPAWN_LON_MAX = -115.0, -75.0

SEVERITY = {
    "MODERATE": ConvectiveSeverity.MODERATE,
    "SEVERE": ConvectiveSeverity.SEVERE,
    "EXTREME": ConvectiveSeverity.EXTREME,
}
UNFORECAST_MODES = ("always", "after_forecast", "off")


class ActiveCell:
    """Tracks a live convective cell with its remaining lifetime."""

    def __init__(self, cell_id: str, lat: float, lon: float,
                 radius_nm: float, base_alt: int, top_alt: int,
                 severity: ConvectiveSeverity,
                 heading_deg: float, speed_kt: float,
                 lifetime_s: float):
        self.cell_id = cell_id
        self.lat = lat
        self.lon = lon
        self.radius_nm = radius_nm
        self.base_alt = base_alt
        self.top_alt = top_alt
        self.severity = severity
        self.heading_deg = heading_deg
        self.speed_kt = speed_kt
        self.lifetime_s = lifetime_s
        self.age_s = 0.0

    def advance(self, dt_s: float):
        """Move cell by dt_s of sim-time."""
        self.age_s += dt_s
        nm = self.speed_kt / 3600.0 * dt_s
        self.lat += (nm * math.cos(math.radians(self.heading_deg))) / 60.0
        self.lon += (nm * math.sin(math.radians(self.heading_deg))) / (
            60.0 * max(math.cos(math.radians(self.lat)), 0.01)
        )

    @property
    def expired(self) -> bool:
        return self.age_s >= self.lifetime_s

    def to_sample(self) -> ConvectiveCell:
        return ConvectiveCell(
            cell_id=self.cell_id,
            center_latitude=self.lat,
            center_longitude=self.lon,
            radius_nm=self.radius_nm,
            top_altitude_ft=self.top_alt,
            base_altitude_ft=self.base_alt,
            severity=self.severity,
            movement_heading_deg=self.heading_deg,
            movement_speed_knots=self.speed_kt,
            observation_time=now_ms(),
        )


class WeatherService:
    """Publishes and manages convective cells over DDS."""

    def __init__(
        self,
        config_path: str,
        publish_interval_s: float = 300.0,
        wall_start_time=None,
    ):
        self.publish_interval_s = publish_interval_s
        self.config_path = config_path
        self.cells: dict[str, ActiveCell] = {}
        self._time_since_spawn = 0.0
        self._time_since_publish = 0.0

        # Scenario weather: scripted forecast cells + unforecast random cells
        weather = load_scenario(config_path).get("weather", {})
        self.forecast_cells = sorted(
            weather.get("forecast_cells", []),
            key=lambda c: parse_utc(c["sim_appear_time"]),
        )
        self.sim_valid_until = (
            parse_utc(weather["sim_valid_until"]) if weather.get("sim_valid_until") else None
        )
        unforecast = weather.get("unforecast", {})
        self.unforecast_mode = unforecast.get("mode", "always")
        if self.unforecast_mode not in UNFORECAST_MODES:
            raise ValueError(f"weather.unforecast.mode must be one of {UNFORECAST_MODES}")
        self.spawn_interval_s = float(unforecast.get("sim_spawn_every_min", 0.5)) * 60.0
        self.max_cells = int(unforecast.get("max_cells", 5))
        self.rng = scenario_rng(config_path, "weather")
        self._airport_coords = load_airport_coords(config_path)

        # DDS
        self.qos_provider = load_qos_provider()
        dp_partitions = [
            "OPS/WEATHER/*",
            "OPS/ENROUTE/*",
        ]
        self.participant = create_participant(
            self.qos_provider,
            dp_partitions=dp_partitions,
            participant_name="WeatherService",
            app_name="ATC_WeatherService",
        )
        self.clock = SimClock(self.participant, config_path, wall_start_time)
        self.publisher = create_publisher(self.participant)

        cell_topic = dds.Topic(self.participant, "ConvectiveCell", ConvectiveCell)
        speed = self.clock.speed()
        self.cell_writer = dds.DataWriter(
            self.publisher, cell_topic,
            writer_qos_for_speed(self.qos_provider, "ConvectiveCellProfile", speed),
        )
        self._last_qos_speed = speed

        log.info(
            "WeatherService initialized — %d forecast cell(s), unforecast %s "
            "(every %.0fs sim, max %d), publish every %.0fs",
            len(self.forecast_cells), self.unforecast_mode,
            self.spawn_interval_s, self.max_cells, publish_interval_s,
        )

    def _forecast_position(self, spec: dict) -> tuple[float, float]:
        """Cell center from lat/lon, or a fraction along the route between two airports.

        on_airway uses the same straight lat/lon interpolation that aircraft
        use for their routes, so the cell sits on that route.
        """
        if "on_airway" in spec:
            a, b = spec["on_airway"]
            frac = float(spec.get("fraction", 0.5))
            (alat, alon), (blat, blon) = self._airport_coords[a], self._airport_coords[b]
            return alat + (blat - alat) * frac, alon + (blon - alon) * frac
        return float(spec["lat"]), float(spec["lon"])

    def _spawn_forecast_cells(self, sim_now):
        """Publish every forecast cell whose sim_appear_time has arrived."""
        spawned = False
        while self.forecast_cells and parse_utc(self.forecast_cells[0]["sim_appear_time"]) <= sim_now:
            spec = self.forecast_cells.pop(0)
            lat, lon = self._forecast_position(spec)
            cell_id = spec.get("id") or make_id("WX-FCST-")
            cell = ActiveCell(
                cell_id=cell_id, lat=lat, lon=lon,
                radius_nm=float(spec.get("radius_nm", 20.0)),
                base_alt=int(spec.get("base_altitude_ft", 10000)),
                top_alt=int(spec.get("top_altitude_ft", 45000)),
                severity=SEVERITY[spec.get("severity", "SEVERE").upper()],
                heading_deg=float(spec.get("heading_deg", 0.0)),
                speed_kt=float(spec.get("speed_kt", 0.0)),
                lifetime_s=float(spec.get("sim_duration_min", 60)) * 60.0,
            )
            self.cells[cell_id] = cell
            spawned = True
            log.info(
                "Forecast cell %s appeared at (%.1f, %.1f) r=%.0fnm %s — lifetime %.0fs",
                cell_id, lat, lon, cell.radius_nm, cell.severity.name, cell.lifetime_s,
            )
        return spawned

    def _unforecast_active(self, sim_now) -> bool:
        if self.unforecast_mode == "off":
            return False
        if self.unforecast_mode == "after_forecast":
            return self.sim_valid_until is None or sim_now >= self.sim_valid_until
        return True

    def _spawn_cell(self):
        """Create a new random (unforecast) convective cell."""
        rng = self.rng
        cell_id = make_id("WX-")
        lat = rng.uniform(SPAWN_LAT_MIN, SPAWN_LAT_MAX)
        lon = rng.uniform(SPAWN_LON_MIN, SPAWN_LON_MAX)
        radius = rng.uniform(8.0, 30.0)
        base_alt = rng.choice([10000, 15000, 18000])
        top_alt = rng.choice([35000, 40000, 45000])
        severity = rng.choice([
            ConvectiveSeverity.MODERATE,
            ConvectiveSeverity.MODERATE,
            ConvectiveSeverity.SEVERE,
            ConvectiveSeverity.EXTREME,
        ])
        heading = rng.uniform(30, 120)   # generally SW→NE movement
        speed = rng.uniform(15.0, 45.0)
        # Real single-cell storms last 30-60 min; at 450 kt cruise that's
        # 225-450 nm — the distance scale that makes reroutes meaningful.
        lifetime = rng.uniform(1800, 3600)  # 30–60 minutes of sim-time

        cell = ActiveCell(
            cell_id=cell_id, lat=lat, lon=lon,
            radius_nm=radius, base_alt=base_alt, top_alt=top_alt,
            severity=severity, heading_deg=heading, speed_kt=speed,
            lifetime_s=lifetime,
        )
        self.cells[cell_id] = cell
        log.info(
            "Spawned cell %s at (%.1f, %.1f) r=%.0fnm %s — lifetime %.0fs",
            cell_id, lat, lon, radius, severity.name, lifetime,
        )

    def _publish_cells(self):
        """Publish all active cells."""
        for cell in self.cells.values():
            self.cell_writer.write(cell.to_sample())

    def _dispose_cell(self, cell_id: str):
        """Dispose (remove) a dissipated cell."""
        sample = ConvectiveCell(cell_id=cell_id)
        handle = self.cell_writer.lookup_instance(sample)
        if handle is not None and not handle.is_nil:
            self.cell_writer.dispose_instance(handle)
            log.info("Disposed cell %s (dissipated)", cell_id)

    def run(self, duration_s: float = 120.0):
        """Main loop — advance cells every wall-tick, publish at sim-time interval."""
        log.info("WeatherService running")
        start = time.time()
        TICK = 1.0  # wall-clock seconds between iterations

        while not shutdown_flag and (time.time() - start) < duration_s:
            sim_speed = self.clock.speed()
            sim_now = self.clock.now()
            dt = TICK * sim_speed
            if sim_speed != self._last_qos_speed:
                self._last_qos_speed = sim_speed
                self.cell_writer.qos = writer_qos_for_speed(
                    self.qos_provider, "ConvectiveCellProfile", sim_speed,
                )
                log.info("Cell QoS rescaled for speed=%.1fx", sim_speed)

            # Advance all cells
            for cell in list(self.cells.values()):
                cell.advance(dt)

            # Remove expired cells
            expired = [cid for cid, c in self.cells.items() if c.expired]
            for cid in expired:
                self._dispose_cell(cid)
                del self.cells[cid]

            # Forecast cells appear at their scripted times; unforecast cells
            # spawn periodically when the scenario allows it
            spawned = self._spawn_forecast_cells(sim_now)
            if self._unforecast_active(sim_now):
                self._time_since_spawn += dt
                if self._time_since_spawn >= self.spawn_interval_s and len(self.cells) < self.max_cells:
                    self._spawn_cell()
                    self._time_since_spawn = 0.0
                    spawned = True
            if spawned:
                # Publish immediately on spawn so subscribers see the new cell
                self._publish_cells()
                self._time_since_publish = 0.0
            else:
                # Publish at realistic radar interval (default 5 min sim-time)
                self._time_since_publish += dt
                if self._time_since_publish >= self.publish_interval_s:
                    self._publish_cells()
                    self._time_since_publish = 0.0

            time.sleep(TICK)

        # Clean up — dispose all remaining cells
        for cid in list(self.cells):
            self._dispose_cell(cid)
        log.info("WeatherService shutdown — disposed %d cells", len(self.cells))


def main():
    parser = argparse.ArgumentParser(description="ATC Weather Service (ConvectiveCell)")
    parser.add_argument("--config", required=True, help="Path to scenario config JSON")
    parser.add_argument("--qos-file", required=True, help="Path to QoS XML file")
    parser.add_argument("--duration", type=float, default=120.0, help="Run duration in seconds")
    parser.add_argument("--publish-interval", type=float, default=300.0,
                        help="Cell publication interval in sim-time seconds (default: 300 = 5 min)")
    add_wall_start_time_arg(parser)
    args = parser.parse_args()

    common.QOS_FILE = args.qos_file

    svc = WeatherService(
        config_path=args.config,
        publish_interval_s=args.publish_interval,
        wall_start_time=args.wall_start_time,
    )
    svc.run(duration_s=args.duration)
    svc.participant.close()
    
    dds.DomainParticipant.finalize_participant_factory()



if __name__ == "__main__":
    main()
