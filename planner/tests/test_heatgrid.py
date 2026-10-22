import pytest

from planner.domain.heatgrid import corridor_cells

from .factories import straight_route


def test_short_route_uses_finest_grid():
    size, cells = corridor_cells([straight_route(length_m=50_000)])
    assert size == 0.1
    assert 0 < len(cells) <= 350


def test_long_route_coarsens_to_fit_budget():
    size, cells = corridor_cells([straight_route(length_m=2_000_000, n=401)], max_cells=350)
    assert size > 0.1
    assert len(cells) <= 350


def test_cells_are_on_the_tenth_degree_grid():
    _, cells = corridor_cells([straight_route(length_m=800_000, n=201)])
    for lat, lon in cells:
        assert round(lat * 10) == pytest.approx(lat * 10, abs=1e-9)
        assert round(lon * 10) == pytest.approx(lon * 10, abs=1e-9)


def test_corridor_covers_every_route_sample():
    route = straight_route(length_m=300_000, n=101)
    size, cells = corridor_cells([route])
    cell_set = set(cells)
    for lat, lon in route.resample(5_000):
        assert (round(round(lat / size) * size, 1), round(round(lon / size) * size, 1)) in cell_set


def test_extreme_trip_is_thinned_to_budget():
    routes = [straight_route(start=(30 + k, -120), length_m=4_000_000, n=401) for k in range(3)]
    _, cells = corridor_cells(routes, max_cells=50)
    assert len(cells) == 50


def test_requires_routes():
    with pytest.raises(ValueError):
        corridor_cells([])
