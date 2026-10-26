"""Route string -> geographic points (反向解析).

The parser is deliberately conservative: every token is classified against the
index of the selected cycle, and anything that cannot be resolved is reported
through ``warnings`` instead of being invented.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from . import geo
from . import legtext
from .index import NavCycle

KEYWORDS = {"DCT", "SID", "STAR", "IAP", "DIRECT", "APCH", "APP", "RNAV", "TRANS"}

RUNWAY_RE = re.compile(r"^(?:RW)?(\d{1,2}[LCR]?)$")
LATLON_PATTERNS = (
    re.compile(r"^(\d{2})(\d{2})([NS])(\d{3})(\d{2})([EW])$"),
    re.compile(r"^([NS])(\d{2})(\d{2})([EW])(\d{3})(\d{2})$"),
)

SPLIT_RE = re.compile(r"[\s,;>|\-]+")

IDENT_RE = re.compile(r"[^A-Z0-9]")

# ICAO flight-plan speed/level annotation, the form SimBrief appends to a
# waypoint when the aircraft crosses metric-altitude airspace:
#
#     VIDEK/K0919S1130   -> 919 km/h, metric level 11300 m
#     NIGOR/K0887F380    -> 887 km/h, FL380
#     ORTUS/M080F370     -> Mach .80, FL370
#     VIDEK/FL380        -> FL380 only
#
# Speed letter: K = km/h, N = knots, M = Mach.
# Level letter: F = flight level, A = altitude (100 ft), S = metric, V = VFR,
# or the spelled-out FL prefix.
# These carry no position, so they are stripped before tokenising instead of
# being reported as unrecognised route elements.
_SPD = r"[KMN]\d{3,4}"
_LVL = r"(?:FL\d{2,3}|[FASV]\d{2,4})"
ANN_SLASH_RE = re.compile(r"/\s*(?:" + _SPD + r")?" + _LVL + r"(?![0-9A-Z])")
ANN_TOKEN_RE = re.compile(r"^" + _SPD + _LVL + r"$")

# A fix that is not on the airway may still be joined from a chain end.
JOIN_RADIUS_NM = 300.0


def tokenize(route: str) -> List[str]:
    s = (route or "").upper().strip()
    if not s:
        return []
    s = s.replace("...", " ").replace("..", " ").replace("→", " ").replace("–", " ")
    # VIDEK/K0919S1130 -> VIDEK (the annotation is not a waypoint)
    s = ANN_SLASH_RE.sub("", s)
    s = s.replace("/", " / ")
    return [t for t in SPLIT_RE.split(s) if t and not ANN_TOKEN_RE.match(t)]


def _key_eq(a, b) -> bool:
    """Identifier comparison tolerant of padding and numeric suffixes."""
    x = IDENT_RE.sub("", (a or "").upper())
    y = IDENT_RE.sub("", (b or "").upper())
    if not x or not y:
        return False
    return x == y or x.endswith(y) or y.endswith(x)


def _latlon(token: str) -> Optional[Tuple[float, float]]:
    for pattern in LATLON_PATTERNS:
        m = pattern.match(token)
        if not m:
            continue
        g = m.groups()
        if g[0] in ("N", "S"):
            lat = float(g[1]) + float(g[2]) / 60.0
            lon = float(g[4]) + float(g[5]) / 60.0
            lat = -lat if g[0] == "S" else lat
            lon = -lon if g[3] == "W" else lon
        else:
            lat = float(g[0]) + float(g[1]) / 60.0
            lon = float(g[3]) + float(g[4]) / 60.0
            lat = -lat if g[2] == "S" else lat
            lon = -lon if g[5] == "W" else lon
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return (lat, lon)
    return None


def _append(points: List[Tuple[str, float, float]], name: str, lat: float, lon: float) -> None:
    if points:
        last = points[-1]
        if last[0] == name and abs(last[1] - lat) < 1e-6 and abs(last[2] - lon) < 1e-6:
            return
        if abs(last[1] - lat) < 1e-9 and abs(last[2] - lon) < 1e-9:
            return
    points.append((name, lat, lon))


def _segment(chain, pi: int, pj: int, include_start: bool = False):
    fixes, lats, lons = chain.fixes, chain.lats, chain.lons
    if pi < pj:
        start = pi if include_start else pi + 1
        return [(fixes[k], lats[k], lons[k]) for k in range(start, pj + 1)]
    start = pi if include_start else pi - 1
    return [(fixes[k], lats[k], lons[k]) for k in range(start, pj - 1, -1)]


def _direction_ok(chain, pi: int, pj: int) -> bool:
    dirs = chain.dirs
    if pi < pj:
        return not any(dirs[k] == "B" for k in range(pi + 1, pj + 1))
    return not any(dirs[k] == "F" for k in range(pj + 1, pi + 1))


def _path_length(seg) -> float:
    total = 0.0
    for a, b in zip(seg, seg[1:]):
        total += geo.haversine(a[1], a[2], b[1], b[2])
    return total


def _peek_fix(cyc: NavCycle, tokens: List[str], start: int, end: int, near) -> Optional[str]:
    for k in range(start, end):
        tok = tokens[k]
        if not tok or tok == "/" or tok in KEYWORDS:
            continue
        if RUNWAY_RE.match(tok) and not cyc.is_airway(tok):
            continue
        if cyc.is_airway(tok):
            return None
        if cyc.airport(tok) or cyc.resolve_point(tok, near) is not None:
            return tok
    return None


def _best_candidate(cyc, awy, cur_pt, entry_name, exit_cands, chains, pos):
    """Find the cheapest way to fly ``awy`` from ``cur_pt`` to the exit fix.

    The score prefers (1) the closest entry point, (2) the segment with the most
    published intermediate fixes, (3) the shortest geometric path — so a route
    that is coded twice (coarse + detailed) resolves to the detailed one.
    """
    entry_cands = pos.get(entry_name, ())
    best = None
    restricted = None

    def consider(score, chain, pi, pj, seg, join_fix=None):
        nonlocal best, restricted
        if _direction_ok(chain, pi, pj):
            if best is None or score < best["score"]:
                best = {"score": score, "chain": chain, "pi": pi, "pj": pj,
                        "seg": seg, "join": join_fix}
        elif restricted is None or score < restricted["score"]:
            restricted = {"score": score, "chain": chain, "pi": pi, "pj": pj,
                          "seg": seg, "join": join_fix}

    for (ci, pi) in entry_cands:
        chain = chains[ci]
        base = geo.dist(cur_pt, (chain.lats[pi], chain.lons[pi]))
        for (cj, pj) in exit_cands:
            if cj != ci or pj == pi:
                continue
            seg = _segment(chain, pi, pj)
            if seg:
                consider((round(base, 1), -len(seg), _path_length(seg)), chain, pi, pj, seg)

    # The entry fix is not on the airway (e.g. ``ZBAA A461 ...``):
    # join at the nearest chain end and warn.
    if best is None and not entry_cands:
        for ci, chain in enumerate(chains):
            for start in (0, len(chain.fixes) - 1):
                d = geo.dist(cur_pt, (chain.lats[start], chain.lons[start]))
                if d > JOIN_RADIUS_NM:
                    continue
                for (cj, pj) in exit_cands:
                    if cj != ci or pj == start:
                        continue
                    seg = _segment(chain, start, pj, include_start=True)
                    if seg:
                        consider((round(d, 1), -len(seg), _path_length(seg)), chain, start, pj,
                                 seg, join_fix=chain.fixes[start])

    if best is None and restricted is not None:
        return restricted, "direction"
    if best is not None and best["join"]:
        return best, "join"
    return best, None


def _take_airway(cyc, awy, cur, tokens, i, n, pts, warnings):
    """Consume ``awy`` plus its exit fix.

    Returns ``(next_index, new_cur, handled)``.  ``handled`` is False only when
    the token should be re-examined as a plain fix.
    """
    if cur is None:
        warnings.append(f"航路 {awy} 前缺少进入点，已忽略")
        return i + 1, None, True

    chains = cyc.airways.get(awy)
    if not chains:
        warnings.append(f"航路 {awy} 在周期 {cyc.cycle} 中不存在")
        return i + 1, None, True
    pos = cyc.aw_pos.get(awy, {})

    # --- look ahead for the exit fix (skip self-references) ------------
    j = i + 1
    exit_name = None
    while j < n:
        tok = tokens[j]
        if not tok or tok == "/" or tok in KEYWORDS:
            j += 1
            continue
        if RUNWAY_RE.match(tok) and not cyc.is_airway(tok):
            j += 1
            continue
        if cyc.is_airway(tok):
            break
        if tok == cur[0]:
            j += 1
            continue
        # a fix published on this airway is a valid exit even when it is not
        # present in the standalone waypoint tables
        if tok in pos or cyc.airport(tok) or cyc.resolve_point(tok, (cur[1], cur[2])) is not None:
            exit_name = tok
            break
        j += 1

    if exit_name is None:
        warnings.append(f"航路 {awy} 缺少退出点，已忽略")
        return i + 1, None, True

    exit_cands = pos.get(exit_name, ())
    if not exit_cands:
        warnings.append(f"{exit_name} 不在航路 {awy} 上，该段按直飞处理")
        return i + 1, None, True

    best, note = _best_candidate(cyc, awy, (cur[1], cur[2]), cur[0], exit_cands, chains, pos)
    if best is None:
        warnings.append(f"航路 {awy} 上没有从 {cur[0]} 到 {exit_name} 的连续航段，已忽略")
        return i + 1, None, True
    if note == "direction":
        warnings.append(f"航路 {awy} 的 {cur[0]}→{exit_name} 段带单向限制标注，已按几何走向解析")
    elif note == "join":
        warnings.append(f"{cur[0]} 未直接位于航路 {awy} 上，已在 {best['join']} 处切入")

    for (name, lat, lon) in best["seg"]:
        _append(pts, name, lat, lon)
    chain, pj = best["chain"], best["pj"]
    return j + 1, (chain.fixes[pj], chain.lats[pj], chain.lons[pj]), True


def _skip_back(tokens: List[str], k: int) -> int:
    """Step back over separators, runway designators and procedure keywords."""
    k -= 1
    while k >= 0 and (tokens[k] == "/" or RUNWAY_RE.match(tokens[k])
                      or tokens[k] in KEYWORDS):
        k -= 1
    return k


def _tail_airport(cyc: NavCycle, tokens: List[str]):
    """Return ``(icao, index)`` of the airport at the end of the route."""
    k = len(tokens) - 1
    while k >= 0 and (tokens[k] == "/" or RUNWAY_RE.match(tokens[k])):
        k -= 1
    # An approach is often written behind the arrival airport
    # ("… ZGGG I18LY"): the airport is still the arrival, not a waypoint.  The
    # name may even collide with an airway (``R14``) or a small airport
    # (``I06``), so it is matched against the previous airport's approaches
    # before anything else.
    if k >= 0:
        prev = _skip_back(tokens, k)
        if prev >= 0 and tokens[prev] != tokens[k] and cyc.airport(tokens[prev]):
            if cyc.has_procedure("iap", tokens[prev], tokens[k]):
                return tokens[prev], prev
    if k >= 0 and cyc.airport(tokens[k]):
        return tokens[k], k
    if k >= 0:
        prev = _skip_back(tokens, k)
        if prev >= 0 and cyc.airport(tokens[prev]) and not cyc.is_airway(tokens[k]) \
                and cyc.resolve_point(tokens[k]) is None:
            # clearly not a published fix: report the airport, let the main
            # loop warn about the unknown token
            return tokens[prev], prev
    return None, -1


def _runway_after(cyc: NavCycle, tokens: List[str], start: int, end: int,
                  apt: str) -> Optional[str]:
    """Runway designator that belongs to ``apt``'s procedure.

    Handles both ``ZBAA/18L DOTR2G`` and ``... ENVIP1 ZGGG/02R``: the arrival
    airport token is stepped over, but a *different* airport ends the search so
    a STAR at the departure airport can never pick up the arrival runway.
    """
    for k in range(start, min(start + 5, end)):
        tok = tokens[k]
        if not tok or tok == "/" or tok in KEYWORDS:
            continue
        if cyc.is_airway(tok):
            return None
        if RUNWAY_RE.match(tok) and not cyc.airport(tok):
            return tok
        if cyc.airport(tok):
            if tok != apt:
                return None
            continue
        if cyc.resolve_point(tok) is not None:
            return None
    return None


def _transition_for(cyc: NavCycle, proc, kind: str, cur, tokens, i, n):
    """Pick the enroute transition of ``proc`` from the route context.

    SID: the fix that follows the procedure name (the cleared exit fix).
    STAR: the fix that precedes it (the airway join point).
    """
    if kind == "sid":
        exit_fix = _peek_fix(cyc, tokens, i + 1, n, (cur[1], cur[2]) if cur else None)
        if exit_fix is None:
            return None, []
        if not proc.transitions:
            return None, []
        for trans in proc.transitions:
            if _key_eq(trans, exit_fix):
                return trans, []
        return exit_fix, []
    if cur is None or not proc.transitions:
        return None, []
    for trans in proc.transitions:
        if _key_eq(trans, cur[0]):
            return trans, []
    # the transition may be identified by its first fix instead of its name
    for trans in proc.transitions:
        legs = proc.parts_for(trans)
        if legs and _key_eq(legs[0].name, cur[0]):
            return trans, []
    return None, []


def _leg_distance_nm(legs) -> Optional[float]:
    total = 0.0
    seen = False
    for a, b in zip(legs, legs[1:]):
        if a.lat is None or b.lat is None:
            continue
        total += geo.dist((a.lat, a.lon), (b.lat, b.lon))
        seen = True
    return round(total, 1) if seen else None


def parse_route(cyc: NavCycle, route: str, detail: bool = True) -> dict:
    warnings: List[str] = []
    tokens = tokenize(route)
    # Some identifiers contain a hyphen: an airway (``FANS-2``) but also an
    # approach (``R01-Y`` / ``VOR-A``).  The splitter removes the hyphen, so the
    # parts are joined back only when the joined name really exists here.
    apt_hints: List[str] = []
    for tok in tokens:
        if tok not in apt_hints and cyc.airport(tok):
            apt_hints.append(tok)

    def _known_joined(name: str) -> bool:
        if cyc.is_airway(name):
            return True
        for apt in apt_hints:
            for kind in ("sid", "star", "iap"):
                if cyc.has_procedure(kind, apt, name):
                    return True
        return False

    k = 0
    merged: List[str] = []
    while k < len(tokens):
        if k + 1 < len(tokens) and _known_joined(tokens[k] + "-" + tokens[k + 1]):
            merged.append(tokens[k] + "-" + tokens[k + 1])
            k += 2
        else:
            merged.append(tokens[k])
            k += 1
    tokens = merged
    result = {
        "cycle": cyc.cycle,
        "route": (route or "").strip(),
        "warnings": warnings,
        "waypoints": [],
        "dep": None,
        "arr": None,
        "sid": None,
        "star": None,
        "iap": None,
    }
    if not tokens:
        warnings.append("航路为空")
        return result

    n = len(tokens)
    dep = arr = None
    if n >= 2 and cyc.airport(tokens[0]):
        dep = tokens[0]
    # the arrival airport may be followed by a runway ("ZGGG/02R")
    arr, arr_idx = _tail_airport(cyc, tokens)
    if arr == dep:
        arr, arr_idx = None, -1
    if dep and arr and dep == arr:
        warnings.append("起降机场相同")
        return result
    result["dep"], result["arr"] = dep, arr

    pts: List[Tuple[str, float, float]] = []
    cur = None
    if dep:
        lat, lon, _ = cyc.airport(dep)
        cur = (dep, lat, lon)
        pts.append(cur)

    ctx_airport = dep
    ctx_runway = None
    forced = None
    i = 1 if dep else 0

    while i < n:
        tok = tokens[i]

        # arrival airport (may be followed by "/02R")
        if arr and i == arr_idx:
            lat, lon, _ = cyc.airport(arr)
            if result["iap"] is None:
                # an approach written in front of the airport already ends on
                # the runway; appending the reference point would fly back
                _append(pts, arr, lat, lon)
            cur = (arr, lat, lon)
            ctx_airport, ctx_runway = arr, None
            i += 1
            continue

        if not tok or tok == "/":
            i += 1
            continue

        # explicit "SID" / "STAR" / "IAP" keyword in front of the procedure name
        if tok in ("SID", "STAR", "IAP", "APCH", "APP", "APR"):
            forced = {"SID": "sid", "STAR": "star"}.get(tok, "iap")
            i += 1
            continue

        if tok in KEYWORDS:
            i += 1
            continue

        # runway designator following an airport / procedure
        if cur is not None and RUNWAY_RE.match(tok) and not cyc.is_airway(tok) \
                and not cyc.airport(tok):
            ctx_runway = tok
            i += 1
            continue

        # a mid-route airport is a valid waypoint — unless it is really the
        # approach of the airport just parsed ("RCBS I06": I06 is an airport
        # identifier as well)
        if cyc.airport(tok) and not cyc.is_airway(tok) and not (
                ctx_airport and ctx_airport != tok
                and cyc.has_procedure("iap", ctx_airport, tok)):
            lat, lon, _ = cyc.airport(tok)
            _append(pts, tok, lat, lon)
            cur = (tok, lat, lon)
            ctx_airport, ctx_runway = tok, None
            i += 1
            continue

        # SID / STAR / APPROACH -----------------------------------------
        # An approach name can collide with an airway (``YPMP R14``: R14 is
        # both), so a known approach of the airport just parsed is tried even
        # when the identifier also names an airway.
        is_local_approach = bool(
            ctx_airport and ctx_airport != tok
            and cyc.has_procedure("iap", ctx_airport, tok))
        if not cyc.is_airway(tok) or is_local_approach:
            contexts: List[Tuple[str, str]] = []
            if forced == "sid" and ctx_airport:
                contexts.append(("sid", ctx_airport))
            elif forced == "star":
                for apt in (arr, ctx_airport):
                    if apt:
                        contexts.append(("star", apt))
            elif forced == "iap":
                for apt in (arr, ctx_airport):
                    if apt:
                        contexts.append(("iap", apt))
            else:
                for apt in (ctx_airport, arr):
                    if apt:
                        contexts.append(("sid", apt))
                        contexts.append(("star", apt))
                # an approach is only ever tried last: an identifier that is
                # already a SID / STAR of this airport keeps that meaning
                for apt in (arr, ctx_airport):
                    if apt:
                        contexts.append(("iap", apt))
                if not contexts:
                    for apt in (dep, arr):
                        if apt:
                            contexts.append(("star", apt))
                            contexts.append(("sid", apt))
            seen = set()
            expanded = False
            for kind, apt in contexts:
                if (kind, apt) in seen:
                    continue
                seen.add((kind, apt))
                proc = cyc.procedure(kind, apt, tok)
                if proc is None:
                    continue
                runway = _runway_after(cyc, tokens, i + 1, n, apt)
                if kind == "sid":
                    # the runway typed after the departure airport wins
                    runway = (ctx_runway if ctx_airport == apt else None) or runway
                else:
                    # ... a STAR takes the runway written after it or after the
                    # arrival airport, never the departure runway
                    runway = runway or (ctx_runway if ctx_airport == apt else None)
                trans, _ = _transition_for(cyc, proc, kind, cur, tokens, i, n)
                legs, rw, tr, notes = proc.expand(runway, trans)
                if not legs:
                    if proc.runways:
                        warnings.append(
                            f"{tok} 有 {len(proc.runways)} 条跑道过渡且没有公共段，"
                            f"需指定跑道（如 {apt}/{proc.runways[0]}）才能展开"
                        )
                        forced = None
                        i += 1
                        expanded = True
                        break
                    continue
                warnings.extend(notes)
                if kind == "star" and tr is None and proc.transitions and cur is not None \
                        and not any(_key_eq(t, cur[0]) for t in proc.transitions):
                    warnings.append(f"{cur[0]} 不是 {tok} 的航路过渡，已按公共段解析")
                missed: list = []
                if kind == "iap":
                    # an approach ends on the runway: the go-around coded right
                    # behind it is a separate sequence, never part of the route
                    legs, missed = proc.split_missed(legs)
                    missed = missed[1:] if missed else []
                    if not legs:
                        continue
                for (name, lat, lon) in [(l.name, l.lat, l.lon) for l in legs]:
                    _append(pts, name, lat, lon)
                cur = (legs[-1].name, legs[-1].lat, legs[-1].lon)
                # The expanded legs are reported with the very same detail the
                # procedure endpoint returns: an EFB client that only calls
                # parseRoute must still see how each leg is flown.
                segs, _, _, _ = proc.expand_segments(runway, trans)
                info = {
                    "name": proc.name,
                    "airport": apt,
                    "runway": rw,
                    "transition": tr,
                    "waypoints": len(legs),
                    "runways": proc.runways,
                    "transitions": proc.transitions,
                    "leg_types": sorted({l.path for l in segs if l.path}),
                    "start": legs[0].name if legs else None,
                    "end": legs[-1].name if legs else None,
                    "distance_nm": _leg_distance_nm(legs),
                }
                if kind == "iap":
                    app_track, ga_track, end_leg = proc.track_split(runway, trans)
                    info["approach"] = proc.approach
                    info["approach_name"] = proc.approach_name
                    info["final_types"] = list(proc.final_types)
                    info["missed"] = len(missed)
                    if end_leg is not None:
                        info["approach_end"] = {
                            "name": end_leg.name,
                            "lat": round(end_leg.lat, 8),
                            "lon": round(end_leg.lon, 8),
                            "role": end_leg.role,
                        }
                    if detail:
                        info["track"] = legtext.track_points_from(app_track)
                        info["missed_track"] = legtext.track_points_from(ga_track)
                        info["missed_waypoints"] = [
                            {
                                "name": l.name,
                                "lat": round(l.lat, 8),
                                "lon": round(l.lon, 8),
                                "role": l.role,
                                "path": l.path,
                            }
                            for l in missed
                        ]
                if detail:
                    info["segments"] = legtext.segments(segs)
                    if kind != "iap":
                        info["track"] = legtext.track_points(segs)
                result[kind] = info
                expanded = True
                break
            if expanded:
                forced = None
                i += 1
                continue

        # airway
        if cyc.is_airway(tok):
            i, new_cur, handled = _take_airway(cyc, tok, cur, tokens, i, n, pts, warnings)
            if handled:
                if new_cur is not None:
                    cur = new_cur
                continue

        # plain fix / navaid
        near = (cur[1], cur[2]) if cur else None
        point = cyc.resolve_point(tok, near)
        if point is not None:
            _append(pts, tok, point[0], point[1])
            cur = (tok, point[0], point[1])
            i += 1
            continue

        coord = _latlon(tok)
        if coord is not None:
            _append(pts, tok, coord[0], coord[1])
            cur = (tok, coord[0], coord[1])
            i += 1
            continue

        warnings.append(f"无法识别的航路元素 {tok}")
        i += 1

    if not pts:
        warnings.append("未能解析出任何航路点")
    result["waypoints"] = [
        {"name": name, "lat": round(lat, 8), "lon": round(lon, 8)} for name, lat, lon in pts
    ]
    return result
