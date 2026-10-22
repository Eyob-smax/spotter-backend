"""Checkpoint placement along a route.

Checkpoints sit at mile 0, every ``interval_mi`` miles, and at the destination.
Each checkpoint represents the stretch of road halfway to its neighbours
(``segment_mi``), so segment miles always sum to the route length; this is
what "miles at Severe/High risk" are computed from.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geo import METERS_PER_MILE, LatLon, Polyline

ALLOWED_INTERVALS_MI = (10, 25, 50)
# Don't add a separate destination checkpoint this close to the last regular one.
MIN_TAIL_MI = 0.5


@dataclass(frozen=True)
class Checkpoint:
    index: int
    mile: float
    point: LatLon
    elapsed_s: float  # travel time from departure
    progress: float  # 0..1 along route distance
    segment_mi: float


def place_checkpoints(route: Polyline, interval_mi: float) -> list[Checkpoint]:
    if interval_mi <= 0:
        raise ValueError("interval_mi must be positive.")
    length_mi = route.length_m / METERS_PER_MILE

    miles: list[float] = []
    k = 0
    while k * interval_mi < length_mi:
        miles.append(k * interval_mi)
        k += 1
    if not miles or length_mi - miles[-1] >= MIN_TAIL_MI or len(miles) == 1:
        miles.append(length_mi)
    else:
        miles[-1] = length_mi  # snap a near-destination checkpoint onto the destination

    checkpoints: list[Checkpoint] = []
    for i, mile in enumerate(miles):
        start = 0.0 if i == 0 else (miles[i - 1] + mile) / 2
        end = length_mi if i == len(miles) - 1 else (mile + miles[i + 1]) / 2
        point, elapsed = route.at_distance(mile * METERS_PER_MILE)
        checkpoints.append(
            Checkpoint(
                index=i,
                mile=mile,
                point=point,
                elapsed_s=elapsed,
                progress=0.0 if length_mi == 0 else mile / length_mi,
                segment_mi=end - start,
            )
        )
    return checkpoints
