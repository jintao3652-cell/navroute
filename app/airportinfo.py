"""Airport / runway / ILS facts.

The route engine only needs runway *thresholds* (``NavCycle.runways``); an EFB
client also wants the physical runway (magnetic / true heading, length, width,
surface, threshold elevation) and the landing aids installed on it (ILS
identifier, frequency, localizer course, glide slope).

Everything here is read straight from the DFD tables — nothing is derived or
guessed:

* ``tbl_pa_airports``              airport name, elevation, magnetic variation …
* ``tbl_pg_runways``               one row per landing direction
* ``tbl_pi_localizers_glideslopes`` localizer + glide slope
* ``tbl_pm_localizer_marker``      OM / MM / IM markers
* ``tbl_d_vhfnavaids``             navaid name + DME collocation of the localizer

A field that the source does not carry stays ``None`` / ``[]`` — a runway with
no ILS simply has ``"ils": []``.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Dict, List, Optional

from .procedures import normalize_runway

FT_PER_M = 0.3048

# ARINC 424 surface codes -> short English word (the code itself is kept too).
SURFACES = {
    "ASPH": "asphalt", "BITU": "bituminous", "CONC": "concrete", "TARM": "tarmac",
    "TRTD": "treated", "PAVD": "paved", "UNPV": "unpaved", "GRAS": "grass",
    "TURF": "turf", "GRVL": "gravel", "DIRT": "dirt", "SAND": "sand",
    "CLAY": "clay", "SOIL": "soil", "SILT": "silt", "CORL": "coral",
    "LATE": "laterite", "MACA": "macadam", "BRCK": "brick", "MATS": "mats",
    "PLNG": "planking", "SELD": "sealed", "SNOW": "snow", "ICE": "ice",
    "WATE": "water", "UNKN": "unknown",
}

_TRAFFIC = {"L": "left", "R": "right", "N": "none"}

# "RW18L" -> "18L"; a helipad / water lane ("NE", "WATE") is kept verbatim.
_NUMERIC = re.compile(r"^\d{1,2}[LCR]?$")


def _f(value) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num


def _r(value, digits: int = 1) -> Optional[float]:
    num = _f(value)
    return None if num is None else round(num, digits)


def _i(value) -> Optional[int]:
    num = _f(value)
    return None if num is None else int(round(num))


def _m(ft) -> Optional[int]:
    num = _f(ft)
    return None if num is None else int(round(num * FT_PER_M))


def _yes(value) -> Optional[bool]:
    if value is None:
        return None
    return str(value).strip().upper() == "Y"


def category_text(raw) -> Optional[str]:
    """``1`` / ``2`` / ``3`` -> ``CAT I`` / ``CAT II`` / ``CAT III``."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if s in ("1", "2", "3"):
        return "CAT " + "I" * int(s)
    return s or None


# --------------------------------------------------------------------- queries

_AIRPORT_SQL = (
    "SELECT airport_name, city, country, ata_iata_code, elevation, "
    "       magnetic_variation, transition_altitude, transition_level, time_zone, "
    "       speed_limit, speed_limit_altitude, ifr_capability, longest_runway_surface_code "
    "FROM tbl_pa_airports WHERE airport_identifier=?"
)

_RUNWAY_SQL = (
    "SELECT runway_identifier, runway_latitude, runway_longitude, "
    "       runway_magnetic_bearing, runway_true_bearing, runway_length, runway_width, "
    "       surface_code, landing_threshold_elevation, displaced_threshold_distance, "
    "       threshold_crossing_height, runway_gradient, traffic_pattern, "
    "       runway_lights, part_time_lights, llz_identifier "
    "FROM tbl_pg_runways WHERE airport_identifier=? ORDER BY runway_identifier"
)

_ILS_SQL = (
    "SELECT runway_identifier, llz_identifier, llz_frequency, llz_bearing, "
    "       llz_truebearing, llz_width, llz_latitude, llz_longitude, llz_position, "
    "       gs_angle, gs_beam_width, gs_elevation, gs_latitude, gs_longitude, "
    "       gs_position, ils_mls_gls_category, station_declination "
    "FROM tbl_pi_localizers_glideslopes WHERE airport_identifier=? "
    "ORDER BY runway_identifier, llz_identifier"
)

_MARKER_SQL = (
    "SELECT runway_identifier, llz_identifier, marker_type, marker_identifier, "
    "       marker_latitude, marker_longitude "
    "FROM tbl_pm_localizer_marker WHERE airport_identifier=?"
)

_NAVAID_SQL = (
    "SELECT navaid_identifier, navaid_name, navaid_frequency, dme_ident "
    "FROM tbl_d_vhfnavaids WHERE airport_identifier=?"
)


def _rows(cyc, sql: str, apt: str) -> List[tuple]:
    try:
        return cyc.conn.execute(sql, (apt,)).fetchall()
    except sqlite3.Error:
        return []


def airport_meta(cyc, apt: str) -> Optional[dict]:
    rows = _rows(cyc, _AIRPORT_SQL, apt)
    if not rows:
        return None
    (name, city, country, iata, elev, variation, trans_alt, trans_lvl,
     tz, speed_limit, speed_alt, ifr, longest_surface) = rows[0]
    return {
        "airport": apt,
        "name": name,
        "city": city,
        "country": country,
        "iata": iata,
        "elevation_ft": _i(elev),
        "magnetic_variation": _r(variation, 2),
        "transition_altitude_ft": _i(trans_alt),
        "transition_level_ft": _i(trans_lvl),
        "time_zone": tz,
        "speed_limit_kt": _i(speed_limit),
        "speed_limit_altitude_ft": _i(speed_alt),
        "ifr": _yes(ifr),
        "longest_runway_surface": longest_surface,
    }


def _ils_entries(cyc, apt: str) -> Dict[str, List[dict]]:
    """ILS / localizer records of one airport, grouped by runway designator.

    Navaid name, DME collocation and the compass locator markers are attached
    here so both the runway view and the flat list carry them.
    """
    by_rwy: Dict[str, List[dict]] = {}
    for row in _rows(cyc, _ILS_SQL, apt):
        (rwy_raw, ident, freq, bearing, truebearing, width, llz_lat, llz_lon,
         llz_pos, gs_angle, gs_width, gs_elev, gs_lat, gs_lon, gs_pos,
         category, declination) = row
        rwy = normalize_runway(rwy_raw)
        item = {
            "runway": rwy,
            "ident": (ident or "").strip() or None,
            "type": "ILS" if _f(gs_angle) is not None else "LOC",
            "category": category_text(category),
            "frequency_mhz": _r(freq, 2),
            "course_mag": _r(bearing, 1),
            "course_true": _r(truebearing, 1),
            "localizer": {
                "lat": _r(llz_lat, 6),
                "lon": _r(llz_lon, 6),
                "width_deg": _r(width, 2),
                "distance_from_threshold_ft": _i(llz_pos),
            },
            "glideslope": {
                "angle_deg": _r(gs_angle, 2),
                "beam_width_deg": _r(gs_width, 2),
                "elevation_ft": _i(gs_elev),
                "lat": _r(gs_lat, 6),
                "lon": _r(gs_lon, 6),
                "distance_from_threshold_ft": _i(gs_pos),
            } if _f(gs_angle) is not None or _f(gs_lat) is not None else None,
            "declination": _r(declination, 2),
            "name": None,
            "dme": None,
            "markers": [],
        }
        by_rwy.setdefault(rwy or "", []).append(item)
    if not by_rwy:
        return by_rwy

    navaids = {}
    for ident, name, freq, dme in _rows(cyc, _NAVAID_SQL, apt):
        navaids[(ident or "").strip().upper()] = (name, _r(freq, 2), bool(dme))
    markers: Dict[tuple, List[dict]] = {}
    for rwy_raw, ident, mtype, mident, mlat, mlon in _rows(cyc, _MARKER_SQL, apt):
        key = (normalize_runway(rwy_raw) or "", (ident or "").strip().upper())
        markers.setdefault(key, []).append({
            "type": (mtype or "").strip() or None,
            "ident": (mident or "").strip() or None,
            "lat": _r(mlat, 6),
            "lon": _r(mlon, 6),
        })

    for rwy, items in by_rwy.items():
        for item in items:
            key = (rwy, (item["ident"] or "").upper())
            nav = navaids.get(key[1])
            if nav:
                item["name"], nav_freq, has_dme = nav
                if item["frequency_mhz"] is None:
                    item["frequency_mhz"] = nav_freq
                item["dme"] = has_dme
            item["markers"] = markers.get(key, [])
    return by_rwy


def runway_list(cyc, apt: str) -> List[dict]:
    """Every landing direction of an airport, with the ILS fitted to it."""
    ils_by_rwy = _ils_entries(cyc, apt)
    out: List[dict] = []
    seen = set()
    for row in _rows(cyc, _RUNWAY_SQL, apt):
        (ident, lat, lon, mag, true, length, width, surface, elev, displaced,
         tch, gradient, pattern, lights, part_lights, llz) = row
        rwy = normalize_runway(ident)
        if not rwy or rwy in seen:
            continue
        seen.add(rwy)
        aids = ils_by_rwy.pop(rwy, [])
        for item in aids:
            item["designator"] = rwy
        out.append({
            "designator": rwy,
            "ident": (ident or "").strip() or None,
            "kind": "runway" if _NUMERIC.match(rwy) else "other",
            "lat": _r(lat, 8),
            "lon": _r(lon, 8),
            "heading_mag": _r(mag, 1),
            "heading_true": _r(true, 1),
            "length_ft": _i(length),
            "length_m": _m(length),
            "width_ft": _i(width),
            "width_m": _m(width),
            "surface": (surface or "").strip() or None,
            "surface_text": SURFACES.get((surface or "").strip().upper()),
            "elevation_ft": _i(elev),
            "displaced_threshold_ft": _i(displaced),
            "threshold_crossing_height_ft": _i(tch),
            "gradient_pct": _r(gradient, 3),
            "traffic_pattern": (pattern or "").strip() or None,
            "traffic_pattern_text": _TRAFFIC.get((pattern or "").strip().upper()),
            "lights": _yes(lights),
            "part_time_lights": _yes(part_lights),
            "llz_ident": (llz or "").strip() or None,
            "ils": aids,
        })
    # an ILS whose runway row is missing must not disappear
    for rwy, aids in ils_by_rwy.items():
        if not aids:
            continue
        for item in aids:
            item["designator"] = rwy or None
        out.append({
            "designator": rwy or None,
            "ident": None,
            "kind": "unknown",
            "lat": None, "lon": None,
            "heading_mag": None, "heading_true": None,
            "length_ft": None, "length_m": None,
            "width_ft": None, "width_m": None,
            "surface": None, "surface_text": None,
            "elevation_ft": None,
            "displaced_threshold_ft": None,
            "threshold_crossing_height_ft": None,
            "gradient_pct": None,
            "traffic_pattern": None, "traffic_pattern_text": None,
            "lights": None, "part_time_lights": None,
            "llz_ident": None,
            "ils": aids,
        })
    return out


def build(cyc, apt: str) -> dict:
    """Everything an EFB needs to know about one airport's runways."""
    runways = runway_list(cyc, apt)
    flat: List[dict] = []
    with_ils = 0
    for rwy in runways:
        aids = rwy.get("ils") or []
        if aids:
            with_ils += 1
        for item in aids:
            item["designator"] = rwy.get("designator")
            flat.append(item)
    meta = airport_meta(cyc, apt) or {"airport": apt}
    data = dict(meta)
    data["runways"] = runways
    data["ils"] = flat
    data["counts"] = {
        "runways": len([r for r in runways if r.get("kind") == "runway"]),
        "landing_directions": len(runways),
        "runways_with_ils": with_ils,
        "ils": len(flat),
    }
    return data


def compact(rwy: dict) -> dict:
    """One runway without the ILS body — for embedding in other responses."""
    return {
        "designator": rwy.get("designator"),
        "lat": rwy.get("lat"),
        "lon": rwy.get("lon"),
        "heading_mag": rwy.get("heading_mag"),
        "heading_true": rwy.get("heading_true"),
        "length_ft": rwy.get("length_ft"),
        "length_m": rwy.get("length_m"),
        "width_ft": rwy.get("width_ft"),
        "surface": rwy.get("surface"),
        "elevation_ft": rwy.get("elevation_ft"),
        "ils": [i.get("ident") for i in (rwy.get("ils") or [])],
        "ils_frequency": [i.get("frequency_mhz") for i in (rwy.get("ils") or [])],
    }
