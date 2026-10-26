"""Runtime configuration (all values can be overridden by environment variables)."""

from __future__ import annotations

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


DATA_DIR = Path(os.environ.get("NRS_DATA_DIR", "/data")).expanduser().resolve()
CYCLES_DIR = DATA_DIR / "cycles"
TMP_DIR = DATA_DIR / "tmp"
REGISTRY_FILE = DATA_DIR / "registry.json"

# How many cycles are kept fully indexed in RAM at the same time (LRU).
MAX_LOADED_CYCLES = _int("NRS_MAX_LOADED_CYCLES", 3)
# Cycle used when the request does not specify one.
DEFAULT_CYCLE = os.environ.get("NRS_DEFAULT_CYCLE", "").strip()
# Warm the default cycle index during container start-up.
PRELOAD = _bool("NRS_PRELOAD", True)
# Also build the airway graph for the warmed cycle (first /api/efb/route call
# then costs ~14 ms instead of ~360 ms).
PRELOAD_GRAPH = _bool("NRS_PRELOAD_GRAPH", True)

# Rebuild the in-memory index straight after a successful upload: the new
# cycle is live immediately, without a restart and without a cold first call.
REBUILD_ON_UPLOAD = _bool("NRS_REBUILD_ON_UPLOAD", True)
# Also rebuild the airway graph during that post-upload rebuild (the first
# /api/efb/route call on the fresh cycle then costs ~5 ms instead of ~360 ms).
REBUILD_GRAPH_ON_UPLOAD = _bool("NRS_REBUILD_GRAPH_ON_UPLOAD", True)

# /api/efb/route: airports are connected to enroute fixes inside this radius.
CONNECT_RADIUS_NM = _float("NRS_CONNECT_RADIUS_NM", 250.0)
ROUTE_MAX_EXPAND = _int("NRS_ROUTE_MAX_EXPAND", 400000)
ROUTE_TIME_LIMIT = _float("NRS_ROUTE_TIME_LIMIT", 5.0)

# SID/STAR: how many airports keep their procedure rows cached in RAM.
PROC_CACHE = _int("NRS_PROC_CACHE", 512)

# Upload guard (bytes).
MAX_UPLOAD_BYTES = _int("NRS_MAX_UPLOAD_MB", 4096) * 1024 * 1024

# Every POST request must carry this token (query string, body field or header).
# Empty string disables the guard.
TOKEN = os.environ.get("NRS_TOKEN", "admin123456").strip()

# Web console login (the HTML page only; the JSON API keeps using TOKEN).
ADMIN_USER = os.environ.get("NRS_ADMIN_USER", "admin").strip()
ADMIN_PASS = os.environ.get("NRS_ADMIN_PASS", "admin123456")
# Session lifetime in seconds and cookie flags.
SESSION_TTL = _int("NRS_SESSION_TTL", 12 * 3600)
COOKIE_SECURE = _bool("NRS_COOKIE_SECURE", False)
SESSION_COOKIE = "nrs_sid"
# Brute-force guard: failures per client IP before a cool-down kicks in.
LOGIN_MAX_FAILS = _int("NRS_LOGIN_MAX_FAILS", 8)
LOGIN_LOCK_SECONDS = _int("NRS_LOGIN_LOCK_SECONDS", 60)
# Trust X-Forwarded-For for the login rate limit (only behind a real proxy).
TRUST_PROXY = _bool("NRS_TRUST_PROXY", False)

# Overridden at start-up: True when the data directory cannot be written
# (read-only mount, wrong volume ownership, ...).  Queries keep working, the
# write endpoints answer 503 instead of restarting the container in a loop.
READ_ONLY = False
