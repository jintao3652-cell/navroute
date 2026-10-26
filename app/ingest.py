"""Ingest a navigation data package and pre-build every index it needs.

Ingest is the only slow operation of the service; it happens once per uploaded
cycle.  Everything that a route request needs afterwards is either written to
``index.pkl`` (loaded in one read) or backed by a prepared SQLite index.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import pickle
import re
import shutil
import sqlite3
import threading
import tempfile
import time
import zipfile
from typing import Dict, List, Optional, Tuple

from . import geo
from .index import CACHE_FILE, CACHE_VERSION, Chain

log = logging.getLogger("navroute.ingest")

# one ingest at a time keeps the registry and the data directory consistent
INGEST_LOCK = threading.Lock()

REQUIRED_TABLES = {
    "tbl_hdr_header",
    "tbl_er_enroute_airways",
    "tbl_ea_enroute_waypoints",
    "tbl_pa_airports",
}

# Chain splitting heuristics (see README, "航路分链").
MAX_LEG_NM = 2000.0
SUSPECT_LEG_NM = 150.0
SUSPECT_RATIO = 5.0

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# AIRAC 2401 became effective on 2024-01-25; cycles are 28 days apart.
_AIRAC_BASE_INDEX = 24 * 13 + 1
_AIRAC_BASE_DATE = _dt.date(2024, 1, 25)


class IngestError(RuntimeError):
    pass


# --------------------------------------------------------------------- cycle


def airac_period(cycle: str) -> Tuple[Optional[str], Optional[str]]:
    """Derive the 28-day validity window from an AIRAC cycle number."""
    if not cycle or len(cycle) != 4 or not cycle.isdigit():
        return (None, None)
    year = 2000 + int(cycle[:2])
    seq = int(cycle[2:])
    if not 1 <= seq <= 14:
        return (None, None)
    idx = year * 13 + seq
    offset = (idx - _AIRAC_BASE_INDEX) * 28
    start = _AIRAC_BASE_DATE + _dt.timedelta(days=offset)
    end = start + _dt.timedelta(days=27)
    return (start.isoformat(), end.isoformat())


def _parse_dmy(text: str) -> Optional[str]:
    """``01/OCT/2026`` -> ``2026-10-01``."""
    m = re.match(r"^\s*(\d{1,2})[/-]([A-Za-z]{3})[/-](\d{4})\s*$", text or "")
    if not m:
        return None
    day, mon, year = int(m.group(1)), MONTHS.get(m.group(2).upper()), int(m.group(3))
    if not mon:
        return None
    try:
        return _dt.date(year, mon, day).isoformat()
    except ValueError:
        return None


def _parse_cycle_info(text: str) -> dict:
    out: dict = {}
    m = re.search(r"cycle\s*[:=]\s*(\d{4})", text or "", re.I)
    if m:
        out["cycle"] = m.group(1)
    m = re.search(r"version\s*[:=]\s*(\d+)", text or "", re.I)
    if m:
        out["revision"] = m.group(1)
    m = re.search(r"\(\s*from\s*/\s*to\s*\)\s*:\s*([^-\n]+?)\s*-\s*([^\n]+)", text or "", re.I)
    if not m:
        m = re.search(r"(\d{1,2}[/-][A-Za-z]{3}[/-]\d{4})\s*-\s*(\d{1,2}[/-][A-Za-z]{3}[/-]\d{4})", text or "")
    if m:
        vf, vt = _parse_dmy(m.group(1)), _parse_dmy(m.group(2))
        if vf:
            out["valid_from"] = vf
        if vt:
            out["valid_to"] = vt
    return out


def _parse_effective_fromto(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """``0110281026`` -> (2026-10-01, 2026-10-28)."""
    raw = (raw or "").strip()
    if len(raw) != 10 or not raw.isdigit():
        return (None, None)
    try:
        d1, m1, d2, m2, yy = int(raw[0:2]), int(raw[2:4]), int(raw[4:6]), int(raw[6:8]), int(raw[8:10])
        start = _dt.date(2000 + yy, m1, d1)
        end = _dt.date(2000 + yy, m2, d2)
    except ValueError:
        return (None, None)
    return (start.isoformat(), end.isoformat())


def detect_cycle(db_path: str, extra_files: Dict[str, str], filename: str) -> dict:
    """Figure out which AIRAC cycle a package carries.

    Priority: cycle.json -> cycle_info.txt -> table header -> file name.
    """
    meta: dict = {"sources": []}

    cyc_json = extra_files.get("cycle.json")
    if cyc_json:
        try:
            with open(cyc_json, "r", encoding="utf-8-sig") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                meta["sources"].append("cycle.json")
                meta["cycle"] = str(payload.get("cycle") or "").strip()
                meta["revision"] = str(payload.get("revision") or "").strip() or None
                meta["provider"] = (payload.get("provider") or "").strip() or None
                meta["dataset"] = (payload.get("dataset") or payload.get("name") or "").strip() or None
                meta["format"] = (payload.get("format") or "").strip() or None
                meta["created_on"] = (payload.get("createdOn") or "").strip() or None
                period = (payload.get("validityPeriod") or "").strip()
                if "/" in period:
                    vf, vt = period.split("/", 1)
                    meta["valid_from"], meta["valid_to"] = vf.strip(), vt.strip()
        except (OSError, ValueError):
            pass

    info = extra_files.get("cycle_info.txt")
    if info:
        try:
            with open(info, "r", encoding="utf-8-sig", errors="replace") as fh:
                parsed = _parse_cycle_info(fh.read())
            if parsed:
                meta["sources"].append("cycle_info.txt")
                for key in ("cycle", "revision", "valid_from", "valid_to"):
                    if parsed.get(key) and not meta.get(key):
                        meta[key] = parsed[key]
        except OSError:
            pass

    header = _read_header(db_path)
    if header:
        meta["sources"].append("tbl_hdr_header")
        if not meta.get("cycle"):
            meta["cycle"] = (header.get("cycle") or "").strip()
        if not meta.get("revision"):
            meta["revision"] = (header.get("revision") or "").strip() or None
        if not meta.get("provider"):
            meta["provider"] = (header.get("data_provider") or "").strip() or None
        if not meta.get("valid_from"):
            vf, vt = _parse_effective_fromto(header.get("effective_fromto") or "")
            meta["valid_from"], meta["valid_to"] = vf, vt

    if not meta.get("cycle"):
        m = re.search(r"(?<!\d)(\d{4})(?!\d)", filename or "")
        if m and 1 <= int(m.group(1)[2:]) <= 14:
            meta["cycle"] = m.group(1)
            meta["sources"].append("filename")

    if not meta.get("cycle"):
        raise IngestError(
            "无法识别 AIRAC 周期：压缩包内缺少 cycle.json / cycle_info.txt，"
            "数据库表头也没有 cycle 字段"
        )

    cycle = meta["cycle"]
    if not re.fullmatch(r"\d{4}", cycle):
        raise IngestError(f"周期号格式非法: {cycle!r}")

    if not meta.get("valid_from") or not meta.get("valid_to"):
        vf, vt = airac_period(cycle)
        meta.setdefault("valid_from", vf)
        meta.setdefault("valid_to", vt)
    return meta


def _read_header(db_path: str) -> dict:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tbl_hdr_header LIMIT 1").fetchone()
        conn.close()
        return dict(row) if row else {}
    except sqlite3.Error:
        return {}


# ------------------------------------------------------------------ packages


def _safe_extract(zip_path: str, dest: str) -> Dict[str, str]:
    files: Dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            base = os.path.basename(name)
            if not base or base.startswith("._") or "__MACOSX" in name:
                continue
            target = os.path.realpath(os.path.join(dest, base))
            if not target.startswith(os.path.realpath(dest) + os.sep):
                continue
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            files.setdefault(base.lower(), target)
    return files


def _pick_database(files: Dict[str, str]) -> str:
    candidates = [
        p for name, p in files.items()
        if name.endswith((".s3db", ".sqlite", ".sqlite3", ".db"))
    ]
    if not candidates:
        raise IngestError("压缩包内没有找到导航数据库文件（*.s3db）")
    candidates.sort(key=os.path.getsize, reverse=True)
    return candidates[0]


def _validate_database(db_path: str) -> None:
    try:
        conn = sqlite3.connect(db_path)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
    except sqlite3.Error as exc:
        raise IngestError(f"数据库无法打开: {exc}") from exc
    missing = REQUIRED_TABLES - names
    if missing:
        raise IngestError(f"数据库缺少必需的表: {', '.join(sorted(missing))}")


def _remove_dir(path: str) -> None:
    """rmtree with a few retries: Windows may hold a handle for a moment."""
    last: Optional[OSError] = None
    for attempt in range(6):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last = exc
            if os.name != "nt":
                raise
            time.sleep(0.05 * (attempt + 1))
    shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        raise IngestError(f"无法删除旧周期目录 {path}: {last}")


def _replace_dir(src: str, dst: str) -> None:
    """Atomically swap ``src`` into place as ``dst`` (Windows tolerant).

    ``os.replace`` on a directory can fail with WinError 5/32 while an
    antivirus scanner or the indexer still holds a handle on a file that was
    just written inside the staging directory.  Retry briefly, then fall back
    to copying the tree so an upload never dies on a transient lock.
    """
    last: Optional[OSError] = None
    for attempt in range(6):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last = exc
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 183):
                break
            time.sleep(0.05 * (attempt + 1))
    log.warning("atomic swap %s -> %s failed (%s), falling back to copy", src, dst, last)
    os.makedirs(dst, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    shutil.rmtree(src, ignore_errors=True)


# --------------------------------------------------------------------- index


def _create_indexes(conn: sqlite3.Connection) -> List[str]:
    """Build every index the query path relies on (idempotent)."""
    stmts = [
        ("ix_nrs_er_route", "tbl_er_enroute_airways", "route_identifier, seqno"),
        ("ix_nrs_ea_wpt", "tbl_ea_enroute_waypoints", "waypoint_identifier"),
        ("ix_nrs_pa_apt", "tbl_pa_airports", "airport_identifier"),
        ("ix_nrs_d_nav", "tbl_d_vhfnavaids", "navaid_identifier"),
        ("ix_nrs_db_nav", "tbl_db_enroute_ndbnavaids", "navaid_identifier"),
        ("ix_nrs_pc_wpt", "tbl_pc_terminal_waypoints", "waypoint_identifier"),
        ("ix_nrs_pd_proc", "tbl_pd_sids", "airport_identifier, procedure_identifier, seqno"),
        ("ix_nrs_pe_proc", "tbl_pe_stars", "airport_identifier, procedure_identifier, seqno"),
        # approaches (392 k rows) are always fetched per airport
        ("ix_nrs_pf_proc", "tbl_pf_iaps", "airport_identifier, procedure_identifier, seqno"),
        # runways / landing aids: always fetched per airport
        ("ix_nrs_pg_rwy", "tbl_pg_runways", "airport_identifier"),
        ("ix_nrs_pi_llz", "tbl_pi_localizers_glideslopes", "airport_identifier"),
        ("ix_nrs_pm_mkr", "tbl_pm_localizer_marker", "airport_identifier"),
    ]
    existing = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    created: List[str] = []
    for name, table, cols in stmts:
        if name in existing:
            continue
        try:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({cols})")
            created.append(name)
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            log.warning("index %s on %s failed: %s", name, table, exc)
    conn.commit()
    return created


def ensure_indexes(db_path: str) -> List[str]:
    """Create the query indexes on an already-installed cycle (best effort).

    Cycles ingested by an older version have no index on ``tbl_pf_iaps``, which
    turns every approach lookup into a 390 k row scan.  Called once per cycle
    from the background warm-up thread so an upgrade needs no re-upload.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
    except sqlite3.Error:
        return []
    try:
        return _create_indexes(conn)
    except sqlite3.Error as exc:
        log.warning("cannot create indexes on %s: %s", db_path, exc)
        return []
    finally:
        conn.close()


def _split_group(route: str, area: str, rows: List[tuple]) -> List[Chain]:
    chains: List[Chain] = []
    fixes: List[str] = []
    lats: List[float] = []
    lons: List[float] = []
    dirs: List[str] = []
    prev_pt = None
    prev_seq = None

    def flush() -> None:
        nonlocal fixes, lats, lons, dirs
        if len(fixes) >= 2:
            chains.append(Chain(route, area, tuple(fixes), tuple(lats), tuple(lons), tuple(dirs)))
        fixes, lats, lons, dirs = [], [], [], []

    for seq, fix, lat, lon, ind, dirn in rows:
        brk = False
        if prev_pt is not None:
            d = geo.haversine(prev_pt[0], prev_pt[1], lat, lon)
            if seq == prev_seq:
                brk = True
            elif d > MAX_LEG_NM:
                brk = True
            elif ind and d > SUSPECT_LEG_NM and d > SUSPECT_RATIO * ind:
                brk = True
        if brk:
            flush()
        if fixes and fixes[-1] == fix and abs(lats[-1] - lat) < 1e-7:
            prev_pt, prev_seq = (lat, lon), seq
            continue
        fixes.append(fix)
        lats.append(round(float(lat), 8))
        lons.append(round(float(lon), 8))
        dirs.append((dirn or "").strip()[:1] if dirn else "")
        prev_pt, prev_seq = (lat, lon), seq
    flush()
    return chains


def build_index(conn: sqlite3.Connection) -> dict:
    t0 = time.perf_counter()
    cur = conn.cursor()

    # ---- enroute airways -------------------------------------------------
    cur.execute(
        "SELECT route_identifier, area_code, route_identifier_postfix, seqno, "
        "       waypoint_identifier, waypoint_latitude, waypoint_longitude, "
        "       direction_restriction, inbound_distance "
        "FROM tbl_er_enroute_airways "
        "WHERE waypoint_identifier IS NOT NULL AND waypoint_identifier <> '' "
        "  AND waypoint_latitude IS NOT NULL AND waypoint_longitude IS NOT NULL "
        "ORDER BY route_identifier, route_identifier_postfix, area_code, seqno"
    )
    airways: Dict[str, List[Chain]] = {}
    group_key = None
    bucket: List[tuple] = []

    def flush_group() -> None:
        if not bucket:
            return
        route, area = group_key[0], group_key[2]
        for chain in _split_group(route, area, bucket):
            airways.setdefault(route, []).append(chain)

    for row in cur:
        route = (row[0] or "").strip().upper()
        if not route:
            continue
        key = (route, (row[2] or "").strip().upper(), (row[1] or "").strip())
        if key != group_key:
            flush_group()
            group_key, bucket = key, []
        bucket.append((row[3], (row[4] or "").strip().upper(), row[5], row[6], row[8], row[7]))
    flush_group()

    # ---- position index for O(1) "is this fix on that airway" ------------
    aw_pos: Dict[str, Dict[str, Tuple[Tuple[int, int], ...]]] = {}
    for route, chains in airways.items():
        pos: Dict[str, List[Tuple[int, int]]] = {}
        for ci, chain in enumerate(chains):
            for pi, fix in enumerate(chain.fixes):
                pos.setdefault(fix, []).append((ci, pi))
        aw_pos[route] = {k: tuple(v) for k, v in pos.items()}

    # ---- fixes -----------------------------------------------------------
    fixes: Dict[str, List[Tuple[float, float, str]]] = {}

    def add_fix(name, lat, lon, kind) -> None:
        name = (name or "").strip().upper()
        if not name or lat is None or lon is None:
            return
        try:
            lat = round(float(lat), 8)
            lon = round(float(lon), 8)
        except (TypeError, ValueError):
            return
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return
        item = (lat, lon, kind)
        bucket_ = fixes.get(name)
        if bucket_ is None:
            fixes[name] = [item]
        elif item not in bucket_:
            bucket_.append(item)

    cur.execute("SELECT waypoint_identifier, waypoint_latitude, waypoint_longitude "
                "FROM tbl_ea_enroute_waypoints")
    for r in cur:
        add_fix(r[0], r[1], r[2], "E")
    cur.execute("SELECT navaid_identifier, navaid_latitude, navaid_longitude "
                "FROM tbl_d_vhfnavaids")
    for r in cur:
        add_fix(r[0], r[1], r[2], "D")
    cur.execute("SELECT navaid_identifier, navaid_latitude, navaid_longitude "
                "FROM tbl_db_enroute_ndbnavaids")
    for r in cur:
        add_fix(r[0], r[1], r[2], "N")
    # Every fix that appears on an airway is a valid route element even when it
    # has no standalone waypoint record (kind "R" = airway reference).
    for chains in airways.values():
        for chain in chains:
            for k, fix in enumerate(chain.fixes):
                add_fix(fix, chain.lats[k], chain.lons[k], "R")

    # ---- airports --------------------------------------------------------
    airports: Dict[str, Tuple[float, float, str]] = {}
    cur.execute("SELECT airport_identifier, airport_ref_latitude, airport_ref_longitude, "
                "airport_name FROM tbl_pa_airports "
                "WHERE airport_ref_latitude IS NOT NULL AND airport_ref_longitude IS NOT NULL")
    for r in cur:
        ident = (r[0] or "").strip().upper()
        if ident:
            airports[ident] = (round(float(r[1]), 8), round(float(r[2]), 8), (r[3] or "").strip())

    total_legs = sum(len(c.fixes) for cs in airways.values() for c in cs)
    stats = {
        "routes": len(airways),
        "chains": sum(len(cs) for cs in airways.values()),
        "airway_legs": total_legs,
        "fixes": len(fixes),
        "fix_points": sum(len(v) for v in fixes.values()),
        "airports": len(airports),
        "build_ms": int((time.perf_counter() - t0) * 1000),
    }
    log.info("index built: %s", stats)
    return {
        "airways": airways,
        "aw_pos": aw_pos,
        "fixes": {k: tuple(v) for k, v in fixes.items()},
        "airports": airports,
        "stats": stats,
    }


# -------------------------------------------------------------------- ingest


def ingest(package_path: str, filename: str, data_dir: str, overwrite: bool = False,
           before_replace=None) -> dict:
    with INGEST_LOCK:
        return _ingest(package_path, filename, data_dir, overwrite, before_replace)


def _ingest(package_path: str, filename: str, data_dir: str, overwrite: bool,
            before_replace) -> dict:
    t0 = time.perf_counter()
    data_dir = os.path.abspath(data_dir)
    cycles_dir = os.path.join(data_dir, "cycles")
    tmp_root = os.path.join(data_dir, "tmp")
    os.makedirs(cycles_dir, exist_ok=True)
    os.makedirs(tmp_root, exist_ok=True)
    work = tempfile.mkdtemp(prefix="ingest-", dir=tmp_root)
    staged: Optional[str] = None
    try:
        if zipfile.is_zipfile(package_path):
            files = _safe_extract(package_path, work)
        else:
            base = os.path.basename(package_path)
            files = {base.lower(): package_path}
        db_path = _pick_database(files)
        _validate_database(db_path)

        meta = detect_cycle(db_path, files, filename or os.path.basename(db_path))
        cycle = meta["cycle"]
        target_dir = os.path.join(cycles_dir, cycle)
        if os.path.exists(target_dir) and not overwrite:
            raise IngestError(f"cycle {cycle} 已存在，如需覆盖请设置 overwrite=true")

        staged = tempfile.mkdtemp(prefix=f"cycle-{cycle}-", dir=tmp_root)
        db_name = os.path.basename(db_path)
        if not db_name:
            db_name = f"ng_jeppesen_fwdfd_{cycle}.s3db"
        final_db = os.path.join(staged, db_name)
        if os.path.abspath(db_path) != os.path.abspath(final_db):
            shutil.copyfile(db_path, final_db)
        for extra in ("cycle.json", "cycle_info.txt"):
            src = files.get(extra)
            if src:
                shutil.copyfile(src, os.path.join(staged, extra))

        conn = sqlite3.connect(final_db)
        try:
            _create_indexes(conn)
            blob = build_index(conn)
        finally:
            conn.close()
        blob.update({"version": CACHE_VERSION, "cycle": cycle, "db_file": db_name})
        cache_tmp = os.path.join(staged, CACHE_FILE + ".tmp")
        with open(cache_tmp, "wb") as fh:
            pickle.dump(blob, fh, protocol=4)
        os.replace(cache_tmp, os.path.join(staged, CACHE_FILE))

        size_bytes = os.path.getsize(final_db)
        entry = {
            "cycle": cycle,
            "revision": meta.get("revision"),
            "provider": meta.get("provider"),
            "dataset": meta.get("dataset"),
            "format": meta.get("format"),
            "valid_from": meta.get("valid_from"),
            "valid_to": meta.get("valid_to"),
            "created_on": meta.get("created_on"),
            "meta_sources": meta.get("sources", []),
            "db_file": db_name,
            "db_size": size_bytes,
            "cache_size": os.path.getsize(os.path.join(staged, CACHE_FILE)),
            "installed_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stats": blob["stats"],
        }
        with open(os.path.join(staged, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False, indent=2)

        if os.path.exists(target_dir):
            if before_replace is not None:
                before_replace(cycle)
            _remove_dir(target_dir)
        _replace_dir(staged, target_dir)
        staged = None

        entry["ingest_ms"] = int((time.perf_counter() - t0) * 1000)
        return entry
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if staged and os.path.exists(staged):
            shutil.rmtree(staged, ignore_errors=True)
