"""ARINC 424 path terminators (航段路径终止符).

The 23 leg types of ARINC 424-18..23 are grouped by what they need in order to
be drawn:

* **position legs** – the record carries the terminating fix, so the geometry is
  a straight line to it: ``IF`` ``TF`` ``CF`` ``DF`` plus the "from a fix"
  variants ``FC`` ``FD`` ``FA`` ``FM`` that start at a published fix.
* **arc legs** – ``RF`` (fixed radius, RNP AR) and ``AF`` (DME arc).  Their end
  fix is published as well, but the flown path is a circular arc, so plotting
  fixes as a polyline gives a chord that can be several miles off.
* **hold legs** – ``HM`` ``HF`` ``HA`` are a racetrack over the fix.
* **course reversal** – ``PI`` (procedure turn).
* **open legs** – ``CA`` ``VA`` ``CI`` ``VI`` ``CD`` ``VD`` ``CR`` ``VR``
  terminate at an altitude, an intercept or a DME distance that is not a fix.
  In real SID/STAR data these rows carry no coordinates at all (they are the
  radar-vector part of the procedure), so they are reported as *open*
  segments instead of being invented.

``build_track`` turns an ordered leg list into a densified polyline; every
leg type is accepted and unknown terminators degrade to a straight line.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

from . import geo

Point = Tuple[float, float]

# terminator -> (english name, chinese name)
LEG_TYPES = {
    "IF": ("Initial fix", "起始定位点"),
    "TF": ("Track to fix", "航迹至定位点"),
    "CF": ("Course to fix", "航向至定位点"),
    "DF": ("Direct to fix", "直飞定位点"),
    "RF": ("Radius to fix", "定半径转弯至定位点"),
    "AF": ("Arc to fix", "DME 圆弧至定位点"),
    "CA": ("Course to altitude", "航向至高度"),
    "VA": ("Heading to altitude", "航向至高度"),
    "CD": ("Course to DME distance", "航向至 DME 距离"),
    "VD": ("Heading to DME distance", "航向至 DME 距离"),
    "CI": ("Course to intercept", "航向至截获"),
    "VI": ("Heading to intercept", "航向至截获"),
    "CR": ("Course to radial", "航向至径向线"),
    "VR": ("Heading to radial", "航向至径向线"),
    "FC": ("Track from fix to distance", "定位点至距离"),
    "FD": ("Course from fix to DME", "定位点至 DME 距离"),
    "FA": ("Course from fix to altitude", "定位点至高度"),
    "FM": ("Course from fix to manual", "定位点至人工终止"),
    "VM": ("Heading to manual", "航向至人工终止"),
    "HM": ("Hold to manual", "等待至人工终止"),
    "HF": ("Hold to fix", "等待至定位点"),
    "HA": ("Hold to altitude", "等待至高度"),
    "PI": ("Procedure turn", "程序转弯"),
}

ARC_TYPES = frozenset({"RF", "AF"})
HOLD_TYPES = frozenset({"HM", "HF", "HA"})
TURN_ARC_TYPES = ARC_TYPES | frozenset({"PI"})
# Legs that end somewhere that is not a published fix (altitude, intercept,
# DME distance, manual).  Only these may legally appear without coordinates.
OPEN_TYPES = frozenset({"CA", "VA", "CI", "VI", "CD", "VD", "CR", "VR", "FA", "FM", "VM"})

STRAIGHT_TYPES = frozenset(
    {"IF", "TF", "CF", "DF", "FC", "FD"} | OPEN_TYPES | HOLD_TYPES
)

# Drawing parameters.  These only affect the sample density of curves, never
# the published points.
ARC_STEP_NM = 1.0          # one sample per nautical mile of arc
ARC_MAX_POINTS = 240       # safety cap for a full circle
TURN_RADIUS_NM = 1.5       # bank-limited turn radius at ~200 kt
HOLD_LEG_NM = 4.0          # default racetrack leg (1 min at 240 kt)
HOLD_SPEED_KT = 240.0
PI_OUTBOUND_NM = 3.0       # drawn length of a procedure-turn outbound leg


def name_of(path: str) -> str:
    return LEG_TYPES.get((path or "").upper(), ("Unknown", "未知航段"))[0]


def is_known(path: str) -> bool:
    return (path or "").upper() in LEG_TYPES


def is_open(leg) -> bool:
    """True when the leg has no terminating fix (a vector / manual leg)."""
    return leg.lat is None or leg.lon is None


# ------------------------------------------------------------------ primitives


def _arc_points(center: Point, radius: float, start_brg: float,
                delta_deg: float, step_nm: float = ARC_STEP_NM) -> List[Point]:
    """Sample a circular arc around ``center``."""
    if radius <= 0.0 or abs(delta_deg) < 0.05:
        return []
    length = abs(math.radians(delta_deg)) * radius
    steps = int(min(ARC_MAX_POINTS, max(2, math.ceil(length / max(0.1, step_nm)))))
    out: List[Point] = []
    for i in range(1, steps + 1):
        out.append(geo.destination(center, start_brg + delta_deg * i / steps, radius))
    return out


def _turn_delta(turn: str, a0: float, a1: float) -> float:
    """Signed sweep from bearing ``a0`` to ``a1`` for a left/right turn."""
    turn = (turn or "").upper()[:1]
    if turn == "R":
        return (a1 - a0) % 360.0
    if turn == "L":
        return -((a0 - a1) % 360.0)
    return geo.angle_delta(a0, a1)


def arc_between(start: Point, end: Point, center: Optional[Point] = None,
                radius: Optional[float] = None, turn: str = "") -> List[Point]:
    """Arc from ``start`` to ``end``; empty list when the data disagrees.

    ``RF`` publishes the centre and the radius, ``AF`` the DME station and the
    ``rho`` distance.  Both are used when available; otherwise a centre is
    derived from the chord so that even incomplete records stay curved.
    """
    if center is None:
        chord = geo.dist(start, end)
        if chord < 0.2:
            return []
        radius = radius or chord / 2.0
        if radius < chord / 2.0:
            radius = chord / 2.0
        # centre = midpoint offset perpendicular to the chord
        mid = (
            (start[0] + end[0]) / 2.0,
            (start[1] + end[1]) / 2.0,
        )
        offset = math.sqrt(max(0.0, radius * radius - (chord / 2.0) ** 2))
        candidates = [
            geo.destination(mid, (geo.bearing(start, end) + 90.0) % 360.0, offset),
            geo.destination(mid, (geo.bearing(start, end) - 90.0) % 360.0, offset),
        ]
        center = min(candidates, key=lambda c: abs(geo.dist(c, start) - radius))
    r_start = geo.dist(center, start)
    r_end = geo.dist(center, end)
    if radius is None or radius <= 0.0:
        radius = (r_start + r_end) / 2.0
    if r_start < 1.0:
        # the previous leg ends at the station itself, so there is no join
        # bearing on the circle: a radial line to the arc start is drawn
        return []
    # The end fix must sit on the arc; if it does not, the centre/radius pair
    # is wrong (mixed coding standards) and a straight line is safer.
    tolerance = max(3.0, radius * 0.35)
    if abs(r_end - radius) > tolerance:
        return []
    a0 = geo.bearing(center, start)
    a1 = geo.bearing(center, end)
    if abs(r_start - radius) > tolerance:
        # The previous leg ends before the aircraft joins the arc (typical for
        # a DME arc entered after an intercept): start the arc at the join
        # point, which is what the chart shows.
        start = geo.destination(center, a0, radius)
    delta = _turn_delta(turn, a0, a1)
    if abs(delta) < 0.05 or abs(delta) > 350.0:
        return []
    return _arc_points(center, radius, a0, delta)


def racetrack(fix: Point, course: float, turn: str = "R",
              leg_nm: float = HOLD_LEG_NM,
              radius: float = TURN_RADIUS_NM) -> List[Point]:
    """Hold pattern: outbound leg, 180° turn, inbound leg, 180° turn back."""
    course = (course or 0.0) % 360.0
    leg_nm = max(0.5, leg_nm or HOLD_LEG_NM)
    side = 1.0 if (turn or "R").upper().startswith("R") else -1.0
    sweep = 180.0 if side > 0 else -180.0

    out_end = geo.destination(fix, course, leg_nm)
    points: List[Point] = [out_end]
    # first 180° turn: centre abeam the outbound end
    centre1 = geo.destination(out_end, course + 90.0 * side, radius)
    points.extend(_arc_points(centre1, radius, geo.bearing(centre1, out_end), sweep))

    # inbound leg, then the second 180° turn back onto the fix
    inbound_start = points[-1]
    inbound_end = geo.destination(inbound_start, course + 180.0, leg_nm)
    points.append(inbound_end)
    centre2 = geo.destination(inbound_end, course + 180.0 + 90.0 * side, radius)
    points.extend(_arc_points(centre2, radius, geo.bearing(centre2, inbound_end), sweep))
    points.append(fix)
    return points


def procedure_turn(fix: Point, course: float, turn: str = "R",
                   out_nm: float = PI_OUTBOUND_NM,
                   radius: float = TURN_RADIUS_NM) -> List[Point]:
    """Simplified procedure turn: outbound, 180° reversal, inbound."""
    leg_nm = max(out_nm, math.pi * radius + 0.5)
    return racetrack(fix, course, turn, leg_nm, radius)


# ---------------------------------------------------------------------- track


def build_track(legs: Sequence, step_nm: float = ARC_STEP_NM) -> List[Point]:
    """Densified polyline for an ordered leg list.

    Open legs (no fix) contribute nothing; every other terminator contributes
    at least its terminating fix, and the curved ones add intermediate
    samples so the drawn path is not a chord.
    """
    track: List[Point] = []

    def push(pt: Point) -> None:
        if track:
            last = track[-1]
            if abs(last[0] - pt[0]) < 1e-9 and abs(last[1] - pt[1]) < 1e-9:
                return
        track.append(pt)

    for leg in legs:
        if is_open(leg):
            continue
        end = (float(leg.lat), float(leg.lon))
        path = (getattr(leg, "path", "") or "").upper()
        prev = track[-1] if track else None

        if path in ARC_TYPES and prev is not None:
            arc = arc_between(prev, end, _arc_center(leg), _arc_radius(leg),
                              getattr(leg, "turn", "") or "")
            if arc:
                for pt in arc:
                    push(pt)
                push(end)          # the published fix closes the arc exactly
                continue
        elif path in HOLD_TYPES and prev is not None:
            push(end)
            for pt in _hold_points(end, leg):
                push(pt)
            continue
        elif path == "PI" and prev is not None:
            push(end)
            for pt in _pi_points(end, leg):
                push(pt)
            continue

        push(end)
    return track


def _arc_center(leg) -> Optional[Point]:
    center = getattr(leg, "center", None)
    if center:
        return (float(center[0]), float(center[1]))
    navaid = getattr(leg, "navaid", None)
    if navaid:
        return (float(navaid[0]), float(navaid[1]))
    return None


def _arc_radius(leg) -> Optional[float]:
    radius = getattr(leg, "radius", None)
    if radius:
        return float(radius)
    rho = getattr(leg, "rho", None)
    if rho:
        return float(rho)
    return None


def _hold_points(fix: Point, leg) -> List[Point]:
    course = getattr(leg, "course", None)
    if course is None:
        return []
    length = getattr(leg, "dist", None)
    kind = (getattr(leg, "dist_kind", "") or "").upper()
    if length:
        leg_nm = float(length) / 60.0 * HOLD_SPEED_KT if kind == "T" else float(length)
    else:
        leg_nm = HOLD_LEG_NM
    return racetrack(fix, float(course), getattr(leg, "turn", "") or "R", leg_nm)


def _pi_points(fix: Point, leg) -> List[Point]:
    course = getattr(leg, "course", None)
    if course is None:
        return []
    return procedure_turn(fix, float(course), getattr(leg, "turn", "") or "R")


def summarize(legs: Iterable) -> List[str]:
    """Sorted list of the terminators actually used by a leg sequence."""
    return sorted({(getattr(l, "path", "") or "").upper() for l in legs} - {""})
