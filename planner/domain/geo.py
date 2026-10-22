"""Geometry helpers on WGS84 (lat, lon) points. Pure functions, no I/O."""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

LatLon = tuple[float, float]

EARTH_RADIUS_M = 6_371_008.8
METERS_PER_MILE = 1609.344
LB_PER_KG = 2.204_622_621_8


def haversine_m(a: LatLon, b: LatLon) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(a: LatLon, b: LatLon) -> float:
    """Initial great-circle bearing from a to b, in degrees clockwise from north [0, 360)."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def destination_point(start: LatLon, bearing: float, distance_m: float) -> LatLon:
    """Point reached travelling `distance_m` from `start` on initial `bearing` (degrees)."""
    lat1, lon1 = map(math.radians, start)
    brg = math.radians(bearing)
    ang = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(brg))
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    lon_deg = (math.degrees(lon2) + 540.0) % 360.0 - 180.0
    return (math.degrees(lat2), lon_deg)


def cumulative_distances(points: list[LatLon]) -> list[float]:
    out = [0.0]
    for prev, cur in zip(points, points[1:]):
        out.append(out[-1] + haversine_m(prev, cur))
    return out


@dataclass(frozen=True)
class Polyline:
    """A route line with cumulative distance (m) and travel time (s) per vertex.

    Both cumulative arrays are non-decreasing and start at 0.
    """

    points: list[LatLon]
    dist_m: list[float]
    time_s: list[float]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("A polyline needs at least two points.")
        if not (len(self.points) == len(self.dist_m) == len(self.time_s)):
            raise ValueError("points, dist_m and time_s must have equal length.")

    @property
    def length_m(self) -> float:
        return self.dist_m[-1]

    @property
    def duration_s(self) -> float:
        return self.time_s[-1]

    def at_distance(self, d: float) -> tuple[LatLon, float]:
        """Position and elapsed time at distance `d` (clamped to the line)."""
        d = min(max(d, 0.0), self.length_m)
        i = bisect_right(self.dist_m, d) - 1
        i = min(max(i, 0), len(self.points) - 2)
        d0, d1 = self.dist_m[i], self.dist_m[i + 1]
        f = 0.0 if d1 <= d0 else (d - d0) / (d1 - d0)
        (la0, lo0), (la1, lo1) = self.points[i], self.points[i + 1]
        point = (la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f)
        t = self.time_s[i] + (self.time_s[i + 1] - self.time_s[i]) * f
        return point, t

    def bearing_at_distance(self, d: float, lookahead_m: float = 500.0) -> float:
        a, _ = self.at_distance(max(0.0, d - lookahead_m))
        b, _ = self.at_distance(min(self.length_m, d + lookahead_m))
        return bearing_deg(a, b)

    def resample(self, step_m: float) -> list[LatLon]:
        """Points every `step_m` metres along the line, including both ends."""
        if step_m <= 0:
            raise ValueError("step_m must be positive.")
        n = max(1, math.ceil(self.length_m / step_m))
        return [self.at_distance(self.length_m * k / n)[0] for k in range(n + 1)]

    def subset(self, indices: list[int]) -> Polyline:
        return Polyline(
            points=[self.points[i] for i in indices],
            dist_m=[self.dist_m[i] for i in indices],
            time_s=[self.time_s[i] for i in indices],
        )


def _project(points: list[LatLon]) -> list[tuple[float, float]]:
    """Equirectangular projection to metres; accurate enough for simplification tolerances."""
    mean_lat = math.radians(sum(p[0] for p in points) / len(points))
    kx = math.cos(mean_lat) * math.pi * EARTH_RADIUS_M / 180.0
    ky = math.pi * EARTH_RADIUS_M / 180.0
    return [(lon * kx, lat * ky) for lat, lon in points]


def _segment_distance(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify_indices(points: list[LatLon], tolerance_m: float) -> list[int]:
    """Ramer-Douglas-Peucker (iterative). Returns sorted indices of kept vertices, endpoints included."""
    n = len(points)
    if n <= 2:
        return list(range(n))
    xy = _project(points)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        start, end = stack.pop()
        max_d, max_i = -1.0, -1
        a, b = xy[start], xy[end]
        for i in range(start + 1, end):
            d = _segment_distance(xy[i], a, b)
            if d > max_d:
                max_d, max_i = d, i
        if max_d > tolerance_m and max_i != -1:
            keep[max_i] = True
            stack.append((start, max_i))
            stack.append((max_i, end))
    return [i for i, k in enumerate(keep) if k]


def simplify_to_budget(points: list[LatLon], max_points: int, start_tolerance_m: float = 10.0) -> list[int]:
    """Smallest tolerance (doubling from `start_tolerance_m`) that keeps at most `max_points` vertices."""
    if max_points < 2:
        raise ValueError("max_points must be at least 2.")
    tolerance = start_tolerance_m
    indices = simplify_indices(points, tolerance)
    while len(indices) > max_points:
        tolerance *= 2
        indices = simplify_indices(points, tolerance)
    return indices


def _cells(points: list[LatLon], cell_deg: float) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for lat, lon in points:
        ci, cj = math.floor(lat / cell_deg), math.floor(lon / cell_deg)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out.add((ci + di, cj + dj))
    return out


def overlap_fraction(a_samples: list[LatLon], b_samples: list[LatLon], cell_deg: float = 0.01) -> float:
    """Share of `a_samples` lying within ~1 grid cell of route b."""
    if not a_samples:
        return 0.0
    b_cells = _cells(b_samples, cell_deg)
    hits = sum(1 for lat, lon in a_samples if (math.floor(lat / cell_deg), math.floor(lon / cell_deg)) in b_cells)
    return hits / len(a_samples)


def route_similarity(a_samples: list[LatLon], b_samples: list[LatLon]) -> float:
    """Symmetric similarity in [0, 1]: mean of the two directional overlaps."""
    return (overlap_fraction(a_samples, b_samples) + overlap_fraction(b_samples, a_samples)) / 2
