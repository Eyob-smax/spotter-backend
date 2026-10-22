"""Builders for synthetic routes used across tests."""

from planner.domain.geo import Polyline, cumulative_distances, destination_point


def straight_route(
    start=(39.0, -105.0), bearing=90.0, length_m=100_000.0, n=101, speed_mps=25.0, wiggle_deg=0.0
) -> Polyline:
    """A (nearly) straight route with constant speed; optional zig-zag to give simplification work."""
    points = []
    for k in range(n):
        lat, lon = destination_point(start, bearing, length_m * k / (n - 1))
        if wiggle_deg:
            lat += wiggle_deg if k % 2 else -wiggle_deg
        points.append((lat, lon))
    if wiggle_deg:
        points[0], points[-1] = start, destination_point(start, bearing, length_m)
    dist = cumulative_distances(points)
    return Polyline(points=points, dist_m=dist, time_s=[d / speed_mps for d in dist])
