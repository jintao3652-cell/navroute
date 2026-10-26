"""Spherical geometry helpers (all distances in nautical miles)."""

from __future__ import annotations

import math

EARTH_RADIUS_NM = 3440.065


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp * 0.5) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl * 0.5) ** 2
    if h < 0.0:
        h = 0.0
    return 2.0 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(h)))


def dist(a, b) -> float:
    """Distance between two (lat, lon) tuples."""
    return haversine(a[0], a[1], b[0], b[1])


def bearing(a, b) -> float:
    """Initial great-circle bearing from ``a`` to ``b`` in degrees (0..360)."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dl = lon2 - lon1
    y = math.sin(dl) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination(point, brg: float, d: float):
    """Point ``d`` NM from ``point`` on bearing ``brg`` (degrees true)."""
    lat1, lon1 = math.radians(point[0]), math.radians(point[1])
    br = math.radians(brg)
    dr = d / EARTH_RADIUS_NM
    lat2 = math.asin(
        math.sin(lat1) * math.cos(dr) + math.cos(lat1) * math.sin(dr) * math.cos(br)
    )
    lon2 = lon1 + math.atan2(
        math.sin(br) * math.sin(dr) * math.cos(lat1),
        math.cos(dr) - math.sin(lat1) * math.sin(lat2),
    )
    return (math.degrees(lat2), (math.degrees(lon2) + 540.0) % 360.0 - 180.0)


def angle_delta(a: float, b: float) -> float:
    """Signed smallest rotation from ``a`` to ``b`` in degrees (-180..180)."""
    return (b - a + 540.0) % 360.0 - 180.0
