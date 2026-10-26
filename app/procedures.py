"""SID / STAR expansion for Navigraph DFD v2 (ARINC 424 terminal procedures).

A terminal procedure is stored as several independent *parts* that share one
``procedure_identifier``.  The ``route_type`` column tells which part a record
belongs to and, implicitly, which coding standard produced it:

SID  (tbl_pd_sids)                     STAR (tbl_pe_stars)
  0  engine-out / single sequence        1  enroute transition
  1  runway transition                   2  common route
  2  common route                        3  runway transition
  3  enroute transition                  4  RNAV enroute transition
  4  RNAV runway transition              5  RNAV common route
  5  RNAV common route                   6  RNAV runway transition
  6  RNAV enroute transition             7  profile-descent enroute
  F  FMS runway transition                8  profile-descent common
  M  FMS common route                     9  profile-descent runway
  S  FMS enroute transition               F  FMS enroute transition
  T  vector runway transition             M  FMS common route
  V  vector enroute transition            S  FMS runway transition

The assembly order differs: a SID flies runway -> common -> enroute, a STAR
flies enroute -> common -> runway.

Two properties of the real data drive the implementation:

* a procedure may carry several coding families at once (e.g. ``4/5/6`` next to
  ``1/2/3``); legs must never be mixed across families, otherwise the aircraft
  would fly a hybrid path that is published nowhere;
* the runway / enroute transition may be unknown.  Rather than guessing, the
  expansion drops the ambiguous part and reports it, unless every variant is
  geometrically identical (then the choice cannot be wrong).
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from . import geo
from . import legs as leggeom

PROC_TABLE = {"sid": "tbl_pd_sids", "star": "tbl_pe_stars", "iap": "tbl_pf_iaps"}

# ARINC 424 "Route Type" of an approach record.  ``A`` is the approach
# transition (initial approach segments funnelling onto the final course);
# every other letter names the navigation aid the final approach is built on
# and is, by convention, also the first letter of the procedure identifier
# (``I02LZ`` -> ILS, ``R16`` -> RNAV).
APPROACH_TYPES: Dict[str, Tuple[str, str]] = {
    "A": ("TRANS", "进近过渡"),
    "B": ("LOC/BC", "航向道/反航道"),
    "D": ("VOR/DME", "VOR/DME"),
    "F": ("FMS", "FMS"),
    "G": ("IGS", "仪表引导"),
    "H": ("RNP", "RNP"),
    "I": ("ILS", "仪表着陆"),
    "J": ("GLS", "GBAS 着陆"),
    "L": ("LOC", "航向道"),
    "M": ("MLS", "微波着陆"),
    "N": ("NDB", "无方向信标"),
    "P": ("GPS", "GPS"),
    "Q": ("NDB/DME", "NDB/DME"),
    "R": ("RNAV", "区域导航"),
    "S": ("VOR/DME", "VOR/DME(VORTAC)"),
    "T": ("TACAN", "塔康"),
    "U": ("SDF", "简化定向台"),
    "V": ("VOR", "VOR"),
    "W": ("MLS-A", "微波着陆 A"),
    "X": ("LDA", "航向道偏置"),
    "Y": ("MLS-B/C", "微波着陆 B/C"),
}

# route_type -> (role, family)
ROUTE_TYPES: Dict[str, Dict[str, Tuple[str, str]]] = {
    "sid": {
        "0": ("single", "engine-out"),
        "1": ("runway", "conventional"),
        "2": ("common", "conventional"),
        "3": ("enroute", "conventional"),
        "4": ("runway", "rnav"),
        "5": ("common", "rnav"),
        "6": ("enroute", "rnav"),
        "7": ("enroute", "rnav"),
        "F": ("runway", "fms"),
        "M": ("common", "fms"),
        "S": ("enroute", "fms"),
        "T": ("runway", "vector"),
        "V": ("enroute", "vector"),
        "H": ("single", "rnp"),
    },
    "star": {
        "1": ("enroute", "conventional"),
        "2": ("common", "conventional"),
        "3": ("runway", "conventional"),
        "4": ("enroute", "rnav"),
        "5": ("common", "rnav"),
        "6": ("runway", "rnav"),
        "7": ("enroute", "profile"),
        "8": ("common", "profile"),
        "9": ("runway", "profile"),
        "F": ("enroute", "fms"),
        "M": ("common", "fms"),
        "S": ("runway", "fms"),
        "H": ("single", "rnp"),
    },
}

# Lower is better when two families are equally usable.
FAMILY_RANK = {
    "rnav": 0,
    "profile": 1,
    "fms": 2,
    "conventional": 3,
    "vector": 4,
    "rnp": 5,
    "engine-out": 6,
}

ASSEMBLY_ORDER = {
    "sid": ("runway", "common", "enroute"),
    "star": ("enroute", "common", "runway"),
    # an approach flies the transition first, then the common final segment
    "iap": ("transition", "final"),
}

LEG_ROLES = ("runway", "common", "enroute", "single", "transition", "final")

# The runway end of an approach is always coded as a fix named ``RW34L``.
RUNWAY_FIX_RE = re.compile(r"^RW(\d{1,2}[LCR]?)$")
# Procedure identifier of an approach: nav-aid letter + runway + variant
# (``I02LZ`` -> 02L, ``R19-Y`` -> 19, ``T03RA`` -> 03R).
PROC_RUNWAY_RE = re.compile(r"^[A-Z](\d{1,2}[LCR]?)")


def _route_role(kind: str, rtype: str) -> Optional[Tuple[str, str]]:
    """``route_type`` -> ``(role, family)`` for the record's coding standard.

    Approaches use a single family: an ``A`` record is an approach transition,
    anything else is the final approach segment (ILS, RNAV, VOR, ...).  Keeping
    them in one family is what makes "transition + final" a legal combination.
    """
    if kind == "iap":
        return ("transition", "approach") if rtype == "A" else ("final", "approach")
    return ROUTE_TYPES[kind].get(rtype)


# A published terminal leg longer than this is almost always a navdata error
# (e.g. a fix coded with the coordinates of a same-named navaid elsewhere).
LEG_JUMP_WARN_NM = 600.0

RUNWAY_RE = re.compile(r"^(?:RW)?(\d{1,2})([LCR]?)$")

# A published fix closer than this to the runway threshold already *is* the
# runway end, so no extra point is inserted.
RUNWAY_SNAP_NM = 3.0


class Leg(NamedTuple):
    """One leg of a terminal procedure (ARINC 424 record).

    ``lat`` / ``lon`` are ``None`` for the *open* terminators (``CA``, ``VA``,
    ``CI``, ``VI``, ``CD``, ``VD``, ``CR``, ``VR``, ``FA``, ``FM``, ``VM``):
    those end at an altitude, an intercept or a DME distance, so the coding
    carries a course and no position.
    """

    name: str
    lat: Optional[float]
    lon: Optional[float]
    role: str          # runway | common | enroute | single
    transition: str    # transition identifier the leg belongs to ("" for common)
    path: str          # ARINC path terminator (TF, CF, RF, ...)
    desc: str          # waypoint description code
    # ---- ARINC 424 geometry, needed by the path terminators ----
    course: Optional[float] = None      # coded course / heading (degrees)
    course_flag: str = ""               # M = magnetic, T = true
    rho: Optional[float] = None         # DME distance (NM)
    theta: Optional[float] = None       # VOR radial (degrees)
    turn: str = ""                      # L / R turn direction
    radius: Optional[float] = None      # RF arc radius (NM)
    center: Optional[Tuple[float, float]] = None   # RF arc centre
    navaid: Optional[Tuple[float, float]] = None   # DME / VOR reference
    navaid_name: str = ""
    alt_desc: str = ""                  # + / - / @ / B ...
    alt1: Optional[float] = None
    alt2: Optional[float] = None
    dist: Optional[float] = None        # distance or time of the leg
    dist_kind: str = ""                 # D = distance (NM), T = time (min)
    center_name: str = ""               # RF arc centre waypoint name
    speed: Optional[float] = None       # speed limit (kt)
    speed_desc: str = ""                # + / - / @
    vangle: Optional[float] = None      # vertical angle (degrees, -3.0 = 3° path)
    rnp: Optional[float] = None         # RNP value (NM)

    @property
    def open(self) -> bool:
        return self.lat is None or self.lon is None


def normalize_runway(value: Optional[str]) -> Optional[str]:
    """``18l`` / ``RW18L`` / ``RWY18L`` -> ``18L``."""
    if not value:
        return None
    s = str(value).strip().upper().replace(" ", "")
    if s.startswith("RWY"):
        s = s[3:]
    elif s.startswith("RW"):
        s = s[2:]
    m = RUNWAY_RE.match(s)
    if not m:
        return s or None
    return m.group(1) + m.group(2)


def _key(value: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _usable(row) -> bool:
    name, lat, lon = row[3], row[4], row[5]
    if not name or lat is None or lon is None:
        return False
    # A record without a position is a *heading / course / altitude* leg
    # (CA, VA, CI, VI, VM, FM, ...): it has no fix to plot, the next leg starts
    # at the same point.
    try:
        flat, flon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not (-90.0 <= flat <= 90.0) or not (-180.0 <= flon <= 180.0):
        return False
    # (0, 0) is a placeholder, never a real terminal fix
    if abs(flat) < 1e-9 and abs(flon) < 1e-9:
        return False
    return True


def _num(value) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _pos(lat, lon) -> Optional[Tuple[float, float]]:
    try:
        flat, flon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if flat is None or flon is None:
        return None
    if not (-90.0 <= flat <= 90.0) or not (-180.0 <= flon <= 180.0):
        return None
    if abs(flat) < 1e-9 and abs(flon) < 1e-9:
        return None
    return (flat, flon)


def build_leg(row, role: str, transition: str) -> Optional[Leg]:
    """Turn one ARINC 424 row into a :class:`Leg`.

    Positional rows keep their fix; rows whose terminator ends at an altitude,
    an intercept or a DME distance (``CA``/``VA``/``CI``/``VI``/``CD``/``VD``/
    ``CR``/``VR``/``FA``/``FM``/``VM``) are kept as *open* legs so the course,
    altitude and turn direction reach the client instead of being dropped.
    """
    name = str(row[3]).strip().upper() if row[3] else ""
    path = (row[7] or "").strip().upper()
    desc = (row[6] or "").strip().upper()
    position = _pos(row[4], row[5])
    if position is None:
        # No position: legal only for the terminators that end elsewhere.
        if path not in leggeom.OPEN_TYPES:
            return None
    elif not name:
        return None

    extra = row[8:]
    def at(i):
        return extra[i] if len(extra) > i else None

    return Leg(
        name or path,
        position[0] if position else None,
        position[1] if position else None,
        role,
        transition,
        path,
        desc,
        course=_num(at(0)),
        course_flag=(str(at(1) or "")).strip().upper(),
        rho=_num(at(2)),
        theta=_num(at(3)),
        turn=(str(at(4) or "")).strip().upper()[:1],
        radius=_num(at(5)),
        center=_pos(at(6), at(7)),
        navaid=_pos(at(9), at(10)),
        navaid_name=(str(at(8) or "")).strip().upper(),
        alt_desc=(str(at(11) or "")).strip().upper(),
        alt1=_num(at(12)),
        alt2=_num(at(13)),
        dist=_num(at(14)),
        dist_kind=(str(at(15) or "")).strip().upper(),
        center_name=(str(at(16) or "")).strip().upper(),
        speed=_num(at(17)),
        speed_desc=(str(at(18) or "")).strip().upper(),
        vangle=_num(at(19)),
        rnp=_num(at(20)),
    )


def _same_xy(a: Leg, b: Leg) -> bool:
    if a.lat is None or b.lat is None:
        return a.lat is None and b.lat is None
    return abs(a.lat - b.lat) < 1e-6 and abs(a.lon - b.lon) < 1e-6


def _dedupe(legs: List[Leg]) -> List[Leg]:
    out: List[Leg] = []
    for leg in legs:
        if out:
            last = out[-1]
            if last.name == leg.name and _same_xy(last, leg) and last.path == leg.path:
                continue
            if leg.open and last.open and last.path == leg.path \
                    and last.course == leg.course:
                continue
        out.append(leg)
    return out


class Procedure:
    """One SID or STAR, assembled from every ``route_type`` part."""

    __slots__ = ("kind", "airport", "name", "parts", "sequences", "runways_xy",
                 "declared", "final_types", "_expanded")

    def __init__(self, kind: str, airport: str, name: str, rows: Iterable[tuple],
                 runway_points: Optional[Dict[str, Tuple[float, float]]] = None):
        self.kind = kind
        self.airport = airport
        self.name = name
        self.runways_xy = runway_points or {}
        # transitions declared by the coding, even when their legs carry no
        # position at all (a pure "runway then heading" vector SID)
        self.declared: Dict[str, Dict[str, set]] = {}
        # family -> role -> transition -> [Leg]   (positional legs only)
        self.parts: Dict[str, Dict[str, Dict[str, List[Leg]]]] = {}
        # (family, role, transition) -> [Leg]     (every leg, incl. open ones)
        self.sequences: Dict[tuple, List[Leg]] = {}
        # route types actually used by the final approach segment
        self.final_types: List[str] = []
        self._expanded: Dict[tuple, tuple] = {}

        grouped: Dict[tuple, List[tuple]] = {}
        for row in rows:
            # row = (seqno, route_type, transition, name, lat, lon, desc, path, ...)
            rtype = (row[1] or "").strip().upper()
            mapping = _route_role(kind, rtype)
            if mapping is None:
                continue  # unknown coding: never guess
            role, family = mapping
            trans = (row[2] or "").strip().upper()
            if trans and role in ("runway", "enroute", "single", "transition", "final"):
                self.declared.setdefault(family, {}).setdefault(role, set()).add(trans)
            if role == "final" and rtype and rtype not in self.final_types:
                self.final_types.append(rtype)
            key = (family, role, trans)
            grouped.setdefault(key, []).append(row)

        for (family, role, trans), items in grouped.items():
            items.sort(key=lambda r: (r[0] is None, r[0]))
            sequence: List[Leg] = []
            for row in items:
                leg = build_leg(row, role, trans)
                if leg is not None:
                    sequence.append(leg)
            sequence = _dedupe(sequence)
            if not sequence:
                continue
            self.sequences[(family, role, trans)] = sequence
            positional = [l for l in sequence if not l.open]
            if not positional:
                continue
            fam = self.parts.setdefault(family, {r: {} for r in LEG_ROLES})
            fam[role].setdefault(trans, positional)

    def _sequence(self, family: str, role: str, trans: str) -> List[Leg]:
        return list(self.sequences.get((family, role, trans), ()))

    # ---------------------------------------------------------------- properties

    @property
    def families(self) -> List[str]:
        names = set(self.parts) | set(self.declared)
        return sorted(names, key=lambda f: FAMILY_RANK.get(f, 99))

    def _family_runways(self, family: str) -> List[str]:
        legs = self.parts.get(family, {}).get("runway", {})
        pool = set(self.declared.get(family, {}).get("runway", ()))
        pool.update(t for t in legs if t)
        return sorted(pool)

    @property
    def runways(self) -> List[str]:
        """Runway designators served by this procedure (``18L``, ``01`` …)."""
        if self.kind == "iap":
            return self._approach_runways()
        seen: List[str] = []
        for family in self.families:
            for trans in self._family_runways(family):
                rw = normalize_runway(trans)
                if rw and rw not in seen:
                    seen.append(rw)
        return seen

    def _approach_runways(self) -> List[str]:
        """Runway served by an approach: read from the ``RWxx`` fix, else the name."""
        seen: List[str] = []
        for seq in self.sequences.values():
            for leg in seq:
                m = RUNWAY_FIX_RE.match(leg.name or "")
                if m and m.group(1) not in seen:
                    seen.append(m.group(1))
        if not seen:
            m = PROC_RUNWAY_RE.match(self.name or "")
            if m:
                seen.append(normalize_runway(m.group(1)) or m.group(1))
        return seen

    @property
    def approach(self) -> str:
        """Approach type code (``ILS`` / ``RNAV`` / ``VOR/DME`` …), ``""`` if unknown."""
        letters = self.final_types or [self.name[:1]]
        for letter in letters:
            code = APPROACH_TYPES.get((letter or "").upper())
            if code:
                return code[0]
        return (letters[0] or "").upper()

    @property
    def approach_name(self) -> str:
        """Human readable approach type (Chinese) for the console."""
        letters = self.final_types or [self.name[:1]]
        for letter in letters:
            code = APPROACH_TYPES.get((letter or "").upper())
            if code:
                return code[1]
        return ""

    @property
    def transitions(self) -> List[str]:
        """Transition identifiers: enroute for SID/STAR, IAF for an approach."""
        role = "transition" if self.kind == "iap" else "enroute"
        seen: List[str] = []
        for family in self.families:
            pool = set(self.declared.get(family, {}).get(role, ()))
            pool.update(t for t in self.parts.get(family, {}).get(role, {}) if t)
            for trans in sorted(pool):
                if trans not in seen:
                    seen.append(trans)
        return seen

    @property
    def has_positions(self) -> bool:
        """True when at least one leg carries a published position."""
        return bool(self.parts)

    @property
    def single_sequence(self) -> bool:
        return any(self.parts.get(f, {}).get("single") for f in self.parts)

    # ------------------------------------------------------------------ helpers

    def _pick_family(self, runway: Optional[str], transition: Optional[str]) -> str:
        best, best_score = None, None
        for family in self.families:
            data = self.parts.get(family) or {r: {} for r in LEG_ROLES}
            score = 0
            if runway:
                for trans in self._family_runways(family):
                    if normalize_runway(trans) == runway:
                        score += 6
                        break
            if transition:
                for trans in data["enroute"]:
                    if _key(trans) == _key(transition) or \
                            _key(trans).endswith(_key(transition)) or \
                            _key(transition).endswith(_key(trans)):
                        score += 4
                        break
            if any(data["common"].values()):
                score += 2
            score -= FAMILY_RANK.get(family, 99) * 0.1
            if best_score is None or score > best_score:
                best, best_score = family, score
        return best or "conventional"

    def _resolve_runway(self, family: str, runway: Optional[str]):
        """Return (transition_id, notes).  ``None`` means "not usable"."""
        variants = self._family_runways(family)
        if not variants:
            return None, []
        if runway:
            for trans in variants:
                if normalize_runway(trans) == runway:
                    return trans, []
            return None, [f"跑道 {runway} 不属于 {self.name}，已忽略跑道过渡"]
        # Only variants that actually carry legs can be compared; a declared
        # runway transition without positions is still a valid designator.
        legs_of = self.parts.get(family, {}).get("runway", {})
        present = [t for t in variants if legs_of.get(t)]
        if len(variants) == 1:
            return variants[0], []
        if not present:
            return None, []
        signature = [(l.name, round(l.lat, 5), round(l.lon, 5)) for l in legs_of[present[0]]]
        for trans in present[1:]:
            other = legs_of[trans]
            if [(l.name, round(l.lat, 5), round(l.lon, 5)) for l in other] != signature:
                return None, [
                    f"未指定跑道，{self.name} 有 {len(variants)} 条不同的跑道过渡，"
                    f"已从公共段开始"
                ]
        return present[0], []

    def _resolve_transition(self, family: str, transition: Optional[str],
                            role: str = "enroute"):
        pool = self.parts.get(family, {}).get(role, {})
        if not pool:
            # a transition may be coded entirely with open legs
            pool = {
                trans: seq
                for (fam, r, trans), seq in self.sequences.items()
                if fam == family and r == role
            }
        if not pool:
            return None, []
        if transition:
            wanted = _key(transition)
            for trans in pool:
                if _key(trans) == wanted:
                    return trans, []
            for trans in pool:
                if _key(trans).endswith(wanted) or wanted.endswith(_key(trans)):
                    return trans, []
            # A SID is often cleared to a fix that is the transition's *first*
            # fix rather than the transition name itself.
            for trans, legs in pool.items():
                first = next((l for l in legs if not l.open), None)
                if first is not None and _key(first.name) == wanted:
                    return trans, []
            what = "进近过渡" if role == "transition" else "航路过渡"
            return None, [f"{self.name} 没有{what} {transition}，已从公共段开始"]
        if role == "transition":
            # an approach without a named transition always starts at the IAF
            return None, []
        if len(pool) == 1:
            return next(iter(pool)), []
        return None, []

    # ------------------------------------------------------------------- expand

    def expand(
        self,
        runway: Optional[str] = None,
        transition: Optional[str] = None,
    ) -> Tuple[List[Leg], Optional[str], Optional[str], List[str]]:
        """Assemble the procedure into one ordered leg list.

        Returns ``(legs, runway, transition, notes)``.  ``legs`` contains only
        the legs that carry a position; use :meth:`expand_segments` for the
        complete ARINC 424 sequence including the open (vector) legs.
        """
        runway = normalize_runway(runway)
        ckey = (runway or "", (transition or "").upper())
        hit = self._expanded.get(ckey)
        if hit is None:
            hit = self._assemble(runway, transition)
            self._expanded[ckey] = hit
        return list(hit[0]), hit[1], hit[2], list(hit[3])

    def _assemble(self, runway: Optional[str], transition: Optional[str]):
        notes: List[str] = []
        if not self.parts and not self.sequences:
            return (), None, None, [f"{self.name} 没有可定位的航段"], ()

        if self.kind == "iap":
            return self._assemble_iap(runway, transition)

        if not self.parts:
            # Every leg is a heading / altitude leg without a position.  The
            # runway threshold is still real, so a vector SID stays plottable.
            rw = runway
            if rw is None:
                variants = sorted({
                    t for fam in self.declared.values() for t in fam.get("runway", ())
                })
                rw = normalize_runway(variants[0]) if len(variants) == 1 else None
            anchored = self._anchor_runway([], rw) if rw else []
            segs = self._anchor_runway(self._all_open_legs(), rw) if rw else []
            if anchored:
                return tuple(anchored), rw, None, [
                    f"{self.name} 仅含向量航段，已标注跑道入口"
                ], tuple(segs)
            return (), None, None, [f"{self.name} 没有可定位的航段"], ()

        family = self._pick_family(runway, transition)
        data = self.parts[family]

        legs: List[Leg] = []
        segs: List[Leg] = []
        used_runway = None
        used_transition = None

        if data["single"]:
            # Engine-out / RNP procedures: one continuous sequence per runway.
            variants = data["single"]
            chosen = None
            if runway:
                for trans in variants:
                    if normalize_runway(trans) == runway:
                        chosen = trans
                        break
                if chosen is None:
                    notes.append(f"跑道 {runway} 不属于 {self.name}，已改用全部航段")
            if chosen is None:
                chosen = sorted(variants)[0] if len(variants) == 1 else None
            if chosen is not None:
                legs = list(variants[chosen])
                segs = self._sequence(family, "single", chosen) or legs
                used_runway = normalize_runway(chosen)
        else:
            rw_id, rw_notes = self._resolve_runway(family, runway)
            notes.extend(rw_notes)
            tr_id, tr_notes = self._resolve_transition(family, transition)
            notes.extend(tr_notes)

            for role in ASSEMBLY_ORDER[self.kind]:
                if role == "runway":
                    if rw_id:
                        legs.extend(data["runway"].get(rw_id) or [])
                        segs.extend(self._sequence(family, "runway", rw_id))
                        used_runway = normalize_runway(rw_id)
                elif role == "common":
                    common_key = None
                    trunk = data["common"].get("")
                    if trunk:
                        common_key = ""
                    elif rw_id and rw_id in data["common"]:
                        # the trunk is coded per runway instead of once
                        common_key, trunk = rw_id, data["common"][rw_id]
                    elif data["common"]:
                        common_key = next(iter(data["common"]))
                        variants = list(data["common"].values())
                        signatures = {
                            tuple((l.name, round(l.lat, 5), round(l.lon, 5)) for l in v)
                            for v in variants
                        }
                        trunk = variants[0]
                        if len(signatures) > 1:
                            notes.append(
                                f"{self.name} 的公共段按跑道分别编码，已取 "
                                f"{common_key} 的公共段"
                            )
                    legs.extend(trunk or [])
                    if common_key is not None:
                        segs.extend(self._sequence(family, "common", common_key))
                else:
                    if tr_id:
                        legs.extend(data["enroute"].get(tr_id) or [])
                        segs.extend(self._sequence(family, "enroute", tr_id))
                        used_transition = tr_id

        legs = _dedupe(legs)
        segs = _dedupe(segs)
        legs = self._anchor_runway(legs, used_runway)
        segs = self._anchor_runway(segs, used_runway)
        notes.extend(self._jump_notes(legs))
        return tuple(legs), used_runway, used_transition, notes, tuple(segs)

    def _assemble_iap(self, runway: Optional[str], transition: Optional[str]):
        """Approach = approach transition (IAF) -> final segment -> missed approach.

        The final approach segments of one procedure identifier are always coded
        under a single ``route_type`` (``I`` for ILS, ``R`` for RNAV, …), so the
        only choice left is *which* transition to glue in front of it.  The
        missed approach stays in the sequence: it is coded right after the
        runway end and is part of the published procedure.
        """
        notes: List[str] = []
        family = "approach" if "approach" in self.parts else self._pick_family(runway, transition)
        data = self.parts.get(family) or {r: {} for r in LEG_ROLES}

        legs: List[Leg] = []
        segs: List[Leg] = []
        used_transition = None

        tr_id, tr_notes = self._resolve_transition(family, transition, role="transition")
        notes.extend(tr_notes)
        if tr_id:
            legs.extend(data.get("transition", {}).get(tr_id) or [])
            segs.extend(self._sequence(family, "transition", tr_id))
            used_transition = tr_id

        finals = data.get("final") or {}
        fin_key = self._pick_final(finals, runway, notes)
        if fin_key is not None:
            legs.extend(finals.get(fin_key) or [])
            segs.extend(self._sequence(family, "final", fin_key))

        legs = _dedupe(legs)
        segs = _dedupe(segs)
        used_runway = self._iap_runway(legs)
        legs = self._anchor_runway(legs, used_runway)
        segs = self._anchor_runway(segs, used_runway)
        legs = self._mark_missed(legs)
        segs = self._mark_missed(segs)
        notes.extend(self._jump_notes(legs))
        if not legs:
            notes.append(f"{self.name} 没有可定位的最终进近航段")
        return tuple(legs), used_runway, used_transition, notes, tuple(segs)

    @staticmethod
    def _pick_final(finals: Dict[str, List[Leg]], runway: Optional[str],
                    notes: List[str]) -> Optional[str]:
        """Choose the final-approach sequence to fly.

        The plain ``""`` key is the final segment proper.  A few procedures also
        carry per-runway variants (``RW03``); those are only used when there is
        no plain final segment.
        """
        if not finals:
            return None
        if "" in finals:
            return ""
        if runway:
            for key in finals:
                if normalize_runway(key) == runway:
                    return key
        if len(finals) == 1:
            return next(iter(finals))
        keys = sorted(finals)
        notes.append(f"最终进近按跑道分别编码，已取 {keys[0]}")
        return keys[0]

    def _iap_runway(self, legs: List[Leg]) -> Optional[str]:
        """Runway flown: the ``RWxx`` fix if coded, else parsed from the name."""
        for leg in legs:
            if leg.open:
                continue
            m = RUNWAY_FIX_RE.match(leg.name or "")
            if m:
                return m.group(1)
        runways = self._approach_runways()
        return runways[0] if runways else None

    @staticmethod
    def _mark_missed(legs: List[Leg]) -> List[Leg]:
        """Re-tag everything after the runway end as the missed approach."""
        idx = None
        for i, leg in enumerate(legs):
            if leg.open:
                continue
            if RUNWAY_FIX_RE.match(leg.name or ""):
                idx = i
                break
        if idx is None:
            return legs
        return [leg if i <= idx else leg._replace(role="missed")
                for i, leg in enumerate(legs)]

    @staticmethod
    def _jump_notes(legs: List[Leg]) -> List[str]:
        """Warn about legs that are far longer than a terminal leg can be."""
        out: List[str] = []
        positional = [l for l in legs if not l.open]
        for a, b in zip(positional, positional[1:]):
            d = geo.dist((a.lat, a.lon), (b.lat, b.lon))
            if d > LEG_JUMP_WARN_NM:
                out.append(
                    f"航段 {a.name}→{b.name} 长 {d:.0f} NM，超出终端程序合理范围，"
                    f"数据可能存在同名点坐标错误"
                )
        return out[:3]

    def _all_open_legs(self) -> List[Leg]:
        """Every leg of the procedure that has no position, in coding order."""
        out: List[Leg] = []
        for key in sorted(self.sequences):
            out.extend(l for l in self.sequences[key] if l.open)
        return _dedupe(out)

    def expand_segments(
        self,
        runway: Optional[str] = None,
        transition: Optional[str] = None,
    ) -> Tuple[List[Leg], Optional[str], Optional[str], List[str]]:
        """Like :meth:`expand` but keeps the open (vector) legs as well."""
        legs, rw, tr, notes = self.expand(runway, transition)
        hit = self._expanded.get((normalize_runway(runway) or "", (transition or "").upper()))
        segs = list(hit[4]) if hit and len(hit) > 4 else list(legs)
        return segs, rw, tr, notes

    def track(
        self,
        runway: Optional[str] = None,
        transition: Optional[str] = None,
    ) -> List[Tuple[float, float]]:
        """Densified polyline honouring every path terminator (arcs included)."""
        segs, _, _, _ = self.expand_segments(runway, transition)
        return leggeom.build_track(segs)

    @staticmethod
    def split_missed(legs: List[Leg]) -> Tuple[List[Leg], List[Leg]]:
        """Split an approach at the runway end into approach / missed approach.

        Approach procedures code the go-around right behind the runway in the
        very same sequence, so the last point of the raw sequence is a G/A fix
        (a hold or a re-entry), **not** the end of the approach.  The runway leg
        is kept as the first point of the missed part so the go-around still has
        a start reference to draw from.
        """
        idx = None
        for i, leg in enumerate(legs):
            if leg.open:
                continue
            if RUNWAY_FIX_RE.match(leg.name or ""):
                idx = i
                break
        if idx is None:
            return list(legs), []
        return list(legs[:idx + 1]), list(legs[idx:])

    def track_split(
        self,
        runway: Optional[str] = None,
        transition: Optional[str] = None,
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]], Optional[Leg]]:
        """``(approach_track, missed_track, end_leg)``.

        ``approach_track`` stops at the runway threshold, ``missed_track``
        starts there; ``end_leg`` is the leg the approach terminates on.
        """
        segs, _, _, _ = self.expand_segments(runway, transition)
        if self.kind != "iap":
            return leggeom.build_track(segs), [], None
        approach, missed = self.split_missed(segs)
        end = next((l for l in reversed(approach) if not l.open), None)
        return (leggeom.build_track(approach),
                leggeom.build_track(missed) if missed else [],
                end)

    def _anchor_runway(self, legs: List[Leg], runway: Optional[str]) -> List[Leg]:
        """Attach the runway threshold when the coded legs do not reach it.

        A vector SID is published as "runway, then heading" and therefore has no
        fix at all; a vector STAR stops well before the airport.  In both cases
        the runway threshold from ``tbl_pg_runways`` is real data, not a guess.
        """
        if not runway:
            return legs
        point = self.runways_xy.get(runway)
        if point is None:
            return legs
        name = f"RW{runway}"
        threshold = Leg(name, float(point[0]), float(point[1]), "runway", name, "", "G")
        if self.kind == "iap":
            # The runway end (and the missed approach behind it) is part of the
            # published approach: never move the threshold to the end of the
            # list, only supply it when the coding omits it altogether.
            if any(not l.open and RUNWAY_FIX_RE.match(l.name or "") for l in legs):
                return legs
            return list(legs) + [threshold]
        positioned = [l for l in legs if not l.open]
        if not positioned:
            return [threshold] if self.kind == "sid" else list(legs) + [threshold]
        edge = positioned[0] if self.kind == "sid" else positioned[-1]
        if geo.dist((edge.lat, edge.lon), (threshold.lat, threshold.lon)) <= RUNWAY_SNAP_NM:
            return legs
        return [threshold] + legs if self.kind == "sid" else legs + [threshold]

    def fixes(
        self,
        runway: Optional[str] = None,
        transition: Optional[str] = None,
    ) -> List[Tuple[str, float, float]]:
        legs, _, _, _ = self.expand(runway, transition)
        return [(l.name, l.lat, l.lon) for l in legs]

    def parts_for(self, transition: str) -> List[Leg]:
        """Legs of one enroute transition, using the family an expansion picks."""
        family = self._pick_family(None, transition)
        pool = self.parts.get(family, {}).get("enroute", {})
        for trans, legs in pool.items():
            if _key(trans) == _key(transition):
                return list(legs)
        for trans, legs in pool.items():
            if _key(trans).endswith(_key(transition)):
                return list(legs)
        return []

    # -------------------------------------------------------------------- misc

    def summary(self) -> dict:
        """Compact description used by the listing endpoints."""
        out = {
            "name": self.name,
            "type": self.kind,
            "runways": self.runways,
            "transitions": self.transitions,
            "families": self.families,
            "single_sequence": self.single_sequence,
        }
        if self.kind == "iap":
            out["approach"] = self.approach
            out["approach_name"] = self.approach_name
            out["final_types"] = list(self.final_types)
        return out


def procedures_from_rows(kind: str, airport: str,
                         rows: Sequence[tuple],
                         runway_points: Optional[Dict[str, Tuple[float, float]]] = None
                         ) -> Dict[str, Procedure]:
    """Group ``airport``'s SID/STAR rows into :class:`Procedure` objects.

    Row layout (the canonical SELECT order)::

        0  procedure_identifier     6  waypoint_longitude     12 arc_radius
        1  seqno                    7  description code       13 center_waypoint_latitude
        2  route_type               8  path terminator        14 center_waypoint_longitude
        3  transition_identifier    9  course                 15 recommended_navaid
        4  waypoint_identifier     10  course_flag            16 recommended_navaid_latitude
        5  waypoint_latitude       11  rho / theta ...        17 recommended_navaid_longitude

    The exact tail is mapped in :func:`build_leg`; only the first nine columns
    are mandatory, older tuples keep working with the geometry fields empty.
    """
    buckets: Dict[str, List[tuple]] = {}
    for row in rows:
        name = (row[0] or "").strip().upper()
        if not name:
            continue
        buckets.setdefault(name, []).append(row[1:])
    return {
        name: Procedure(kind, airport, name, items, runway_points)
        for name, items in buckets.items()
    }
