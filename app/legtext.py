"""Human readable description of one ARINC 424 leg.

The database row already carries everything a navigator needs — coded course,
arc centre and radius, turn direction, altitude/speed constraints, vertical
angle and RNP — but a raw ``path_terminator`` plus a few numbers does not tell
a reader *how the leg is flown*.  This module turns a :class:`Leg` sequence into
one sentence per leg ("以 AK966 为圆心、半径 12.3 NM 右转 87° 圆弧至 AK622")
plus the structured values the UI shows in its own columns.

Everything here is pure formatting over published data: no geometry is invented.
A value the coding does not carry is left empty instead of being guessed.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from . import geo
from . import legs as leggeom

# Coded reference of a course / heading: magnetic or true north.
COURSE_REF = {"M": "M", "T": "T"}

# Altitude description codes that only mean "cross at the published value".
# ``G``/``H``/``I``/``J`` are DFD extensions that always carry a single value in
# this dataset, so they are shown as a plain altitude with the code attached
# instead of being interpreted as an at/above/below constraint.
ALT_CODE_TEXT = {
    "+": "at or above",
    "-": "at or below",
    "@": "at",
    "B": "between",
    "G": "coded G",
    "H": "coded H",
    "I": "coded I",
    "J": "coded J",
}

SPEED_CODE_TEXT = {
    "+": "at or above",
    "-": "at or below",
    "@": "at",
}


def _num(value, digits: int = 0) -> str:
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits <= 0:
        return str(int(round(num)))
    return f"{num:.{digits}f}".rstrip("0").rstrip(".")


def path_cn(path: str) -> str:
    """Chinese name of a path terminator (runway anchor has no terminator)."""
    if not path:
        return "跑道入口"
    return leggeom.LEG_TYPES.get(path, ("", ""))[1]


def altitude_text(leg) -> str:
    """Altitude constraint as text (``≥ 17700 ft`` / ``11000 – 6500 ft``)."""
    if leg.alt1 is None:
        return ""
    desc = (leg.alt_desc or "").upper()
    a1 = _num(leg.alt1)
    a2 = _num(leg.alt2) if leg.alt2 is not None else ""
    if desc == "+":
        return f"≥ {a1} ft"
    if desc == "-":
        return f"≤ {a1} ft"
    if desc == "@":
        return f"{a1} ft"
    if desc == "B" and a2:
        return f"{a1} – {a2} ft"
    if a2 and a1 != a2:
        return f"{a1} – {a2} ft"
    return f"{a1} ft"


def speed_text(leg) -> str:
    """Speed constraint as text (``≤ 210 kt``)."""
    if leg.speed is None:
        return ""
    desc = (leg.speed_desc or "").upper()
    value = _num(leg.speed)
    if desc == "+":
        return f"≥ {value} kt"
    if desc == "-":
        return f"≤ {value} kt"
    if desc == "@":
        return f"= {value} kt"
    return f"{value} kt"


def course_text(leg) -> str:
    """Coded course/heading with its reference, e.g. ``253°M``."""
    if leg.course is None:
        return ""
    ref = COURSE_REF.get((leg.course_flag or "").upper(), "")
    return f"{_num(leg.course)}°{ref}"


def _turn_word(turn: str) -> str:
    turn = (turn or "").upper()
    if turn == "L":
        return "左转"
    if turn == "R":
        return "右转"
    return ""


def arc_params(leg, prev) -> Optional[Dict[str, object]]:
    """Arc centre / radius / sweep of an ``RF`` or ``AF`` leg.

    ``RF`` codes the centre explicitly; ``AF`` uses the reference navaid and the
    DME distance as centre and radius.  The sweep is measured from the previous
    fix so the reader can see how far the arc actually turns.
    """
    if leg.path not in leggeom.ARC_TYPES:
        return None
    if leg.path == "RF":
        center = leg.center
        radius = leg.radius
        cname = leg.center_name or ""
    else:
        center = leg.navaid
        radius = leg.rho
        cname = leg.navaid_name or ""
    if center is None or radius is None:
        return None

    sweep = None
    if prev is not None and prev.lat is not None and leg.lat is not None:
        a0 = geo.bearing(center, (prev.lat, prev.lon))
        a1 = geo.bearing(center, (leg.lat, leg.lon))
        delta = geo.angle_delta(a0, a1)
        turn = (leg.turn or "").upper()
        if turn == "R" and delta < 0:
            delta += 360.0
        elif turn == "L" and delta > 0:
            delta -= 360.0
        elif turn not in ("L", "R"):
            delta = abs(delta)
        sweep = delta
    return {
        "center_name": cname,
        "center_lat": round(float(center[0]), 8),
        "center_lon": round(float(center[1]), 8),
        "radius_nm": round(float(radius), 3),
        "sweep_deg": None if sweep is None else round(sweep, 1),
        "turn": (leg.turn or "").upper(),
        "kind": leg.path,
    }


def _hold_length(leg) -> str:
    """Hold leg length: ``4 NM`` or ``1 min`` as coded."""
    if leg.dist is None:
        return ""
    if (leg.dist_kind or "").upper() == "T":
        return f"{_num(leg.dist, 1)} min"
    return f"{_num(leg.dist, 1)} NM"


def _length_nm(prev, leg) -> Optional[float]:
    if prev is None or prev.lat is None or leg.lat is None:
        return None
    d = geo.dist((prev.lat, prev.lon), (leg.lat, leg.lon))
    return round(d, 1)


def _track_text(prev, leg) -> str:
    """True track between two fixes, labelled ``T`` like the coded courses."""
    if prev is None or prev.lat is None or leg.lat is None:
        return ""
    return f"{_num(geo.bearing((prev.lat, prev.lon), (leg.lat, leg.lon)))}°T"


def phrase(leg, prev, arc: Optional[dict], alt: str) -> str:
    """One sentence describing how this leg is flown."""
    name = leg.name or leg.path
    course = course_text(leg)
    path = leg.path
    if not path:
        # the runway threshold added from tbl_pg_runways has no ARINC leg
        return f"跑道入口 {name}（按跑道数据补入，非 ARINC 航段）"
    # rho == 0 is a degenerate coding: the DME arc collapses onto the station
    rho_val = leg.rho if (leg.rho is not None and leg.rho > 0) else None
    rho = f"DME {_num(rho_val, 1)} NM" if rho_val is not None else "DME 弧（距离未编码）"
    theta = f"R-{_num(leg.theta)}" if leg.theta is not None else ""
    length = _length_nm(prev, leg)
    length_text = f"{_num(length, 1)} NM" if length is not None else ""
    alt_text = alt or ""

    if path == "IF":
        if prev is None:
            return "起始定位点，从这里开始按程序飞行"
        return f"定位点 {name}：作为下一航段起点，本身不画航迹"
    if path == "TF":
        # a TF leg has no coded course: the track follows the two fixes
        track = course or _track_text(prev, leg)
        base = f"沿 {track} 航迹飞至 {name}" if track else f"航迹飞至 {name}"
        return f"{base}，{length_text}" if length_text else base
    if path == "CF":
        base = f"沿 {course} 航向飞至 {name}" if course else f"航向飞至 {name}"
        return f"{base}，{length_text}" if length_text else base
    if path == "DF":
        return f"从当前位置直飞 {name}" + (f"，{length_text}" if length_text else "")
    if path == "RF":
        if arc is None:
            return f"定半径圆弧至 {name}（源数据未给圆心或半径）"
        cname = arc["center_name"] or "编码圆心"
        turn = _turn_word(arc["turn"])
        sweep = arc["sweep_deg"]
        turn_text = f"{turn} {_num(abs(sweep), 0)}°" if sweep is not None else (turn or "转向未编码")
        return (f"以 {cname} 为圆心、半径 {_num(arc['radius_nm'], 1)} NM，"
                f"{turn_text}画弧至 {name}")
    if path == "AF":
        if arc is None:
            return f"DME 圆弧至 {name}"
        cname = arc["center_name"] or "DME 台"
        turn = _turn_word(arc["turn"])
        sweep = arc["sweep_deg"]
        turn_text = f"{turn} {_num(abs(sweep), 0)}°" if sweep is not None else (turn or "")
        tail = f"，{turn_text}" if turn_text else ""
        return (f"以 {cname} 为圆心（DME {_num(arc['radius_nm'], 1)} NM）"
                f"{tail}画弧至 {name}")
    if path in ("CA", "VA"):
        verb = "航向" if path == "VA" else "航迹"
        return f"沿 {course} {verb}爬升至 {alt_text}" if course else f"爬升至 {alt_text}"
    if path in ("CD", "VD"):
        return f"沿 {course} 飞至 {rho}" if course else f"飞至 {rho}"
    if path in ("CI", "VI"):
        return f"沿 {course} 飞行，截获下一航段" if course else "飞行至截获下一航段"
    if path in ("CR", "VR"):
        return f"沿 {course} 飞行，截获 {theta} 径向线" if course else f"截获 {theta} 径向线"
    if path == "FC":
        d = _hold_length(leg)
        return f"从 {name} 沿 {course} 继续飞 {d}".strip() if course else f"从 {name} 继续飞 {d}"
    if path == "FD":
        return f"从 {name} 沿 {course} 飞至 {rho}" if course else f"从 {name} 飞至 {rho}"
    if path == "FA":
        return f"从 {name} 沿 {course} 爬升至 {alt_text}" if course else f"从 {name} 爬升至 {alt_text}"
    if path == "FM":
        return f"从 {name} 沿 {course} 飞行（人工终止）" if course else f"从 {name} 飞行（人工终止）"
    if path == "VM":
        return f"沿 {course} 航向飞行（人工终止）" if course else "按航向飞行（人工终止）"
    if path in leggeom.HOLD_TYPES:
        turn = _turn_word(leg.turn) or "转弯"
        leg_text = _hold_length(leg)
        tail = f"，出航 {leg_text}" if leg_text else ""
        exit_text = {"HM": "人工终止", "HF": f"回到 {name}", "HA": "至高度"}.get(path, "")
        return f"在 {name} 等待，{turn}{tail}" + (f"，{exit_text}退出" if exit_text else "")
    if path == "PI":
        return f"程序转弯：沿 {course} 出航后反向切入" if course else "程序转弯后反向切入"
    return f"至 {name}"


def _fmt_num(value) -> str:
    """Compact number for display (``12`` / ``12.3``, never ``12.0``)."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(num)) if num.is_integer() else f"{num:g}"


def dme_text(leg) -> str:
    """``ABC 12.5 NM`` – the DME distance of a leg, with its station."""
    if leg.rho is None:
        return ""
    ref = f"{leg.navaid_name} " if leg.navaid_name else ""
    return f"{ref}{_fmt_num(leg.rho)} NM"


def describe(legs: Sequence) -> List[Dict[str, object]]:
    """Per-leg readable description, aligned with ``legs``."""
    out: List[Dict[str, object]] = []
    prev = None
    for leg in legs:
        arc = arc_params(leg, prev)
        alt = altitude_text(leg)
        speed = speed_text(leg)
        item = {
            "instruction": phrase(leg, prev, arc, alt),
            "arc": arc,
            "altitude": alt,
            "altitude_code": (leg.alt_desc or "").upper(),
            "speed": speed,
            "speed_code": (leg.speed_desc or "").upper(),
            "vertical": None if leg.vangle is None else round(float(leg.vangle), 2),
            "rnp": None if leg.rnp is None else round(float(leg.rnp), 2),
            "length_nm": _length_nm(prev, leg),
            "course_text": course_text(leg),
        }
        out.append(item)
        if leg.lat is not None:
            prev = leg
    return out


def segments(legs: Sequence) -> List[Dict[str, object]]:
    """JSON-ready rows of the ARINC 424 leg table, aligned with ``legs``.

    One row answers everything a reader needs about a leg: which terminator it
    is, which fix it ends on, *how* it is flown (``instruction``, and for an arc
    its centre / radius / turn / sweep), the altitude & speed constraints, the
    leg length and the position.

    This is the single source of truth for that table — the console, the
    procedure endpoint, ``parseRoute`` and ``route`` all serialise through
    here, so an EFB client gets exactly the same fields no matter which call
    it made.
    """
    out: List[Dict[str, object]] = []
    for leg, txt in zip(legs, describe(legs)):
        out.append({
            "path": leg.path,
            "path_name": leggeom.name_of(leg.path),
            "path_cn": path_cn(leg.path),
            "name": leg.name,
            "lat": None if leg.open else round(leg.lat, 8),
            "lon": None if leg.open else round(leg.lon, 8),
            "role": leg.role,
            "transition": leg.transition,
            "open": leg.open,
            "course": leg.course,
            "course_text": txt["course_text"],
            "course_ref": (leg.course_flag or "").upper(),
            "turn": leg.turn,
            "altitude": txt["altitude"],
            "altitude_code": txt["altitude_code"],
            "speed": txt["speed"],
            "speed_code": txt["speed_code"],
            "vertical": txt["vertical"],
            "rnp": txt["rnp"],
            "length_nm": txt["length_nm"],
            "dme": dme_text(leg),
            "rho": leg.rho,
            "theta": leg.theta,
            "arc": txt["arc"],
            "instruction": txt["instruction"],
        })
    return out


def track_points(legs: Sequence, digits: int = 6) -> List[List[float]]:
    """Densified ``[[lat, lon], ...]`` polyline for the given legs."""
    return track_points_from(leggeom.build_track(legs), digits)


def track_points_from(points: Sequence, digits: int = 6) -> List[List[float]]:
    """``[(lat, lon), ...]`` -> the rounded list a client draws with."""
    return [[round(lat, digits), round(lon, digits)] for lat, lon in points]
