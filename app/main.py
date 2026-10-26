"""Navigraph DFD route service (FastAPI).

Endpoints
---------
POST /api/efb/parseRoute   route string -> waypoints (incl. SID/STAR)
POST /api/efb/route        dep/arr -> suggested route (incl. SID/STAR)
GET  /api/efb/procedures   list the SID / STAR / APPROACH of an airport
GET  /api/efb/procedure    expand one SID / STAR / APPROACH into its fixes
GET  /api/efb/airport      airport facts + every runway + its ILS
GET  /api/efb/runways      the runways of one airport only (heading / ILS)
POST /api/efb/upload       upload a navigation data package (zip / s3db)
GET  /api/efb/cycles       installed cycles
GET  /api/efb/health       readiness probe
GET  /login                web console login page (session cookie)
POST /api/efb/login        log in  (name / psw)
POST /api/efb/logout       log out
GET  /api/efb/session      current session state
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import config
from .finder import find_route
from .index import CycleError, CycleManager
from .ingest import IngestError, ensure_indexes, ingest
from .parser import parse_route
from .procedures import normalize_runway

logging.basicConfig(
    level=os.environ.get("NRS_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("navroute")

VERSION = "1.0.0"
STARTED_AT = time.time()

manager = CycleManager()


def _probe_dir(path: Path) -> Optional[str]:
    """Create ``path`` and verify it is really writable.

    Returns ``None`` when usable, otherwise a short human readable reason.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".nrs_write_test"
        probe.write_bytes(b"")
        probe.unlink()
        return None
    except OSError as exc:
        return f"{path}（{exc.__class__.__name__}: {exc}）"


def _read_only_hint(reason: str) -> str:
    return (
        f"数据目录不可写：{reason}。"
        f"当前进程 uid={os.getuid() if hasattr(os, 'getuid') else '?'}"
        f"，NRS_DATA_DIR={config.DATA_DIR}。"
        "Docker 下多半是宿主机挂载目录属主与容器内 uid 1000 不一致（bind mount 会覆盖镜像内的属主），"
        "三种处理方式：① 宿主机 `sudo chown -R 1000:1000 <挂载目录>`；"
        "② compose 里把 NRS_UID/NRS_GID 改成宿主机用户；③ 改用命名卷。"
        "当前以降级只读模式运行：查询/解析可用，上传与删除返回 503。"
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    problems = {
        name: reason
        for name, reason in (
            ("cycles", _probe_dir(config.CYCLES_DIR)),
            ("tmp", _probe_dir(config.TMP_DIR)),
        )
        if reason
    }
    config.READ_ONLY = bool(problems)
    if problems:
        # Never crash-loop on a permission problem: keep serving reads.
        log.error(_read_only_hint("；".join(problems.values())))
    else:
        log.info("data dir: %s (writable)", config.DATA_DIR)
    log.info("installed cycles: %s", [c["cycle"] for c in manager.cycles()] or "none")
    if config.PRELOAD:
        target = manager.default()
        if target:
            threading.Thread(target=_warm, args=(target,), name="preload", daemon=True).start()
    yield
    for cycle in manager.loaded_cycles():
        manager.unload(cycle)


def _ensure_cycle_indexes(cycle: str) -> None:
    """Add missing SQLite indexes to an already installed cycle.

    Best effort and never fatal: on a read-only data dir the call simply logs.
    A cycle ingested by an older release has no index on ``tbl_pf_iaps``, so the
    first approach lookup would scan 390 k rows.
    """
    entry = manager.entry(cycle) or {}
    db_file = entry.get("db_file")
    if not db_file:
        return
    path = os.path.join(str(config.CYCLES_DIR), cycle, db_file)
    if not os.path.isfile(path):
        return
    try:
        created = ensure_indexes(path)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("index maintenance for %s skipped: %s", cycle, exc)
        return
    if created:
        log.info("cycle %s: created sqlite indexes %s", cycle, created)


def _warm(target: str) -> None:
    t0 = time.perf_counter()
    try:
        _ensure_cycle_indexes(target)
        cyc = manager.get(target)
        if config.PRELOAD_GRAPH:
            cyc.graph()
        log.info(
            "cycle %s preloaded in %.0f ms (fixes=%d routes=%d graph=%s)",
            target,
            (time.perf_counter() - t0) * 1000,
            cyc.stats.get("fixes", 0),
            cyc.stats.get("routes", 0),
            "yes" if cyc._graph is not None else "lazy",
        )
    except Exception as exc:  # pragma: no cover
        log.warning("preload of cycle %s failed: %s", target, exc)


def _rebuild_cycle(cycle: str) -> dict:
    """Rebuild everything the query path needs for one freshly ingested cycle.

    ``ingest`` already rewrote ``index.pkl`` on disk; this drops any object that
    still points at the previous files and re-reads the snapshot, then rebuilds
    the airway graph so the first ``/api/efb/route`` call is warm.
    """
    t0 = time.perf_counter()
    _ensure_cycle_indexes(cycle)   # no-op when ingest already created them
    manager.unload(cycle)          # evict the pre-upload object (and its caches)
    cyc = manager.get(cycle)       # load index.pkl -> fresh airways/fixes/airports
    load_ms = (time.perf_counter() - t0) * 1000

    graph_ms = 0.0
    graph_ok = False
    if config.REBUILD_GRAPH_ON_UPLOAD:
        g0 = time.perf_counter()
        try:
            cyc.graph()
            graph_ok = True
        finally:
            graph_ms = (time.perf_counter() - g0) * 1000

    return {
        "loaded": True,
        "graph": graph_ok,
        "airways": cyc.stats.get("routes", 0),
        "fixes": cyc.stats.get("fixes", 0),
        "airports": cyc.stats.get("airports", 0),
        "load_ms": round(load_ms, 1),
        "graph_ms": round(graph_ms, 1),
        "rebuild_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


app = FastAPI(
    title="Navigraph DFD Route Service",
    version=VERSION,
    description="航路反向解析 / 航路查询服务，支持多 AIRAC 周期预缓存",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Paths that never need a browser session: the login page plus the JSON API
# (which authenticates with the POST token instead).
PUBLIC_PATHS = {"/login", "/static/login.html", "/favicon.ico"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/api/"):
        return await call_next(request)
    if session_of(request) is None:
        if path == "/" or path.startswith("/static"):
            nxt = path + (("?" + request.url.query) if request.url.query else "")
            return RedirectResponse("/login?next=" + quote(nxt, safe=""), status_code=303)
        return JSONResponse({"code": 401, "data": {}, "message": "请先登录"}, status_code=401)
    return await call_next(request)


# ------------------------------------------------------------------ helpers


def ok(data, message: str = "") -> JSONResponse:
    return JSONResponse({"code": 0, "data": data, "message": message})


def fail(code: int, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"code": code, "data": {}, "message": message}, status_code=status)


async def read_params(request: Request) -> dict:
    """Accept parameters from the query string, a JSON body or a form body."""
    params: dict = {}
    if request.method in ("POST", "PUT", "PATCH"):
        ctype = (request.headers.get("content-type") or "").lower()
        try:
            if "application/json" in ctype:
                raw = await request.body()
                if raw:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        params.update({k: v for k, v in payload.items() if v is not None})
            elif "form-urlencoded" in ctype or "multipart/form-data" in ctype:
                form = await request.form()
                params.update({k: v for k, v in form.items() if isinstance(v, str)})
        except Exception as exc:  # malformed body must not kill the request
            log.warning("cannot parse request body: %s", exc)
    params.update({k: v for k, v in request.query_params.items()})
    return params


def _s(value) -> str:
    return ("" if value is None else str(value)).strip()


def _flag(params: dict, names, default: bool = True) -> bool:
    """Read a boolean switch from the request parameters.

    ``detail=0`` / ``detail=false`` / ``detail=n`` turn it off; an absent
    parameter keeps ``default`` so older clients see no change.
    """
    if isinstance(names, str):
        names = (names,)
    for name in names:
        if name not in params:
            continue
        raw = _s(params.get(name)).lower()
        if raw in ("1", "true", "yes", "y", "on"):
            return True
        if raw in ("0", "false", "no", "n", "off"):
            return False
    return default


# --------------------------------------------------------------------- token


def token_of(request: Request, params: Optional[dict] = None) -> str:
    """Pull the shared token out of the body/query, a header or a bearer."""
    token = ""
    if params:
        token = _s(params.get("token") or params.get("access_token") or params.get("key"))
    if not token:
        token = _s(request.headers.get("x-token") or request.headers.get("x-auth-token"))
    if not token:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return token


def authorized(request: Request, params: Optional[dict] = None) -> bool:
    """POST guard: every write call must present ``NRS_TOKEN``."""
    expected = config.TOKEN
    if not expected:
        return True
    if params is None:
        params = dict(request.query_params)
    return hmac.compare_digest(token_of(request, params), expected)


def token_fail() -> JSONResponse:
    return fail(401, "缺少或错误的 token", 401)


# ----------------------------------------------------------------- read-only


def read_only_fail(action: str) -> JSONResponse:
    """Answer a write request when the data directory could not be written."""
    return fail(
        503,
        f"{action}不可用：数据目录 {config.DATA_DIR} 不可写，服务当前为只读模式。"
        "请在宿主机执行 `sudo chown -R 1000:1000 <挂载到 /data 的目录>`，"
        "或在 compose 中设置 NRS_UID/NRS_GID 与宿主机用户一致，或改用命名卷，然后重启容器。",
        503,
    )


# ------------------------------------------------------------------- session

# In-process session store: one small dict per logged-in browser.  There is no
# PHP and no server-side template engine here — the login form talks to the
# same JSON API, and the HTML page is a static file behind a cookie gate.
_sessions: Dict[str, dict] = {}
_sess_lock = threading.Lock()
_login_fails: Dict[str, tuple] = {}       # client ip -> (fails, blocked_until)


def _client_ip(request: Request) -> str:
    """Client address used by the brute-force guard.

    ``X-Forwarded-For`` is attacker-controlled unless a reverse proxy sets it,
    so it is only honoured when ``NRS_TRUST_PROXY=1``.
    """
    if config.TRUST_PROXY:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "-"


def _session_start(user: str) -> str:
    sid = secrets.token_urlsafe(32)
    with _sess_lock:
        _sessions[sid] = {"user": user, "exp": time.time() + config.SESSION_TTL}
    return sid


def session_of(request: Request) -> Optional[dict]:
    """Return the live session for this request, sliding its expiry."""
    sid = request.cookies.get(config.SESSION_COOKIE) or ""
    if not sid:
        return None
    now = time.time()
    with _sess_lock:
        rec = _sessions.get(sid)
        if rec is None:
            return None
        if rec["exp"] <= now:
            _sessions.pop(sid, None)
            return None
        rec["exp"] = now + config.SESSION_TTL
        return {"sid": sid, "user": rec["user"]}


def session_end(request: Request) -> None:
    sid = request.cookies.get(config.SESSION_COOKIE) or ""
    if sid:
        with _sess_lock:
            _sessions.pop(sid, None)


def login_locked_for(ip: str) -> int:
    """Seconds left in the cool-down for this client, 0 when allowed."""
    rec = _login_fails.get(ip)
    if not rec:
        return 0
    until = rec[1]
    return max(0, int(round(until - time.time())))


def login_failed(ip: str) -> None:
    fails, until = _login_fails.get(ip, (0, 0.0))
    fails += 1
    if fails >= max(1, config.LOGIN_MAX_FAILS):
        _login_fails[ip] = (0, time.time() + config.LOGIN_LOCK_SECONDS)
    else:
        _login_fails[ip] = (fails, until)


def login_ok(ip: str) -> None:
    _login_fails.pop(ip, None)


# ------------------------------------------------------------------ startup


# -------------------------------------------------------------------- routes


@app.get("/api/efb/health")
def health():
    return ok({
        "status": "degraded" if config.READ_ONLY else "ok",
        "read_only": config.READ_ONLY,
        "version": VERSION,
        "uptime_s": int(time.time() - STARTED_AT),
        "cycles": [c["cycle"] for c in manager.cycles()],
        "loaded": manager.loaded_cycles(),
        "default_cycle": manager.default(),
        "data_dir": str(config.DATA_DIR),
    })


@app.get("/api/efb/cycles")
def list_cycles():
    loaded = set(manager.loaded_cycles())
    default = manager.default()
    items = []
    for entry in manager.cycles():
        item = dict(entry)
        item["loaded"] = entry["cycle"] in loaded
        item["default"] = entry["cycle"] == default
        items.append(item)
    return ok({"default": default, "cycles": items})


@app.get("/api/efb/cycles/{cycle}")
def get_cycle(cycle: str):
    entry = manager.entry(cycle)
    if not entry:
        return fail(404, f"cycle {cycle} 未安装", 404)
    entry["loaded"] = cycle in manager.loaded_cycles()
    entry["default"] = cycle == manager.default()
    return ok(entry)


@app.delete("/api/efb/cycles/{cycle}")
def delete_cycle(cycle: str, request: Request):
    if not authorized(request):
        return token_fail()
    if config.READ_ONLY:
        return read_only_fail("删除周期")
    if not manager.entry(cycle):
        return fail(404, f"cycle {cycle} 未安装", 404)
    manager.unregister(cycle)
    target = os.path.join(str(config.CYCLES_DIR), cycle)
    if os.path.isdir(target):
        shutil.rmtree(target, ignore_errors=True)
    log.info("cycle %s removed", cycle)
    return ok({"cycle": cycle, "removed": True})


@app.post("/api/efb/cycles/{cycle}/unload")
def unload_cycle(cycle: str, request: Request):
    if not authorized(request):
        return token_fail()
    return ok({"cycle": cycle, "unloaded": manager.unload(cycle)})


@app.post("/api/efb/cycles/{cycle}/rebuild")
def rebuild_cycle_endpoint(cycle: str, request: Request):
    """Force a rebuild of the in-memory index + graph from the files on disk."""
    if not authorized(request):
        return token_fail()
    if not manager.entry(cycle):
        return fail(404, f"cycle {cycle} 未安装", 404)
    try:
        info = _rebuild_cycle(cycle)
    except Exception as exc:  # pragma: no cover
        log.exception("rebuild of cycle %s failed", cycle)
        return fail(500, f"重构建失败: {exc}", 500)
    log.info("cycle %s rebuilt: %s", cycle, info)
    return ok({"cycle": cycle, **info})


@app.post("/api/efb/upload")
async def upload(request: Request):
    form = await request.form()
    token = _s(form.get("token")) or token_of(request)
    if not authorized(request, {"token": token}):
        return token_fail()
    upload_file = form.get("file")
    if upload_file is None or not hasattr(upload_file, "read"):
        return fail(400, "缺少上传文件字段 file")
    if config.READ_ONLY:
        return read_only_fail("上传")
    overwrite = _s(form.get("overwrite")).lower() in ("1", "true", "yes", "on")
    # Rebuild is the default: an upload only counts as done once the new cycle
    # is actually serving from RAM.  rebuild=false / preload=false opts out.
    raw_rebuild = _s(form.get("rebuild") or form.get("preload")).lower()
    rebuild = config.REBUILD_ON_UPLOAD if not raw_rebuild else raw_rebuild not in ("0", "false", "no", "off")
    filename = _s(getattr(upload_file, "filename", "")) or "package.zip"

    Path(config.TMP_DIR).mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="upload-", suffix=".pkg", dir=str(config.TMP_DIR))
    size = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            while True:
                chunk = await upload_file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    return fail(413, f"文件超过限制 {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
                fh.write(chunk)
        if size == 0:
            return fail(400, "上传文件为空")
        t0 = time.perf_counter()
        entry = await run_in_threadpool(
            ingest, tmp_path, filename, str(config.DATA_DIR), overwrite, manager.unload
        )
    except IngestError as exc:
        return fail(400, str(exc))
    except Exception as exc:  # pragma: no cover
        log.exception("ingest failed")
        return fail(500, f"导入失败: {exc}", 500)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    cycle = entry["cycle"]
    manager.register(cycle, entry)
    if rebuild:
        try:
            entry["rebuild"] = await run_in_threadpool(_rebuild_cycle, cycle)
        except Exception as exc:  # pragma: no cover - index stays on disk
            log.exception("post-upload rebuild of cycle %s failed", cycle)
            entry["rebuild"] = {"loaded": False, "error": str(exc)}
    else:
        entry["rebuild"] = {"loaded": False, "skipped": True}
    entry["upload_ms"] = int((time.perf_counter() - t0) * 1000)
    log.info(
        "cycle %s ingested in %d ms, rebuild=%s: %s",
        cycle, entry["upload_ms"], entry["rebuild"], entry.get("stats"),
    )
    return ok(entry, f"周期 {cycle} 导入完成")


@app.post("/api/efb/parseRoute")
async def api_parse_route(request: Request):
    params = await read_params(request)
    if request.method == "POST" and not authorized(request, params):
        return token_fail()
    cycle = _s(params.get("cycle"))
    route = _s(params.get("route"))
    if not route:
        return fail(400, "参数 route 不能为空")
    # ``detail`` (default on) adds the full ARINC 424 leg table of the SID /
    # STAR that the route string expands to — the same rows the procedure
    # endpoint returns.  detail=0 keeps the payload small (points only).
    detail = _flag(params, ("detail", "segments", "verbose"), True)
    try:
        cyc = await run_in_threadpool(manager.get, cycle)
    except CycleError as exc:
        return fail(404, str(exc), 404)
    t0 = time.perf_counter()
    try:
        data = await run_in_threadpool(parse_route, cyc, route, detail)
    except Exception as exc:  # pragma: no cover
        log.exception("parseRoute failed")
        return fail(500, f"解析失败: {exc}", 500)
    data["took_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return ok(data)


@app.post("/api/efb/route")
async def api_route(request: Request):
    params = await read_params(request)
    if request.method == "POST" and not authorized(request, params):
        return token_fail()
    cycle = _s(params.get("cycle"))
    dep = _s(params.get("dep")).upper()
    arr = _s(params.get("arr")).upper()
    sid = _s(params.get("sid") or params.get("sid_name") or params.get("dep_proc")) or None
    star = _s(params.get("star") or params.get("star_name") or params.get("arr_proc")) or None
    iap = _s(params.get("iap") or params.get("approach") or params.get("app_proc")
             or params.get("arr_iap")) or None
    dep_runway = _s(params.get("dep_runway") or params.get("depRwy")) or None
    arr_runway = _s(params.get("arr_runway") or params.get("arrRwy")) or None
    procedures = _s(params.get("procedures")).lower() not in ("0", "false", "no", "off")
    detail = _flag(params, ("detail", "segments", "verbose"), True)
    if not dep or not arr:
        return fail(400, "参数 dep 与 arr 均不能为空")
    try:
        cyc = await run_in_threadpool(manager.get, cycle)
    except CycleError as exc:
        return fail(404, str(exc), 404)
    t0 = time.perf_counter()
    try:
        data = await run_in_threadpool(
            find_route, cyc, dep, arr,
            sid.upper() if sid else None,
            star.upper() if star else None,
            dep_runway, arr_runway, procedures, detail,
            iap.upper() if iap else None,
        )
    except ValueError as exc:
        return fail(400, str(exc))
    except Exception as exc:  # pragma: no cover
        log.exception("route search failed")
        return fail(500, f"航路查询失败: {exc}", 500)
    data["took_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return ok(data)


# ------------------------------------------------------- airport / runways


def _apt_param(params: dict) -> str:
    return _s(
        params.get("airport") or params.get("icao") or params.get("apt")
        or params.get("icao_code")
    ).upper()


def _pick_runway(info: dict, designator: str) -> dict:
    """Keep a single runway (``designator=18L``) and the ILS fitted to it."""
    rwy = (designator or "").strip().upper()
    if rwy.startswith("RWY"):
        rwy = rwy[3:]
    elif rwy.startswith("RW"):
        rwy = rwy[2:]
    data = dict(info)
    data["runways"] = [r for r in info["runways"] if (r.get("designator") or "") == rwy]
    data["ils"] = [i for i in info["ils"] if (i.get("runway") or "") == rwy]
    data["counts"] = dict(
        info["counts"], filtered=rwy,
        matched_runways=len(data["runways"]), matched_ils=len(data["ils"]),
    )
    return data


@app.api_route("/api/efb/airport", methods=["GET", "POST"])
async def api_airport(request: Request):
    """Airport facts plus every landing direction with its ILS."""
    params = await read_params(request)
    if request.method == "POST" and not authorized(request, params):
        return token_fail()
    cycle = _s(params.get("cycle"))
    apt = _apt_param(params)
    if not apt:
        return fail(400, "参数 airport（机场 ICAO）不能为空")
    try:
        cyc = await run_in_threadpool(manager.get, cycle)
    except CycleError as exc:
        return fail(404, str(exc), 404)
    t0 = time.perf_counter()
    info = await run_in_threadpool(cyc.airport_info, apt)
    if info is None:
        return fail(404, f"周期 {cyc.cycle} 中没有机场 {apt}", 404)
    data = dict(info)
    designator = _s(params.get("designator") or params.get("runway") or params.get("rwy"))
    if designator:
        data = _pick_runway(info, designator)
        if not data["runways"] and not data["ils"]:
            return fail(404, f"{apt} 没有跑道 {designator}", 404)
    data["cycle"] = cyc.cycle
    if _flag(params, ("procedures", "procs"), True):
        names = {}
        for kind in ("sid", "star", "iap"):
            names[kind] = list(await run_in_threadpool(cyc.procedure_names, kind, apt))
        data["procedures"] = names
        data["procedure_counts"] = {k: len(v) for k, v in names.items()}
    data["took_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return ok(data)


@app.api_route("/api/efb/runways", methods=["GET", "POST"])
async def api_runways(request: Request):
    """Only the runways: designator, heading, size, surface, elevation, ILS."""
    params = await read_params(request)
    if request.method == "POST" and not authorized(request, params):
        return token_fail()
    cycle = _s(params.get("cycle"))
    apt = _apt_param(params)
    if not apt:
        return fail(400, "参数 airport（机场 ICAO）不能为空")
    try:
        cyc = await run_in_threadpool(manager.get, cycle)
    except CycleError as exc:
        return fail(404, str(exc), 404)
    t0 = time.perf_counter()
    info = await run_in_threadpool(cyc.airport_info, apt)
    if info is None:
        return fail(404, f"周期 {cyc.cycle} 中没有机场 {apt}", 404)
    designator = _s(params.get("designator") or params.get("runway") or params.get("rwy"))
    if designator:
        info = _pick_runway(info, designator)
        if not info["runways"] and not info["ils"]:
            return fail(404, f"{apt} 没有跑道 {designator}", 404)
    data = {
        "cycle": cyc.cycle,
        "airport": apt,
        "name": info.get("name"),
        "elevation_ft": info.get("elevation_ft"),
        "magnetic_variation": info.get("magnetic_variation"),
        "runways": info["runways"],
        "counts": info["counts"],
    }
    if _flag(params, ("ils", "include_ils"), True):
        data["ils"] = info["ils"]
    data["took_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return ok(data)


# -------------------------------------------------------------- procedures

# ``type`` values accepted by the listing/detail endpoints.  The console (and
# most callers) say "approach"; ARINC 424 calls the table IAP.
KIND_ALIASES = {
    "sid": "sid", "dep": "sid", "departure": "sid",
    "star": "star", "arr": "star", "arrival": "star",
    "iap": "iap", "app": "iap", "approach": "iap", "apr": "iap", "iar": "iap",
}


def kinds_of(raw: str) -> list:
    """Parse the ``type`` parameter into the procedure kinds to return."""
    text = (raw or "").strip().lower()
    if text in ("", "both", "all", "*", "any"):
        return ["sid", "star", "iap"]
    out: list = []
    for part in text.replace("+", ",").replace("|", ",").replace(" ", ",").split(","):
        kind = KIND_ALIASES.get(part.strip())
        if kind and kind not in out:
            out.append(kind)
    return out or ["sid", "star", "iap"]


def _proc_list(cyc, apt: str, kind: str) -> list:
    items = []
    for name in sorted(cyc.procedures(kind, apt)):
        proc = cyc.procedure(kind, apt, name)
        legs, runway, transition, _ = proc.expand(None, None)
        if kind == "iap":
            # list the final approach only: the missed approach is not part of
            # the path a crew selects
            legs = [l for l in legs if l.role != "missed"]
        # leg types are a property of the coding, not of one runway variant
        all_paths = {l.path for seq in proc.sequences.values() for l in seq if l.path}
        item = {
            "name": proc.name,
            "type": kind,
            "runways": proc.runways,
            "transitions": proc.transitions,
            "families": proc.families,
            "waypoints": len(legs),
            "first": legs[0].name if legs else None,
            "last": legs[-1].name if legs else None,
            "leg_types": sorted(all_paths),
            "arc": bool(all_paths & {"RF", "AF"}),
        }
        if kind == "iap":
            item["approach"] = proc.approach
            item["approach_name"] = proc.approach_name
        items.append(item)
    return items


@app.api_route("/api/efb/procedures", methods=["GET", "POST"])
async def api_procedures(request: Request):
    params = await read_params(request)
    if request.method == "POST" and not authorized(request, params):
        return token_fail()
    cycle = _s(params.get("cycle"))
    apt = _s(params.get("airport") or params.get("icao") or params.get("apt")).upper()
    kinds = kinds_of(_s(params.get("type") or params.get("kind")))
    if not apt:
        return fail(400, "参数 airport 不能为空")
    try:
        cyc = await run_in_threadpool(manager.get, cycle)
    except CycleError as exc:
        return fail(404, str(exc), 404)
    if not cyc.airport(apt):
        return fail(404, f"周期 {cyc.cycle} 中没有机场 {apt}", 404)
    # optional filters, so a client can ask for "the SIDs of runway 18L" or
    # "the ILS approaches" only
    rwy_filter = normalize_runway(_s(params.get("runway") or params.get("rwy")))
    approach_filter = _s(params.get("approach") or params.get("app_type")).upper()
    t0 = time.perf_counter()
    data = {
        "cycle": cyc.cycle,
        "airport": apt,
        "types": kinds,
        "runway": rwy_filter,
        "approach": approach_filter or None,
        # every landing direction with heading / length / ILS idents
        "runways": await run_in_threadpool(cyc.runways_compact, apt),
    }
    for kind in kinds:
        items = await run_in_threadpool(_proc_list, cyc, apt, kind)
        if rwy_filter:
            items = [i for i in items
                     if rwy_filter in {normalize_runway(r) for r in i["runways"]}]
        if approach_filter and kind == "iap":
            items = [i for i in items
                     if approach_filter in ((i.get("approach") or "").upper(),
                                            (i.get("approach_name") or "").upper())]
        data[kind] = items
        data[kind + "_count"] = len(items)
    data["took_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return ok(data)


@app.api_route("/api/efb/procedure", methods=["GET", "POST"])
async def api_procedure(request: Request):
    params = await read_params(request)
    if request.method == "POST" and not authorized(request, params):
        return token_fail()
    cycle = _s(params.get("cycle"))
    apt = _s(params.get("airport") or params.get("icao") or params.get("apt")).upper()
    raw_kind = _s(params.get("type") or params.get("kind") or "sid").lower()
    kinds = kinds_of(raw_kind)
    kind = kinds[0]
    name = _s(params.get("name") or params.get("procedure")).upper()
    runway = _s(params.get("runway") or params.get("rwy")) or None
    transition = _s(params.get("transition") or params.get("trans")) or None
    if not apt or not name:
        return fail(400, "参数 airport 与 name 均不能为空")
    if raw_kind not in ("", "both", "all") and not KIND_ALIASES.get(raw_kind):
        return fail(400, "参数 type 只能是 sid、star 或 iap（approach）")
    try:
        cyc = await run_in_threadpool(manager.get, cycle)
    except CycleError as exc:
        return fail(404, str(exc), 404)
    t0 = time.perf_counter()
    detail = None
    # ``name`` alone is enough to find a procedure: with type=all the same
    # identifier could be a SID, a STAR or an approach.
    for candidate in kinds:
        detail = await run_in_threadpool(
            cyc.procedure_detail, candidate, apt, name, runway, transition
        )
        if detail is not None:
            kind = candidate
            break
    if detail is None:
        if len(kinds) > 1:
            return fail(404, f"{apt} 没有程序 {name}", 404)
        return fail(404, f"{apt} 没有 {kind.upper()} {name}", 404)
    detail["cycle"] = cyc.cycle
    detail["took_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return ok(detail)


# ----------------------------------------------------------------------- web

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


def safe_next(raw: str) -> str:
    """Only allow same-site relative targets after a successful login."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    if session_of(request) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.post("/api/efb/login")
async def api_login(request: Request):
    params = await read_params(request)
    name = _s(params.get("name") or params.get("username") or params.get("user"))
    psw = params.get("psw")
    if psw is None:
        psw = params.get("password")
    if psw is None:
        psw = params.get("pass")
    psw = "" if psw is None else str(psw)

    ip = _client_ip(request)
    wait = login_locked_for(ip)
    if wait:
        return fail(429, f"尝试过于频繁，请 {wait} 秒后再试", 429)
    if not (hmac.compare_digest(name, config.ADMIN_USER)
            and hmac.compare_digest(psw, config.ADMIN_PASS)):
        login_failed(ip)
        log.warning("login failed: user=%r ip=%s", name, ip)
        return fail(401, "用户名或密码错误", 401)

    login_ok(ip)
    sid = _session_start(name)
    resp = ok({"user": name, "expires_in": config.SESSION_TTL})
    resp.set_cookie(
        config.SESSION_COOKIE, sid,
        max_age=config.SESSION_TTL, httponly=True, samesite="lax",
        secure=config.COOKIE_SECURE, path="/",
    )
    log.info("login ok: user=%s ip=%s", name, ip)
    return resp


@app.post("/api/efb/logout")
def api_logout(request: Request):
    session_end(request)
    resp = ok({"logout": True})
    resp.delete_cookie(config.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/efb/session")
def api_session(request: Request):
    rec = session_of(request)
    return ok({"authenticated": rec is not None, "user": rec["user"] if rec else None})
