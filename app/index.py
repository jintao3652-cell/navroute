"""In-memory navigation cycle index.

One ``NavCycle`` holds a fully materialised, read-only view of an AIRAC cycle:

* ``airways``  route identifier -> list of :class:`Chain`
* ``aw_pos``   route identifier -> fix name -> ((chain_idx, position), ...)
* ``fixes``    fix identifier -> ((lat, lon, kind), ...)
* ``airports`` ICAO -> (lat, lon, name)

SID/STAR procedures stay in SQLite (they are large and always requested by
primary key), but every lookup goes through a prepared index and an in-process
LRU, so a procedure expansion costs a fraction of a millisecond.
"""

from __future__ import annotations

import json
import os
import pickle
import sqlite3
import threading
import time
from collections import OrderedDict, namedtuple
from typing import Dict, List, Optional, Tuple

from . import config, geo
from . import airportinfo
from . import legtext
from . import legs as leggeom
from .procedures import PROC_TABLE, Procedure, normalize_runway, procedures_from_rows

CACHE_VERSION = 2
CACHE_FILE = "index.pkl"

# route, area, fixes, lats, lons, dirs
Chain = namedtuple("Chain", "route area fixes lats lons dirs")

KIND_RANK = {"E": 0, "D": 1, "N": 2, "R": 3, "A": 4, "C": 5}

# airport procedure cache: one entry holds every SID/STAR of one airport
_PROC_CACHE_MAX = max(8, config.PROC_CACHE)
_PC_CACHE_MAX = 4096


def legs_name(path: str) -> str:
    """Human readable name of an ARINC 424 path terminator."""
    return leggeom.name_of(path)


class CycleError(RuntimeError):
    pass


class NavCycle:
    """Read-only, thread-safe index for one AIRAC cycle."""

    def __init__(self, cycle: str, base_dir: str, meta: dict):
        self.cycle = cycle
        self.base_dir = base_dir
        self.meta = meta or {}
        blob = self._read_cache(base_dir)
        self.airways: Dict[str, List[Chain]] = blob["airways"]
        self.aw_pos: Dict[str, Dict[str, Tuple[Tuple[int, int], ...]]] = blob["aw_pos"]
        self.fixes: Dict[str, Tuple[Tuple[float, float, str], ...]] = blob["fixes"]
        self.airports: Dict[str, Tuple[float, float, str]] = blob["airports"]
        self.stats: dict = blob.get("stats", {})
        self.db_path = os.path.join(base_dir, blob["db_file"])

        self._local = threading.local()
        self._proc_lock = threading.Lock()
        self._proc_cache: "OrderedDict[tuple, dict]" = OrderedDict()
        self._pc_cache: "OrderedDict[str, tuple]" = OrderedDict()
        self._graph = None
        self._graph_lock = threading.Lock()
        self.loaded_at = time.time()
        self.opened = 0

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _read_cache(base_dir: str) -> dict:
        path = os.path.join(base_dir, CACHE_FILE)
        if not os.path.exists(path):
            raise CycleError(f"index cache missing for {base_dir}")
        with open(path, "rb") as fh:
            blob = pickle.load(fh)
        if blob.get("version") != CACHE_VERSION:
            raise CycleError("index cache version mismatch, please re-upload the cycle")
        return blob

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15.0)
            conn.execute("PRAGMA query_only=1")
            conn.execute("PRAGMA cache_size=-32000")
            self._local.conn = conn
            self._local.cur = conn.cursor()
            self.opened += 1
        return conn

    # --------------------------------------------------------------- lookups

    def is_airway(self, ident: str) -> bool:
        return ident in self.airways

    def airport(self, icao: str) -> Optional[Tuple[float, float, str]]:
        return self.airports.get(icao)

    def resolve_point(self, ident: str, near=None) -> Optional[Tuple[float, float]]:
        """Return (lat, lon) for a fix / navaid / airport identifier."""
        cands = self.fixes.get(ident)
        if cands:
            if len(cands) == 1 or near is None:
                best = min(cands, key=lambda c: KIND_RANK.get(c[2], 9))
            else:
                best = min(cands, key=lambda c: geo.haversine(near[0], near[1], c[0], c[1]))
            return (best[0], best[1])
        apt = self.airports.get(ident)
        if apt:
            return (apt[0], apt[1])
        cands = self._terminal_fixes(ident)
        if cands:
            if len(cands) == 1 or near is None:
                return cands[0]
            return min(cands, key=lambda c: geo.haversine(near[0], near[1], c[0], c[1]))
        return None

    def _terminal_fixes(self, ident: str) -> tuple:
        """Terminal (PC) fixes, looked up lazily and memoised."""
        hit = self._pc_cache.get(ident)
        if hit is not None:
            return hit
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT waypoint_latitude, waypoint_longitude FROM tbl_pc_terminal_waypoints "
                "WHERE waypoint_identifier=?",
                (ident,),
            )
            rows = tuple((r[0], r[1]) for r in cur.fetchall() if r[0] is not None)
        except sqlite3.Error:
            rows = ()
        with self._proc_lock:
            self._pc_cache[ident] = rows
            if len(self._pc_cache) > _PC_CACHE_MAX:
                self._pc_cache.popitem(last=False)
        return rows

    # ------------------------------------------------------------ procedures

    def _load_airport_procedures(self, kind: str, apt: str) -> Dict[str, Procedure]:
        """Every SID (or STAR) of one airport, in a single query, LRU cached."""
        key = (kind, apt)
        with self._proc_lock:
            hit = self._proc_cache.get(key)
            if hit is not None:
                self._proc_cache.move_to_end(key)
                return hit
        table = PROC_TABLE[kind]
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT procedure_identifier, seqno, route_type, transition_identifier, "
                "       waypoint_identifier, waypoint_latitude, waypoint_longitude, "
                "       waypoint_description_code, path_termination, "
                "       course, course_flag, rho, theta, turn_direction, arc_radius, "
                "       center_waypoint_latitude, center_waypoint_longitude, "
                "       recommended_navaid, recommended_navaid_latitude, "
                "       recommended_navaid_longitude, "
                "       altitude_description, altitude1, altitude2, "
                "       distance_time, route_distance_holding_distance_time, "
                "       center_waypoint, speed_limit, speed_limit_description, "
                "       vertical_angle, rnp "
                f"FROM {table} WHERE airport_identifier=? "
                "ORDER BY procedure_identifier, route_type, seqno",
                (apt,),
            )
            rows = cur.fetchall()
        except sqlite3.Error:
            rows = []
        procs = procedures_from_rows(kind, apt, rows, self.runways(apt))
        with self._proc_lock:
            self._proc_cache[key] = procs
            if len(self._proc_cache) > _PROC_CACHE_MAX:
                self._proc_cache.popitem(last=False)
        return procs

    def procedures(self, kind: str, apt: str) -> Dict[str, Procedure]:
        return self._load_airport_procedures(kind, apt)

    def procedure(self, kind: str, apt: str, proc: str) -> Optional[Procedure]:
        return self._load_airport_procedures(kind, apt).get((proc or "").strip().upper())

    def has_procedure(self, kind: str, apt: str, proc: str) -> bool:
        return self.procedure(kind, apt, proc) is not None

    def runways(self, apt: str) -> Dict[str, Tuple[float, float]]:
        """Runway designator -> threshold (lat, lon), from ``tbl_pg_runways``."""
        key = ("runways", apt)
        with self._proc_lock:
            hit = self._proc_cache.get(key)
            if hit is not None:
                self._proc_cache.move_to_end(key)
                return hit
        out: Dict[str, Tuple[float, float]] = {}
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT runway_identifier, runway_latitude, runway_longitude "
                "FROM tbl_pg_runways WHERE airport_identifier=?",
                (apt,),
            )
            for ident, lat, lon in cur.fetchall():
                rw = normalize_runway(ident)
                if rw and lat is not None and lon is not None:
                    out.setdefault(rw, (float(lat), float(lon)))
        except sqlite3.Error:
            pass
        with self._proc_lock:
            self._proc_cache[key] = out
            if len(self._proc_cache) > _PROC_CACHE_MAX:
                self._proc_cache.popitem(last=False)
        return out

    def airport_info(self, apt: str) -> Optional[dict]:
        """Airport + runway + ILS facts, one SQLite round trip, LRU cached."""
        if apt not in self.airports:
            return None
        key = ("airport", apt)
        with self._proc_lock:
            hit = self._proc_cache.get(key)
            if hit is not None:
                self._proc_cache.move_to_end(key)
                return hit
        data = airportinfo.build(self, apt)
        with self._proc_lock:
            self._proc_cache[key] = data
            if len(self._proc_cache) > _PROC_CACHE_MAX:
                self._proc_cache.popitem(last=False)
        return data

    def runways_compact(self, apt: str) -> List[dict]:
        """Runway summary (heading / length / ILS idents) for embedding."""
        info = self.airport_info(apt)
        if not info:
            return []
        return [airportinfo.compact(r) for r in info["runways"]]

    def procedure_names(self, kind: str, apt: str) -> List[str]:
        return sorted(self._load_airport_procedures(kind, apt))

    def procedure_fixes(
        self,
        kind: str,
        apt: str,
        proc: str,
        runway: Optional[str] = None,
        exit_fix: Optional[str] = None,
        transition: Optional[str] = None,
    ) -> Optional[List[Tuple[str, float, float]]]:
        """Expand a SID/STAR into an ordered list of (ident, lat, lon)."""
        p = self.procedure(kind, apt, proc)
        if p is None:
            return None
        legs, _, _, _ = p.expand(runway, transition or exit_fix)
        if not legs:
            return None
        return [(l.name, l.lat, l.lon) for l in legs]

    def procedure_detail(self, kind: str, apt: str, proc: str,
                         runway: Optional[str], transition: Optional[str]) -> Optional[dict]:
        p = self.procedure(kind, apt, proc)
        if p is None:
            return None
        legs, rw, tr, notes = p.expand(runway, transition)
        segments, _, _, _ = p.expand_segments(runway, transition)
        track, missed_track, end_leg = p.track_split(runway, transition)
        if p.kind == "iap":
            # The approach ends at the runway: the go-around coded behind it is
            # a separate sequence, never the end point of the procedure.
            leg_list, missed_list = p.split_missed(legs)
            missed_list = missed_list[1:] if missed_list else []
        else:
            leg_list, missed_list = legs, []
        data = {
            "name": p.name,
            "type": p.kind,
            "airport": apt,
            "runway": rw,
            "transition": tr,
            "runways": p.runways,
            "transitions": p.transitions,
            "families": p.families,
            "warnings": notes,
            "leg_types": sorted({l.path for l in segments if l.path}),
            "waypoints": [
                {
                    "name": l.name,
                    "lat": round(l.lat, 8),
                    "lon": round(l.lon, 8),
                    "role": l.role,
                    "path": l.path,
                }
                for l in leg_list
            ],
            "segments": legtext.segments(segments),
            "track": [[round(lat, 6), round(lon, 6)] for lat, lon in track],
            "missed_track": [[round(lat, 6), round(lon, 6)] for lat, lon in missed_track],
        }
        if end_leg is not None:
            data["approach_end"] = {
                "name": end_leg.name,
                "lat": round(end_leg.lat, 8),
                "lon": round(end_leg.lon, 8),
                "role": end_leg.role,
            }
        if p.kind == "iap":
            data["approach"] = p.approach
            data["approach_name"] = p.approach_name
            data["final_types"] = list(p.final_types)
            data["missed"] = len(missed_list)
            data["missed_waypoints"] = [
                {
                    "name": l.name,
                    "lat": round(l.lat, 8),
                    "lon": round(l.lon, 8),
                    "role": l.role,
                    "path": l.path,
                }
                for l in missed_list
            ]
        return data

    # ---------------------------------------------------------------- graph

    def graph(self):
        """Lazily build the enroute connectivity graph used by /api/efb/route."""
        if self._graph is not None:
            return self._graph
        with self._graph_lock:
            if self._graph is None:
                from .finder import RouteGraph

                self._graph = RouteGraph(self)
        return self._graph


# --------------------------------------------------------------------- manager


class CycleManager:
    """Stores every installed cycle on disk, keeps the hot ones in memory."""

    def __init__(self, data_dir=None, max_loaded: int = 0, default_cycle: str = ""):
        self.data_dir = str(data_dir or config.DATA_DIR)
        self.cycles_dir = os.path.join(self.data_dir, "cycles")
        self.registry_file = os.path.join(self.data_dir, "registry.json")
        self.max_loaded = max_loaded or config.MAX_LOADED_CYCLES
        self.default_cycle = default_cycle or config.DEFAULT_CYCLE
        self._lock = threading.RLock()
        self._loaded: "OrderedDict[str, NavCycle]" = OrderedDict()
        # A read-only data dir must not kill the process at import time; the
        # lifespan probe in main.py flags the service as degraded instead.
        try:
            os.makedirs(self.cycles_dir, exist_ok=True)
        except OSError:
            pass
        self._registry = self._read_registry()

    # ---------------------------------------------------------------- files

    def _read_registry(self) -> dict:
        try:
            with open(self.registry_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict) and isinstance(data.get("cycles"), dict):
                    return data
        except (OSError, ValueError):
            pass
        return {"default": self.default_cycle or "", "cycles": {}}

    def save_registry(self) -> None:
        with self._lock:
            payload = json.dumps(self._registry, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.registry_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, self.registry_file)

    # ------------------------------------------------------------- registry

    def cycles(self) -> List[dict]:
        with self._lock:
            items = [dict(v, cycle=k) for k, v in self._registry["cycles"].items()]
        items.sort(key=lambda c: (c.get("valid_from") or "", c.get("cycle") or ""), reverse=True)
        return items

    def entry(self, cycle: str) -> Optional[dict]:
        with self._lock:
            val = self._registry["cycles"].get(cycle)
            return dict(val, cycle=cycle) if val else None

    def register(self, cycle: str, entry: dict) -> None:
        with self._lock:
            self._registry["cycles"][cycle] = entry
            if not self._registry.get("default") or not config.DEFAULT_CYCLE:
                self._registry["default"] = self._pick_default()
        self.save_registry()

    def unregister(self, cycle: str) -> bool:
        with self._lock:
            existed = self._registry["cycles"].pop(cycle, None) is not None
            if not existed:
                return False
            if self._registry.get("default") == cycle:
                self._registry["default"] = self._pick_default()
            old = self._loaded.pop(cycle, None)
        if old is not None:
            old.close()
        self.save_registry()
        return True

    def _pick_default(self) -> str:
        """Prefer a cycle that is valid today, otherwise the newest one."""
        best, best_key = "", None
        today = time.strftime("%Y-%m-%d")
        for cycle, entry in self._registry.get("cycles", {}).items():
            vf = entry.get("valid_from") or ""
            vt = entry.get("valid_to") or ""
            active = bool(vf and vt and vf <= today <= vt)
            key = (1 if active else 0, vt or "", cycle)
            if best_key is None or key > best_key:
                best, best_key = cycle, key
        return best

    def default(self) -> str:
        with self._lock:
            if self.default_cycle and self.default_cycle in self._registry["cycles"]:
                return self.default_cycle
            cur = self._registry.get("default") or ""
            if cur in self._registry["cycles"]:
                return cur
            return self._pick_default()

    # --------------------------------------------------------------- loading

    def get(self, cycle: Optional[str] = None) -> NavCycle:
        cycle = (cycle or "").strip() or self.default()
        if not cycle:
            raise CycleError("no navigation cycle installed, upload one first")
        with self._lock:
            entry = self._registry["cycles"].get(cycle)
            if entry is None:
                raise CycleError(f"cycle {cycle} is not installed")
            hit = self._loaded.get(cycle)
            if hit is not None:
                self._loaded.move_to_end(cycle)
                return hit
        base_dir = os.path.join(self.cycles_dir, cycle)
        obj = NavCycle(cycle, base_dir, entry)
        with self._lock:
            again = self._loaded.get(cycle)
            if again is not None:
                obj.close()
                self._loaded.move_to_end(cycle)
                return again
            self._loaded[cycle] = obj
            while len(self._loaded) > max(1, self.max_loaded):
                _, victim = self._loaded.popitem(last=False)
                victim.close()
        return obj

    def loaded_cycles(self) -> List[str]:
        with self._lock:
            return list(self._loaded.keys())

    def unload(self, cycle: str) -> bool:
        with self._lock:
            obj = self._loaded.pop(cycle, None)
        if obj is None:
            return False
        obj.close()
        return True

    def preload(self, cycle: Optional[str] = None) -> Optional[NavCycle]:
        try:
            return self.get(cycle)
        except Exception:
            return None
