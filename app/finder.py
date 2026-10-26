"""Dep -> Arr route search over the enroute airway graph (A*).

When the departure / arrival airport publishes terminal procedures the search
starts and ends at the *procedure* exits instead of the airport reference
point, so the returned route is a flyable clearance:

    ZBAA DOTR2G DOTRA G212 SGM ENVIP1 ZGGG

The procedure legs are prepended / appended with their published geometry; the
airway part in between is the shortest legal path over ``RouteGraph``.
"""

from __future__ import annotations

import heapq
import math
import time
from typing import Dict, List, Optional, Tuple

from . import config, geo
from . import legtext


class RouteGraph:
    """Airway connectivity graph for one cycle, built once and cached."""

    def __init__(self, cyc):
        self.cyc = cyc
        nodes: Dict[tuple, int] = {}
        names: List[str] = []
        lats: List[float] = []
        lons: List[float] = []
        adj: List[list] = []

        for route, chains in cyc.airways.items():
            for chain in chains:
                ids = []
                for k, fix in enumerate(chain.fixes):
                    key = (fix, chain.lats[k], chain.lons[k])
                    nid = nodes.get(key)
                    if nid is None:
                        nid = len(names)
                        nodes[key] = nid
                        names.append(fix)
                        lats.append(chain.lats[k])
                        lons.append(chain.lons[k])
                        adj.append([])
                    ids.append(nid)
                dirs = chain.dirs
                for k in range(len(ids) - 1):
                    a, b = ids[k], ids[k + 1]
                    if a == b:
                        continue
                    d = geo.haversine(lats[a], lons[a], lats[b], lons[b])
                    if d < 0.1:
                        continue
                    flag = dirs[k + 1] if k + 1 < len(dirs) else ""
                    adj[a].append((b, d, route, flag != "B"))
                    adj[b].append((a, d, route, flag != "F"))

        self.names = names
        self.lats = lats
        self.lons = lons
        self.adj = adj
        self.nodes = nodes
        self.edges = sum(len(a) for a in adj) // 2

        by_name: Dict[str, List[int]] = {}
        for i, name in enumerate(names):
            by_name.setdefault(name, []).append(i)
        self.by_name = by_name
        self._grid: Optional[Dict[Tuple[int, int], List[int]]] = None
        self._grid_cell = 1.0
        # Published fix -> graph node connections are immutable for one cycle,
        # and airports reuse the same fixes, so the result is memoised.
        self._attach_cache: Dict[tuple, List[Tuple[int, float]]] = {}

    # ------------------------------------------------------------------ geo

    def _build_grid(self, cell: float = 1.0) -> Dict[Tuple[int, int], List[int]]:
        grid: Dict[Tuple[int, int], List[int]] = {}
        for i, lat in enumerate(self.lats):
            key = (int(math.floor(lat / cell)), int(math.floor(self.lons[i] / cell)))
            grid.setdefault(key, []).append(i)
        self._grid_cell = cell
        return grid

    def near_nodes(self, lat: float, lon: float, radius: float,
                   limit: int = 2000) -> List[Tuple[int, float]]:
        """Graph nodes within ``radius`` NM, nearest first (grid indexed)."""
        if self._grid is None:
            self._grid = self._build_grid()
        cell = self._grid_cell
        span = int(math.ceil(radius / 60.0 / cell))
        cy = int(math.floor(lat / cell))
        coslat = max(0.15, abs(math.cos(math.radians(lat))))
        span_x = int(math.ceil(radius / (60.0 * cell * coslat)))
        out: List[Tuple[int, float]] = []
        seen = set()
        for dy in range(-span, span + 1):
            for dx in range(-span_x, span_x + 1):
                for nid in self._grid.get((cy + dy, int(math.floor(lon / cell)) + dx), ()):
                    if nid in seen:
                        continue
                    seen.add(nid)
                    d = geo.haversine(lat, lon, self.lats[nid], self.lons[nid])
                    if d <= radius:
                        out.append((nid, d))
        out.sort(key=lambda x: x[1])
        return out[:limit]

    def attach(self, name: str, lat: float, lon: float,
               radius: float = 40.0) -> List[Tuple[int, float]]:
        """Graph nodes representing one published fix."""
        key = (name, round(lat, 3), round(lon, 3), radius)
        hit = self._attach_cache.get(key)
        if hit is not None:
            return hit
        out: List[Tuple[int, float]] = []
        for nid in self.by_name.get(name, ()):  # exact identifier first
            d = geo.haversine(lat, lon, self.lats[nid], self.lons[nid])
            if d <= radius:
                out.append((nid, d))
        if out:
            out.sort(key=lambda x: x[1])
        else:
            out = self.near_nodes(lat, lon, radius, limit=8)
        if len(self._attach_cache) > 50000:
            self._attach_cache.clear()
        self._attach_cache[key] = out
        return out

    # --------------------------------------------------------------- search

    def _near(self, lat: float, lon: float, radius: float) -> List[Tuple[int, float]]:
        return self.near_nodes(lat, lon, radius)

    def search(
        self,
        dep_pt: Tuple[float, float],
        arr_pt: Tuple[float, float],
        radius: float = None,
        max_expand: int = None,
        time_limit: float = None,
        respect_direction: bool = True,
        max_first: int = 1500,
    ) -> Optional[dict]:
        radius = config.CONNECT_RADIUS_NM if radius is None else radius
        starts = self._near(dep_pt[0], dep_pt[1], radius)[:max_first]
        goals = {i: d for i, d in self._near(arr_pt[0], arr_pt[1], radius)[:max_first]}
        if not starts or not goals:
            return None
        return self.search_multi(
            {nid: d for nid, d in starts},
            goals,
            arr_pt=arr_pt,
            max_expand=max_expand,
            time_limit=time_limit,
            respect_direction=respect_direction,
        )

    def search_multi(
        self,
        sources: Dict[int, float],
        targets: Dict[int, float],
        arr_pt: Optional[Tuple[float, float]] = None,
        max_expand: int = None,
        time_limit: float = None,
        respect_direction: bool = True,
    ) -> Optional[dict]:
        """A* from many entry nodes to many exit nodes.

        ``sources`` maps node -> cost already flown to reach that node,
        ``targets`` maps node -> cost still to fly from that node to the goal.
        """
        if not sources or not targets:
            return None
        max_expand = config.ROUTE_MAX_EXPAND if max_expand is None else max_expand
        time_limit = config.ROUTE_TIME_LIMIT if time_limit is None else time_limit

        h = geo.haversine
        # All targets sit next to the arrival airport, so the great-circle
        # distance to the airport, minus the connector allowance, is admissible.
        if arr_pt is not None:
            alat, alon = arr_pt

            def heuristic(nid: int) -> float:
                d = h(self.lats[nid], self.lons[nid], alat, alon)
                return max(0.0, d - 100.0)
        else:
            def heuristic(_nid: int) -> float:
                return 0.0

        g: Dict[int, float] = {}
        parent: Dict[int, Tuple[int, str]] = {}
        pq: List[Tuple[float, float, int]] = []
        for nid, cost in sources.items():
            if cost < g.get(nid, float("inf")):
                g[nid] = cost
                parent[nid] = (-1, "")
                heapq.heappush(pq, (cost + heuristic(nid), cost, nid))

        deadline = time.perf_counter() + time_limit
        expand = 0
        while pq:
            _, gc, u = heapq.heappop(pq)
            if gc > g.get(u, float("inf")) + 1e-9:
                continue
            expand += 1
            if u in targets:
                total = gc + targets[u]
                path: List[int] = []
                edges: List[Tuple[int, int, str]] = []
                cur = u
                while cur != -1:
                    path.append(cur)
                    prev, route = parent[cur]
                    if prev != -1:
                        edges.append((prev, cur, route))
                    cur = prev
                path.reverse()
                edges.reverse()
                return {
                    "nodes": path,
                    "edges": edges,
                    "distance_nm": total,
                    "expanded": expand,
                }
            if expand > max_expand or (expand & 2047) == 0 and time.perf_counter() > deadline:
                break
            for v, d, route, ok in self.adj[u]:
                if not ok and respect_direction:
                    continue
                ng = gc + d
                if ng < g.get(v, float("inf")) - 1e-9:
                    g[v] = ng
                    parent[v] = (u, route)
                    heapq.heappush(pq, (ng + heuristic(v), ng, v))
        return None


# ------------------------------------------------------------------ terminal


def _procedure_candidates(cyc, apt: str, kind: str, name: Optional[str] = None,
                          runway: Optional[str] = None,
                          warnings: Optional[list] = None) -> List[dict]:
    """Every flyable variant of ``apt``'s SID (or STAR).

    A SID is expanded once per enroute transition, a STAR once per enroute
    transition as well; when a procedure has no transitions it yields a single
    candidate.  Each candidate carries the leg list and its ground length.
    """
    procs = cyc.procedures(kind, apt)
    if not procs:
        return []
    if name:
        wanted = name.strip().upper()
        proc = procs.get(wanted)
        if proc is None:
            for key, cand in procs.items():
                if key.startswith(wanted) or wanted.startswith(key):
                    proc = cand
                    break
        if proc is None:
            what = {"sid": "离场", "star": "进场"}.get(kind, "进近")
            raise ValueError(f"{apt} 没有{what}程序 {name}")
        targets = [proc]
    else:
        targets = [procs[k] for k in sorted(procs)]

    out: List[dict] = []
    for proc in targets:
        transitions = proc.transitions or [None]
        if runway:
            runways = [runway]
        else:
            # Without a stated runway every variant is a separate candidate so
            # the search can choose the one that fits the flight best.  A SID
            # that is only cleared from an unknown runway would otherwise lose
            # its runway transition, and a STAR would never reach the airport.
            runways = proc.runways or [None]
        for rw in runways:
            for transition in transitions:
                full_legs, used_rw, tr, notes = proc.expand(rw, transition)
                legs = full_legs
                if kind == "iap":
                    # The route ends on the runway; the go-around coded behind
                    # it is never flown on a normal arrival.
                    legs, _missed = proc.split_missed(full_legs)
                if len(legs) < 1:
                    continue
                # the ARINC 424 sequence incl. the open (vector) legs — this is
                # what the caller gets as ``segments``, identical to the
                # procedure endpoint
                segs, _, _, _ = proc.expand_segments(rw, transition)
                length = 0.0
                for a, b in zip(legs, legs[1:]):
                    length += geo.haversine(a.lat, a.lon, b.lat, b.lon)
                out.append({
                    "procedure": proc,
                    "name": proc.name,
                    "kind": kind,
                    "airport": apt,
                    "runway": used_rw,
                    "transition": tr,
                    "legs": legs,
                    "full_legs": full_legs,
                    "segs": segs,
                    "length_nm": length,
                    "notes": notes,
                })
    return out


def _route_notes(notes: List[str]) -> List[str]:
    """Keep the informative expansion notes, drop the expected ones."""
    return [n for n in notes if "未指定跑道" not in n]


def _dedupe_points(points: List[Tuple[str, float, float]]) -> List[Tuple[str, float, float]]:
    out: List[Tuple[str, float, float]] = []
    for name, lat, lon in points:
        if out:
            last = out[-1]
            if last[0] == name and abs(last[1] - lat) < 1e-6 and abs(last[2] - lon) < 1e-6:
                continue
            if abs(last[1] - lat) < 1e-7 and abs(last[2] - lon) < 1e-7:
                continue
        out.append((name, lat, lon))
    return out


def _route_string(dep: str, arr: str, sid: Optional[dict], star: Optional[dict],
                  graph: RouteGraph, path: List[int],
                  edges: List[Tuple[int, int, str]]) -> str:
    out: List[str] = [dep]
    if sid:
        out.append(sid["name"])
    current = None
    tail = list(edges)
    if star and tail:
        entry = star["legs"][0].name
        while tail and graph.names[tail[-1][1]] == entry:
            tail.pop()
    for _, b, route in tail:
        if route != current:
            out.append(route if route else "DCT")
            current = route
        out.append(graph.names[b])
    if not edges and not sid:
        out.append("DCT")
    if star:
        out.append(star["name"])
    out.append(arr)
    compact: List[str] = []
    for tok in out:
        if compact and compact[-1] == tok:
            continue
        compact.append(tok)
    return " ".join(compact)


def find_route(
    cyc,
    dep: str,
    arr: str,
    sid: Optional[str] = None,
    star: Optional[str] = None,
    dep_runway: Optional[str] = None,
    arr_runway: Optional[str] = None,
    use_procedures: bool = True,
    detail: bool = True,
    iap: Optional[str] = None,
) -> dict:
    warnings: List[str] = []
    dep_apt = cyc.airport(dep)
    arr_apt = cyc.airport(arr)
    if not dep_apt:
        raise ValueError(f"未找到机场 {dep}")
    if not arr_apt:
        raise ValueError(f"未找到机场 {arr}")
    if dep == arr:
        raise ValueError("起降机场相同")

    dep_pt = (dep_apt[0], dep_apt[1])
    arr_pt = (arr_apt[0], arr_apt[1])
    direct = geo.dist(dep_pt, arr_pt)
    graph = cyc.graph()

    sid_cands: List[dict] = []
    star_cands: List[dict] = []
    iap_cands: List[dict] = []
    if use_procedures:
        sid_cands = _procedure_candidates(cyc, dep, "sid", sid, dep_runway)
        if iap:
            # An explicit approach replaces the STAR: its IAF becomes the point
            # the enroute part is connected to, its runway the end of the route.
            try:
                iap_cands = _procedure_candidates(cyc, arr, "iap", iap, arr_runway)
            except ValueError:
                iap_cands = []
            if not iap_cands:
                warnings.append(f"{arr} 没有可用的进近程序 {iap}，已按 STAR 处理")
                star_cands = _procedure_candidates(cyc, arr, "star", star, arr_runway)
            elif star:
                warnings.append(f"已指定进近 {iap}，进场程序 {star} 不再接入")
        else:
            star_cands = _procedure_candidates(cyc, arr, "star", star, arr_runway)
        if not sid_cands and sid:
            warnings.append(f"{dep} 没有可用的离场程序 {sid}")
        if not star_cands and star and not iap_cands:
            warnings.append(f"{arr} 没有可用的进场程序 {star}")

    # the procedure the route terminates with (STAR or, when asked, approach)
    terminal_cands = star_cands or iap_cands

    # ---- multi-source / multi-target A* --------------------------------
    def build_sources():
        sources: Dict[int, float] = {}
        meta: Dict[int, tuple] = {}
        for cand in sid_cands:
            legs = cand["legs"]
            entry = (legs[0].lat, legs[0].lon)
            exit_leg = legs[-1]
            base = geo.dist(dep_pt, entry) + cand["length_nm"]
            for nid, d in graph.attach(exit_leg.name, exit_leg.lat, exit_leg.lon):
                cost = base + d
                if cost < sources.get(nid, float("inf")):
                    sources[nid] = cost
                    meta[nid] = (cand, d)
        return sources, meta

    def build_targets():
        targets: Dict[int, float] = {}
        meta: Dict[int, tuple] = {}
        for cand in terminal_cands:
            legs = cand["legs"]
            entry = legs[0]
            exit_pt = (legs[-1].lat, legs[-1].lon)
            base = cand["length_nm"] + geo.dist(exit_pt, arr_pt)
            # an approach may start at an IAF well outside the terminal area,
            # so it is allowed the same reach as the airport connector
            radius = config.CONNECT_RADIUS_NM if cand["kind"] == "iap" else 40.0
            for nid, d in graph.attach(entry.name, entry.lat, entry.lon, radius):
                cost = base + d
                if cost < targets.get(nid, float("inf")):
                    targets[nid] = cost
                    meta[nid] = (cand, d)
        return targets, meta

    sources, sid_meta = build_sources() if sid_cands else ({}, {})
    targets, term_meta = build_targets() if terminal_cands else ({}, {})

    # Fall back to plain airport connectors when no procedure is usable.
    if not sources:
        if sid_cands:
            warnings.append("离场程序无法接入航路图，已改从机场直连航路")
        sources = {nid: d for nid, d in graph.near_nodes(
            dep_pt[0], dep_pt[1], config.CONNECT_RADIUS_NM, limit=1500)}
        sid_meta = {}
    if not targets:
        if terminal_cands:
            warnings.append("进场程序无法接入航路图，已改从航路直连机场")
        targets = {nid: d for nid, d in graph.near_nodes(
            arr_pt[0], arr_pt[1], config.CONNECT_RADIUS_NM, limit=1500)}
        term_meta = {}

    found = None
    if sources and targets:
        found = graph.search_multi(sources, targets, arr_pt=arr_pt, respect_direction=True)
        if found is None:
            found = graph.search_multi(sources, targets, arr_pt=arr_pt, respect_direction=False)
            if found is not None:
                warnings.append("未找到完全符合单向限制的航路，已放宽方向限制")

    if found is None:
        warnings.append("未找到可用航路，返回直飞")
        return {
            "cycle": cyc.cycle,
            "dep": dep,
            "arr": arr,
            "route": f"{dep} DCT {arr}",
            "distance_nm": round(direct, 1),
            "waypoints": [
                {"name": dep, "lat": dep_pt[0], "lon": dep_pt[1]},
                {"name": arr, "lat": arr_pt[0], "lon": arr_pt[1]},
            ],
            "warnings": warnings,
            "sid": None,
            "star": None,
            "iap": None,
        }

    path = found["nodes"]
    edges = found["edges"]
    sid_choice = sid_meta.get(path[0])[0] if path and path[0] in sid_meta else None
    term_choice = term_meta.get(path[-1])[0] if path and path[-1] in term_meta else None

    points: List[Tuple[str, float, float]] = [(dep, dep_pt[0], dep_pt[1])]
    if sid_choice:
        for leg in sid_choice["legs"]:
            points.append((leg.name, leg.lat, leg.lon))
        warnings.extend(_route_notes(sid_choice["notes"]))
    for nid in path:
        points.append((graph.names[nid], graph.lats[nid], graph.lons[nid]))
    if term_choice:
        for leg in term_choice["legs"]:
            points.append((leg.name, leg.lat, leg.lon))
        warnings.extend(_route_notes(term_choice["notes"]))
    if not (term_choice and term_choice["kind"] == "iap"):
        # an approach already ends on the runway: no reference point behind it
        points.append((arr, arr_pt[0], arr_pt[1]))
    points = _dedupe_points(points)

    total = 0.0
    for a, b in zip(points, points[1:]):
        total += geo.haversine(a[1], a[2], b[1], b[2])

    route_str = _route_string(dep, arr, sid_choice, term_choice, graph, path, edges)
    if direct > 0 and total > direct * 1.6:
        warnings.append(
            "航路图只包含公开的 ER 航路（不含洋区航迹），"
            f"实际距离 {total:.0f} NM 明显长于直线 {direct:.0f} NM"
        )

    def describe(cand):
        if not cand:
            return None
        segs = cand.get("segs") or cand["legs"]
        info = {
            "name": cand["name"],
            "type": cand["kind"],
            "airport": cand["airport"],
            "runway": cand["runway"],
            "transition": cand["transition"],
            "waypoints": len(cand["legs"]),
            "distance_nm": round(cand["length_nm"], 1),
            "leg_types": sorted({l.path for l in segs if l.path}),
        }
        if cand["kind"] == "iap":
            proc = cand["procedure"]
            app_track, ga_track, end_leg = proc.track_split(
                cand["runway"], cand["transition"])
            info["approach"] = proc.approach
            info["approach_name"] = proc.approach_name
            missed = proc.split_missed(cand.get("full_legs") or cand["legs"])[1]
            missed = missed[1:] if missed else []
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
                    {"name": l.name, "lat": round(l.lat, 8), "lon": round(l.lon, 8),
                     "role": l.role, "path": l.path}
                    for l in missed
                ]
        if detail:
            # same ARINC 424 leg table as /api/efb/procedure
            info["segments"] = legtext.segments(segs)
            if cand["kind"] != "iap":
                info["track"] = legtext.track_points(segs)
        return info

    term_info = describe(term_choice)
    return {
        "cycle": cyc.cycle,
        "dep": dep,
        "arr": arr,
        "route": route_str,
        "distance_nm": round(total, 1),
        "waypoints": [
            {"name": n, "lat": round(lat, 8), "lon": round(lon, 8)}
            for n, lat, lon in points
        ],
        "warnings": warnings,
        "sid": describe(sid_choice),
        "star": term_info if term_choice and term_choice["kind"] == "star" else None,
        "iap": term_info if term_choice and term_choice["kind"] == "iap" else None,
    }
