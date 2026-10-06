# SPDX-FileCopyrightText: 2026 Real-Time Innovations, Inc.
# SPDX-License-Identifier: Apache-2.0
"""
Airplane Application — Aircraft simulator.

Publishes AircraftPosition at ~5 Hz, subscribes to ControllerInstruction via CFT,
publishes PilotAcknowledgment, and uses Request/Reply for flight plan filing and
gate assignment.

Each aircraft flies its own schedule from the scenario file.  For each leg it
waits at the gate until the leg's sim_departure_time (if any) and the end of
its turnaround, requests pushback (releasing the gate), flies the leg, and
requests a gate on arrival.  After the last leg it stays parked ("park") or
keeps choosing its own next legs ("continue").
"""

import argparse
import datetime
import math
import os
import random
import re
import signal
import sys
import time

import rti.connextdds as dds
from rti.rpc import Requester
from air_traffic_types import NationalAirTrafficControl as ATC

AircraftPosition = ATC.AircraftPosition
AcknowledgmentStatus = ATC.AcknowledgmentStatus
ControllerInstruction = ATC.ControllerInstruction
FlightPlan = ATC.FlightPlan
FlightPlanRequest = ATC.FlightPlanRequest
FlightPlanResponse = ATC.FlightPlanResponse
FlightPlanStatus = ATC.FlightPlanStatus
FlightPhase = ATC.FlightPhase
GateAssignmentReply = ATC.GateAssignmentReply
GateRequest = ATC.GateRequest
GateRequestKind = ATC.GateRequestKind
GateAssignmentStatusKind = ATC.GateAssignmentStatusKind
GeoPosition = ATC.GeoPosition
InstructionType = ATC.InstructionType
NavStatus = ATC.NavStatus
PilotAcknowledgment = ATC.PilotAcknowledgment
Waypoint = ATC.Waypoint
WeatherReport = ATC.WeatherReport
from common import (
    SimClock,
    add_wall_start_time_arg,
    bearing_deg,
    create_participant,
    create_publisher,
    create_subscriber,
    distance_nm,
    load_aircraft_config,
    load_airport_coords,
    load_qos_provider,
    load_scenario,
    load_tracon_for_airport,
    make_id,
    now_ms,
    parse_utc,
    reader_qos,
    scenario_rng,
    setup_logging,
    writer_qos,
)
import common

log = setup_logging("airplane")

# Loaded lazily after --config is parsed
AIRPORT_COORDS = {}

shutdown_flag = False


def signal_handler(_sig, _frame):
    global shutdown_flag
    shutdown_flag = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


class AirplaneSimulator:
    """Simulates a single aircraft in the ATC system."""

    def __init__(
        self,
        tail_number: str,
        schedule: list[dict],
        config_path: str,
        after_schedule: str = "park",
        sim_turnaround_min: float = 45.0,
        wall_start_time=None,
        cruise_alt: float = 35000.0,
    ):
        """schedule: legs (flights) as dicts with "callsign", "from", "to",
        and an optional "sim_departure_time" (datetime).  Consecutive legs
        must connect.  The tail number identifies the aircraft; the callsign
        (flight number) changes with each leg."""
        self.tail_number = tail_number
        self.schedule = list(schedule)
        self.after_schedule = after_schedule
        self.sim_turnaround_min = sim_turnaround_min
        self.config_path = config_path
        self.cruise_alt = cruise_alt
        # Cosmetic variation comes from fixed identities, so it is identical
        # on every run without needing a seed: the starting gate position
        # from the tail number, each flight's route offsets from its callsign
        self._jitter = random.Random(f"jitter:{tail_number}")
        # Legs chosen after the schedule ends ("continue") are random; with a
        # scenario seed they repeat exactly
        self._rng = scenario_rng(config_path, "aircraft", tail_number)
        self.assigned_runway: str | None = None
        self.assigned_gate: str | None = None

        # Simulation state — start at the first leg's origin gate
        first = self.schedule[0]
        olat, olon = AIRPORT_COORDS.get(first["from"], (40.6413, -73.7781))
        self.lat = olat + self._jitter.uniform(-0.02, 0.02)
        self.lon = olon + self._jitter.uniform(-0.02, 0.02)
        self._set_leg_state(first["from"], first["to"], first["callsign"])

        # DDS setup
        self.qos_provider = load_qos_provider()
        self._tracon_for_airport = load_tracon_for_airport(config_path)
        self.participant = create_participant(
            self.qos_provider,
            dp_partitions=self._dp_partitions(self.origin, self.destination),
            participant_name=f"Airplane_{tail_number}",
            app_name="ATC_Airplane",
        )
        self.clock = SimClock(self.participant, config_path, wall_start_time)

        self.publisher = create_publisher(self.participant)
        self.subscriber = create_subscriber(self.participant)

        # Position writer
        pos_topic = dds.Topic(self.participant, "AircraftPosition", AircraftPosition)
        self.pos_writer = dds.DataWriter(
            self.publisher, pos_topic,
            writer_qos(self.qos_provider, "AircraftPositionProfile"),
        )

        # Ack writer
        ack_topic = dds.Topic(self.participant, "PilotAcknowledgment", PilotAcknowledgment)
        self.ack_writer = dds.DataWriter(
            self.publisher, ack_topic,
            writer_qos(self.qos_provider, "PilotAcknowledgmentProfile"),
        )

        # Instruction reader (content-filtered by tail_number)
        instr_topic = dds.Topic(self.participant, "ControllerInstruction", ControllerInstruction)
        self.instr_cft = dds.ContentFilteredTopic(
            instr_topic,
            f"MyInstructions_{tail_number}",
            dds.Filter(f"tail_number = '{tail_number}'"),
        )
        self.instr_reader = dds.DataReader(
            self.subscriber, self.instr_cft,
            reader_qos(self.qos_provider, "ControllerInstructionProfile"),
        )

        # Weather reader (content-filtered by destination; the parameter is
        # updated when a new leg starts)
        wx_topic = dds.Topic(self.participant, "WeatherReport", WeatherReport)
        self.wx_cft = dds.ContentFilteredTopic(
            wx_topic,
            f"DestWeather_{tail_number}",
            dds.Filter("airport_code = %0", [f"'{self.destination}'"]),
        )
        self.wx_reader = dds.DataReader(
            self.subscriber, self.wx_cft,
            reader_qos(self.qos_provider, "WeatherReportProfile"),
        )

        # Gate assignment requester (created once, reused for every gate
        # assignment and pushback)
        self.gate_requester = Requester(
            request_type=GateRequest,
            reply_type=GateAssignmentReply,
            participant=self.participant,
            service_name="GateAssignmentService",
            datawriter_qos=writer_qos(self.qos_provider, "GateAssignmentRequestReplyProfile"),
            datareader_qos=reader_qos(self.qos_provider, "GateAssignmentRequestReplyProfile"),
        )

        log.info(
            "Aircraft %s initialized: %s (%s after the schedule)",
            tail_number,
            ", ".join(f"{leg['callsign']} {leg['from']}->{leg['to']}" for leg in self.schedule),
            after_schedule,
        )

    def _set_leg_state(self, origin: str, destination: str, callsign: str):
        """Reset the flight state for a new leg starting at the origin gate."""
        self.callsign = callsign
        self.origin = origin
        self.destination = destination
        self._route_jitter = random.Random(f"route:{callsign}")
        dlat, dlon = AIRPORT_COORDS.get(destination, (33.9425, -118.4081))
        self.alt = 0.0
        self.heading = bearing_deg(self.lat, self.lon, dlat, dlon)
        self.ground_speed = 0.0
        self.vertical_speed = 0.0
        self.phase = FlightPhase.PREFLIGHT
        self.assigned_runway = None
        # Build waypoint list along the route
        self.waypoints = self._build_waypoints(self.lat, self.lon, dlat, dlon)
        self.current_wp_index = 0  # index of next waypoint to fly to
        # Distance tracking for descent planning
        self._total_route_nm = distance_nm(self.lat, self.lon, dlat, dlon)
        # Fuel: load proportional to route distance so all flights land
        # with ~15-20% reserve.  Burn rate is 0.001/tick; at cruise 450 kt
        # that's ~0.04%/nm.  Initial fuel = estimated_burn + 20% reserve,
        # clamped to [40, 100].
        estimated_burn = self._total_route_nm * 0.04
        self.fuel = min(100.0, max(40.0, estimated_burn + 20.0))
        # Weather deviation: when a HEADING instruction is received for
        # weather avoidance, hold heading indefinitely until Center issues
        # a CLEARANCE to resume own navigation.
        self._wx_deviating = False

    def _dp_partitions(self, origin: str, destination: str) -> list[str]:
        """Participant partitions for a leg: its airports and terminal areas."""
        terminal_partitions = []
        for airport in (origin, destination):
            tracon = self._tracon_for_airport.get(airport)
            if tracon and f"OPS/TERMINAL/{tracon}" not in terminal_partitions:
                terminal_partitions.append(f"OPS/TERMINAL/{tracon}")
        return [
            "OPS/FPS/*",
            *terminal_partitions,
            "OPS/ENROUTE/*",
            f"OPS/AIRPORT/{origin}",
            f"OPS/AIRPORT/{destination}",
        ]

    def _start_leg(self, origin: str, destination: str, callsign: str):
        """Begin a later leg: new flight state, partitions, and weather filter."""
        self._set_leg_state(origin, destination, callsign)
        qos = self.participant.qos
        qos.partition.name = self._dp_partitions(origin, destination)
        self.participant.qos = qos
        self.wx_cft.filter_parameters = [f"'{destination}'"]

    # ── Navigation helpers ──────────────────────────────────────────────

    @staticmethod
    def _interpolate(lat1, lon1, lat2, lon2, frac):
        """Linearly interpolate between two points by fraction [0,1]."""
        return (lat1 + (lat2 - lat1) * frac, lon1 + (lon2 - lon1) * frac)

    def _build_waypoints(self, olat, olon, dlat, dlon):
        """Generate waypoints along the route: DEPART, intermediate points, ARRIVE."""
        dist_nm = distance_nm(olat, olon, dlat, dlon)
        # Short routes get fewer waypoints; long routes get more
        n_intermediate = max(1, min(6, int(dist_nm / 400)))
        wpts = [("DEPART", olat, olon, 0.0)]
        for i in range(1, n_intermediate + 1):
            frac = i / (n_intermediate + 1)
            ilat, ilon = self._interpolate(olat, olon, dlat, dlon, frac)
            # Add slight lateral offset for realism (±0.3°)
            ilat += self._route_jitter.uniform(-0.3, 0.3)
            ilon += self._route_jitter.uniform(-0.3, 0.3)
            name = f"WP{i:02d}"
            wpts.append((name, ilat, ilon, self.cruise_alt))
        wpts.append(("ARRIVE", dlat, dlon, 0.0))
        return wpts  # list of (name, lat, lon, alt)

    def _steer_to_waypoint(self):
        """Update heading to aim at the current target waypoint."""
        if self.current_wp_index >= len(self.waypoints):
            return
        _, wlat, wlon, _ = self.waypoints[self.current_wp_index]
        dist = distance_nm(self.lat, self.lon, wlat, wlon)
        # Advance to next waypoint if we're within 5nm
        if dist < 5.0 and self.current_wp_index < len(self.waypoints) - 1:
            self.current_wp_index += 1
            _, wlat, wlon, _ = self.waypoints[self.current_wp_index]
            log.info("Passing waypoint → next: %s", self.waypoints[self.current_wp_index][0])
        self.heading = bearing_deg(self.lat, self.lon, wlat, wlon)

    # ── Flight plan filing ──────────────────────────────────────────────

    def file_flight_plan(self):
        """File a flight plan via Request/Reply."""
        try:
            requester = Requester(
                request_type=FlightPlanRequest,
                reply_type=FlightPlanResponse,
                participant=self.participant,
                service_name="FlightPlanFilingService",
                datawriter_qos=writer_qos(self.qos_provider, "FlightPlanRequestReplyProfile"),
                datareader_qos=reader_qos(self.qos_provider, "FlightPlanRequestReplyProfile"),
            )

            if not requester.wait_for_service(dds.Duration(seconds=5)):
                log.warning("FlightPlanFilingService not available, proceeding without filing")
                return

            plan = FlightPlan(
                flight_plan_id=make_id("FP-"),
                tail_number=self.tail_number,
                callsign=self.callsign,
                departure_airport=self.origin,
                arrival_airport=self.destination,
                waypoints=[
                    Waypoint(name=n, position=GeoPosition(lat, lon, alt))
                    for n, lat, lon, alt in self.waypoints
                ],
                scheduled_departure_time=now_ms(),
                status=FlightPlanStatus.FILED,
                last_updated=now_ms(),
            )

            request = FlightPlanRequest(plan=plan)
            request_id = requester.send_request(request)
            replies = requester.receive_replies(
                dds.Duration(seconds=10),
                related_request_id=request_id,
            )

            for reply, info in replies:
                if info.valid:
                    status = "ACCEPTED" if reply.accepted else "REJECTED"
                    log.info("Flight plan %s: %s", status, reply.message)

        except Exception as e:
            log.warning("Flight plan filing failed: %s", e)

    def request_pushback(self):
        """Tell the origin airport's ramp control we are leaving the gate.

        The airport releases the gate for the next arrival.  The request is
        sent without waiting for discovery or for the reply, so departure
        timing depends only on the simulated clock (runs stay repeatable).
        On a first departure no gate is assigned yet, so a request sent
        before the airport is discovered loses nothing.  The airport's
        RELEASED reply is collected later, in request_gate().
        """
        try:
            self.gate_requester.send_request(GateRequest(
                flight_id=self.tail_number,
                aerodrome_id=self.origin,
                requested_timestamp=now_ms(),
                kind=GateRequestKind.PUSHBACK,
            ))
            log.info("Pushback from %s at %s", self.assigned_gate or "the gate", self.origin)
            self.assigned_gate = None
        except Exception as e:
            log.warning("Pushback request failed: %s", e)

    def request_gate(self):
        """Request a gate at the destination (ASSIGN) after landing."""
        try:
            # Collect ramp control's confirmations of earlier pushbacks
            for reply, info in self.gate_requester.take_replies():
                if info.valid and reply.assignment.status == GateAssignmentStatusKind.RELEASED:
                    log.info("Ramp control confirmed pushback (%s released)",
                             reply.assignment.gate_name or "no gate")

            if not self.gate_requester.wait_for_service(dds.Duration(seconds=5)):
                log.warning("GateAssignmentService not available")
                return

            request = GateRequest(
                flight_id=self.tail_number,
                aerodrome_id=self.destination,
                requested_timestamp=now_ms(),
                kind=GateRequestKind.ASSIGN,
            )
            request_id = self.gate_requester.send_request(request)
            replies = self.gate_requester.receive_replies(
                dds.Duration(seconds=10),
                related_request_id=request_id,
            )

            for reply, info in replies:
                if info.valid:
                    a = reply.assignment
                    self.assigned_gate = a.gate_name or None
                    log.info("Gate assignment: %s at %s (%s)",
                             a.gate_name, self.destination, a.status.name)

        except Exception as e:
            log.warning("Gate request failed: %s", e)

    def _dist_to_destination(self):
        """Distance in nm from current position to destination airport."""
        dlat, dlon = AIRPORT_COORDS.get(self.destination, (33.9425, -118.4081))
        return distance_nm(self.lat, self.lon, dlat, dlon)

    def advance_simulation(self):
        """Advance aircraft position and state by one tick."""
        speed = self.clock.speed()
        TICK = 0.2 * speed  # seconds of sim-time per tick (5 Hz wall-clock)

        # Always steer toward waypoints when airborne (unless wx deviation active)
        if self.phase not in (FlightPhase.PREFLIGHT, FlightPhase.TAXI_OUT,
                               FlightPhase.TAXI_IN, FlightPhase.PARKED):
            if not self._wx_deviating:
                self._steer_to_waypoint()

        # Auto-trigger descent when close enough to destination
        # Descent from cruise_alt at 1500 fpm, at ~450 kt ground speed
        # Rule of thumb: start descent at (alt_to_lose / 1000) * 3 nm
        if self.phase == FlightPhase.CRUISE:
            descent_nm = (self.alt / 1000.0) * 3.0
            if self._dist_to_destination() <= descent_nm:
                self.phase = FlightPhase.DESCENT
                self.vertical_speed = -1500.0
                self.ground_speed = 350.0
                log.info("Top of descent — %.0f nm from %s", self._dist_to_destination(), self.destination)

        if self.phase == FlightPhase.PREFLIGHT:
            self.phase = FlightPhase.TAXI_OUT
            self.ground_speed = 15.0
        elif self.phase == FlightPhase.TAXI_OUT:
            self.phase = FlightPhase.TAKEOFF
            self.ground_speed = 150.0
            self.vertical_speed = 2500.0
        elif self.phase == FlightPhase.TAKEOFF:
            self.alt += self.vertical_speed / 60.0 * TICK
            if self.alt >= 1500:
                self.phase = FlightPhase.CLIMB
                self.ground_speed = 350.0
                log.info("Leaving tower airspace — CLIMB (%.0fft)", self.alt)
        elif self.phase == FlightPhase.CLIMB:
            self.alt += self.vertical_speed / 60.0 * TICK
            if self.alt >= self.cruise_alt:
                self.alt = self.cruise_alt
                self.phase = FlightPhase.CRUISE
                self.vertical_speed = 0
                self.ground_speed = 450.0
        elif self.phase == FlightPhase.CRUISE:
            pass  # Steady state — descent auto-triggered above
        elif self.phase == FlightPhase.DESCENT:
            self.alt += self.vertical_speed / 60.0 * TICK
            if self.alt <= 3000:
                self.phase = FlightPhase.APPROACH
                self.ground_speed = 180.0
                log.info("Entering tower airspace — APPROACH (%.0fft)", self.alt)
        elif self.phase == FlightPhase.APPROACH:
            self.alt += self.vertical_speed / 60.0 * TICK
            if self.alt <= 200:
                self.phase = FlightPhase.LANDING
        elif self.phase == FlightPhase.LANDING:
            self.alt = 0
            self.ground_speed = 60.0
            self.vertical_speed = 0
            self.phase = FlightPhase.TAXI_IN
        elif self.phase == FlightPhase.TAXI_IN:
            self.ground_speed = 15.0
            # Snap to destination coords
            dlat, dlon = AIRPORT_COORDS.get(self.destination, (self.lat, self.lon))
            self.lat, self.lon = dlat, dlon
            self.phase = FlightPhase.PARKED
            self.ground_speed = 0.0
            self.vertical_speed = 0.0

        # Advance position based on heading
        if self.ground_speed > 0 and self.phase not in (FlightPhase.PARKED,):
            nm_per_tick = self.ground_speed / 3600.0 * TICK
            # Correct for longitude convergence at latitude
            self.lat += (nm_per_tick * math.cos(math.radians(self.heading))) / 60.0
            self.lon += (nm_per_tick * math.sin(math.radians(self.heading))) / (60.0 * math.cos(math.radians(self.lat)))

        # Burn fuel (scaled by sim speed so consumption is consistent in sim-time)
        # ~0.005%/sim-sec at cruise: SFO-JFK (~5h) lands with ~10%, JFK-ORD (~1.6h) with ~71%
        if self.phase not in (FlightPhase.PREFLIGHT, FlightPhase.PARKED):
            self.fuel = max(5.0, self.fuel - 0.001 * speed)

    def publish_position(self):
        """Publish current aircraft position."""
        sample = AircraftPosition(
            tail_number=self.tail_number,
            callsign=self.callsign,
            position=GeoPosition(self.lat, self.lon, self.alt),
            ground_speed_knots=self.ground_speed,
            vertical_speed_fpm=self.vertical_speed,
            heading_degrees=self.heading,
            flight_phase=self.phase,
            origin_airport=self.origin,
            destination_airport=self.destination,
            fuel_level_percent=self.fuel,
            nav_status=NavStatus.WEATHER_DEVIATION if self._wx_deviating else NavStatus.NORMAL,
            assigned_runway=self.assigned_runway,
            assigned_gate=self.assigned_gate,
            timestamp=now_ms(),
        )
        self.pos_writer.write(sample)

    def process_instructions(self):
        """Read and acknowledge any pending controller instructions."""
        for sample in self.instr_reader.take_data():
            log.debug(
                "Instruction from %s: %s %s",
                sample.controller_id,
                sample.instruction_type.name,
                sample.clearance_text or "",
            )

            # Apply instruction
            if sample.instruction_type == InstructionType.HEADING and sample.assigned_heading_degrees is not None:
                self.heading = sample.assigned_heading_degrees
                self._wx_deviating = True
                log.info("Weather deviation: HDG %.0f (holding until CLEARANCE)", self.heading)
            elif sample.instruction_type == InstructionType.CLEARANCE and sample.clearance_text:
                self._handle_clearance(sample)
            elif sample.instruction_type == InstructionType.ALTITUDE and sample.assigned_altitude_feet is not None:
                target = sample.assigned_altitude_feet
                self.vertical_speed = 2000 if target > self.alt else -1500
                if target < self.alt:
                    self.phase = FlightPhase.DESCENT

            # Send acknowledgment
            ack = PilotAcknowledgment(
                acknowledgment_id=make_id("ACK-"),
                instruction_id=sample.instruction_id,
                tail_number=self.tail_number,
                status=AcknowledgmentStatus.WILCO,
                response_text=f"WILCO {sample.instruction_type.name}",
                acknowledged_at=now_ms(),
            )
            self.ack_writer.write(ack)

    def _handle_clearance(self, sample):
        """Handle a CLEARANCE instruction — typically 'resume own nav direct WPxx'.

        The Center picks the forward waypoint and sends it in clearance_text
        as 'RESUME OWN NAV DIRECT <waypoint_name>'.  We parse the waypoint
        name and update our waypoint index accordingly.
        """
        text = sample.clearance_text or ""
        # Extract waypoint name after "DIRECT "
        wp_name = None
        if "DIRECT " in text:
            wp_name = text.split("DIRECT ", 1)[1].split()[0]

        if wp_name:
            for i, (name, _, _, _) in enumerate(self.waypoints):
                if name == wp_name:
                    old_name = self.waypoints[self.current_wp_index][0]
                    self.current_wp_index = i
                    self._wx_deviating = False
                    log.info(
                        "Resume own navigation: %s → direct %s (cleared by %s)",
                        old_name, wp_name, sample.controller_id,
                    )
                    return

        # Fallback: waypoint not found or no DIRECT — just resume nav
        self._wx_deviating = False
        log.info("CLEARANCE received from %s — resuming own navigation: %s",
                 sample.controller_id, text)

    def check_weather(self):
        """Check destination weather reports."""
        for sample in self.wx_reader.take_data():
            log.info(
                "Weather at %s: %s, vis=%.0fm, wind=%d@%.0fkt",
                sample.airport_code,
                sample.conditions.name,
                sample.visibility_meters,
                sample.wind.direction_degrees,
                sample.wind.speed_knots,
            )

    def _running(self, start: float, duration_s: float | None) -> bool:
        return not shutdown_flag and (duration_s is None or time.time() - start < duration_s)

    def _wait_at_gate(self, start, duration_s, until) -> bool:
        """Stay at the gate, publishing position, until simulated time `until`."""
        while self._running(start, duration_s) and self.clock.now() < until:
            self.publish_position()
            time.sleep(0.2)
        return self._running(start, duration_s)

    def _next_leg(self, leg_index: int) -> dict | None:
        """The leg after leg_index: from the schedule, or chosen ("continue")."""
        if leg_index + 1 < len(self.schedule):
            return self.schedule[leg_index + 1]
        if self.after_schedule == "continue":
            choices = sorted(code for code in AIRPORT_COORDS if code != self.destination)
            return {"callsign": _next_flight_number(self.callsign),
                    "from": self.destination, "to": self._rng.choice(choices)}
        return None  # "park"

    def run(self, duration_s: float | None = None):
        """Fly the schedule at ~5 Hz until the simulation ends.

        For each leg: wait at the gate until the leg's sim_departure_time and
        the end of turnaround (sim_turnaround_min after arriving), request
        pushback, file the flight plan, fly, and request a gate on arrival.
        After the last leg the aircraft parks ("park") or keeps choosing its
        own legs ("continue").  duration_s (wall seconds) of None means run
        until stopped.
        """
        start = time.time()
        leg_index, leg = 0, self.schedule[0]
        sim_ready_time = None  # end of turnaround after the previous arrival

        while self._running(start, duration_s):
            if leg_index > 0:
                # Turnaround at the gate, still parked from the previous leg
                if not self._wait_at_gate(start, duration_s, sim_ready_time):
                    break
                self._start_leg(leg["from"], leg["to"], leg["callsign"])

            # Scheduled departure time, if any
            dep = leg.get("sim_departure_time")
            if dep is not None and self.clock.now() < dep:
                log.info("At the gate in %s — scheduled departure %s",
                         self.origin, dep.isoformat())
                if not self._wait_at_gate(start, duration_s, dep):
                    break

            self.request_pushback()
            log.info("Starting flight %s %s -> %s", self.callsign, self.origin, self.destination)
            self.file_flight_plan()

            while self._running(start, duration_s) and self.phase != FlightPhase.PARKED:
                self.advance_simulation()
                self.publish_position()
                self.process_instructions()
                self.check_weather()
                time.sleep(0.2)  # 5 Hz
            if self.phase != FlightPhase.PARKED:
                break

            log.info("Aircraft parked, requesting gate")
            self.request_gate()
            sim_ready_time = self.clock.now() + datetime.timedelta(minutes=self.sim_turnaround_min)
            leg = self._next_leg(leg_index)
            if leg is None:
                break
            leg_index += 1

        # Stay parked at the gate (after_schedule "park") until the end
        while self._running(start, duration_s):
            self.publish_position()
            time.sleep(0.2)

        log.info("Aircraft %s simulation ended (phase=%s)", self.tail_number, self.phase.name)


def _random_tail_number():
    """Generate a realistic US N-number (e.g., N738WN, N12345)."""
    fmt = random.choice(["digits", "mixed"])
    if fmt == "digits":
        return f"N{random.randint(1, 99999)}"
    else:
        letters = "ABCDEFGHJKLMNPRSTUVWXYZ"  # no I, O, Q
        return f"N{random.randint(1, 999)}{random.choice(letters)}{random.choice(letters)}"


AFTER_SCHEDULE = ("park", "continue")


def _next_flight_number(callsign: str) -> str:
    """Next flight number for a leg chosen after the schedule ("continue"):
    SWA401 -> SWA402, FDX010 -> FDX011."""
    match = re.match(r"^(.*?)(\d+)$", callsign)
    if not match:
        return f"{callsign}1"
    prefix, number = match.groups()
    return f"{prefix}{int(number) + 1:0{len(number)}d}"


def _schedule(parser, args, cfg, scenario) -> tuple[list[dict], str, float]:
    """Return (legs, after_schedule, sim_turnaround_min) for this aircraft.

    --callsign/--from/--to (an ad-hoc one-leg flight departing now, then
    parking) override the aircraft's schedule in the scenario file.
    """
    default_turnaround = float(scenario.get("sim_turnaround_min", 45))
    if args.callsign or args.from_airport or args.to_airport:
        if not (args.callsign and args.from_airport and args.to_airport):
            parser.error("an ad-hoc flight needs --callsign, --from, and --to")
        return ([{"callsign": args.callsign, "from": args.from_airport,
                  "to": args.to_airport, "sim_departure_time": None}],
                "park", default_turnaround)
    if not cfg:
        parser.error(f"aircraft {args.tail_number} is not in the scenario; "
                     "give --callsign, --from, and --to for an ad-hoc flight")
    tail = cfg["tail_number"]
    schedule = cfg.get("schedule") or []
    if not schedule:
        parser.error(f"{tail}: the schedule has no legs")
    legs = []
    for i, leg in enumerate(schedule):
        if not leg.get("callsign"):
            parser.error(f"{tail}: leg {i + 1} has no callsign (flight number)")
        if i > 0 and leg["from"] != schedule[i - 1]["to"]:
            parser.error(f"{tail}: leg {i + 1} departs {leg['from']} but "
                         f"leg {i} arrives at {schedule[i - 1]['to']}")
        dep = leg.get("sim_departure_time")
        legs.append({"callsign": leg["callsign"], "from": leg["from"], "to": leg["to"],
                     "sim_departure_time": parse_utc(dep) if dep else None})
    after = cfg.get("after_schedule", "park")
    if after not in AFTER_SCHEDULE:
        parser.error(f"{tail}: after_schedule must be one of {AFTER_SCHEDULE}")
    return legs, after, float(cfg.get("sim_turnaround_min", default_turnaround))


def main():
    global AIRPORT_COORDS
    parser = argparse.ArgumentParser(description="ATC Aircraft Simulator")
    parser.add_argument("--config", required=True, help="Path to scenario config JSON")
    parser.add_argument("--qos-file", required=True, help="Path to QoS XML file")
    parser.add_argument("--tail-number", default=None,
                        help="Aircraft to fly (its schedule is in the scenario), e.g. N738WN; "
                             "for an ad-hoc flight, the tail to use (default: generated)")
    parser.add_argument("--callsign", default=None,
                        help="Ad-hoc one-leg flight: its callsign (with --from and --to)")
    parser.add_argument("--from", dest="from_airport", default=None,
                        help="Ad-hoc one-leg flight: departure airport")
    parser.add_argument("--to", dest="to_airport", default=None,
                        help="Ad-hoc one-leg flight: arrival airport")
    parser.add_argument("--duration", type=float, default=None,
                        help="Run time in wall seconds (default: until stopped)")
    add_wall_start_time_arg(parser)
    args = parser.parse_args()

    common.QOS_FILE = args.qos_file
    AIRPORT_COORDS = load_airport_coords(args.config)

    cfg = load_aircraft_config(args.tail_number, args.config) if args.tail_number else None
    tail = args.tail_number or _random_tail_number()

    global log
    log = setup_logging(tail)
    legs, after_schedule, sim_turnaround_min = _schedule(
        parser, args, cfg, load_scenario(args.config))

    airplane = AirplaneSimulator(
        tail_number=tail,
        schedule=legs,
        config_path=args.config,
        after_schedule=after_schedule,
        sim_turnaround_min=sim_turnaround_min,
        wall_start_time=args.wall_start_time,
    )
    airplane.run(duration_s=args.duration)
    airplane.gate_requester.close()
    airplane.participant.close()
    
    dds.DomainParticipant.finalize_participant_factory()
    


if __name__ == "__main__":
    main()
