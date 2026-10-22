"""Grid of cells covering the trip corridor, used for the forecast heatmap.

Cell centres lie on multiples of the cell size, and every size is a multiple
of 0.1 degrees, so heatmap cells share the weather cache with checkpoints.
"""

from __future__ import annotations

from .geo import LatLon, Polyline

CELL_SIZES_DEG = (0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0)
DEFAULT_MAX_CELLS = 350
METERS_PER_DEGREE_LAT = 111_320.0


def _cells_for_size(routes: list[Polyline], size: float, halo: int) -> set[tuple[int, int]]:
    step_m = size * METERS_PER_DEGREE_LAT / 2
    cells: set[tuple[int, int]] = set()
    for route in routes:
        for lat, lon in route.resample(step_m):
            i, j = round(lat / size), round(lon / size)
            for di in range(-halo, halo + 1):
                for dj in range(-halo, halo + 1):
                    cells.add((i + di, j + dj))
    return cells


def corridor_cells(
    routes: list[Polyline], max_cells: int = DEFAULT_MAX_CELLS, halo: int = 1
) -> tuple[float, list[LatLon]]:
    """Smallest cell size whose corridor fits in ``max_cells``; returns (size, sorted cell centres)."""
    if not routes:
        raise ValueError("At least one route is required.")
    attempts = [(size, halo) for size in CELL_SIZES_DEG] + [(CELL_SIZES_DEG[-1], 0)]
    for size, h in attempts:
        cells = _cells_for_size(routes, size, h)
        if len(cells) <= max_cells:
            break
    centres = sorted((round(i * size, 1), round(j * size, 1)) for i, j in cells)
    if len(centres) > max_cells:
        # Extreme trips only (coarsest grid, no halo): thin evenly so the whole corridor stays covered.
        step = len(centres) / max_cells
        centres = [centres[int(k * step)] for k in range(max_cells)]
    return size, centres
