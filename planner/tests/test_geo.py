import pytest

from planner.domain.geo import (
    Polyline,
    bearing_deg,
    destination_point,
    haversine_m,
    route_similarity,
    simplify_indices,
    simplify_to_budget,
)

from .factories import straight_route

DENVER = (39.7392, -104.9903)
SLC = (40.7608, -111.8910)


def test_haversine_known_distance():
    # Denver -> Salt Lake City great-circle distance is ~598 km.
    assert haversine_m(DENVER, SLC) == pytest.approx(598_000, rel=0.01)


def test_haversine_zero():
    assert haversine_m(DENVER, DENVER) == 0


@pytest.mark.parametrize("bearing", [0, 45, 90, 180, 270, 359])
def test_destination_point_round_trip(bearing):
    target = destination_point(DENVER, bearing, 50_000)
    assert haversine_m(DENVER, target) == pytest.approx(50_000, rel=1e-6)
    assert bearing_deg(DENVER, target) == pytest.approx(bearing % 360, abs=0.01)


def test_destination_point_wraps_antimeridian():
    lat, lon = destination_point((0.0, 179.9), 90, 50_000)
    assert -180 <= lon < -179


class TestPolyline:
    def test_requires_two_points(self):
        with pytest.raises(ValueError):
            Polyline(points=[(0, 0)], dist_m=[0], time_s=[0])

    def test_requires_equal_lengths(self):
        with pytest.raises(ValueError):
            Polyline(points=[(0, 0), (0, 1)], dist_m=[0], time_s=[0, 1])

    def test_at_distance_interpolates_position_and_time(self):
        route = straight_route(length_m=100_000, speed_mps=25)
        point, t = route.at_distance(50_000)
        assert haversine_m(route.points[0], point) == pytest.approx(50_000, rel=1e-3)
        assert t == pytest.approx(2_000, rel=1e-3)

    def test_at_distance_clamps(self):
        route = straight_route()
        assert route.at_distance(-5)[0] == route.points[0]
        end, t = route.at_distance(route.length_m * 2)
        assert end == pytest.approx(route.points[-1])
        assert t == pytest.approx(route.duration_s)

    def test_resample_includes_ends_and_spacing(self):
        route = straight_route(length_m=10_000)
        samples = route.resample(1_000)
        assert len(samples) == 11
        assert samples[0] == route.points[0]
        assert haversine_m(samples[0], samples[1]) == pytest.approx(1_000, rel=1e-3)

    def test_resample_rejects_non_positive_step(self):
        with pytest.raises(ValueError):
            straight_route().resample(0)

    def test_bearing_at_distance(self):
        route = straight_route(bearing=90)
        assert route.bearing_at_distance(50_000) == pytest.approx(90, abs=1)


class TestSimplify:
    def test_straight_line_collapses_to_endpoints(self):
        # A meridian is straight in the equirectangular projection (an eastward
        # great circle bulges ~600 m over 100 km, so it would rightly keep vertices).
        route = straight_route(bearing=0, n=500)
        assert simplify_indices(route.points, 5) == [0, 499]

    def test_keeps_endpoints_and_order(self):
        route = straight_route(n=301, wiggle_deg=0.01)
        idx = simplify_indices(route.points, 50)
        assert idx[0] == 0 and idx[-1] == 300 and idx == sorted(idx)

    def test_budget_respected(self):
        route = straight_route(n=2001, wiggle_deg=0.005)
        idx = simplify_to_budget(route.points, 200)
        assert len(idx) <= 200

    def test_budget_rejects_tiny(self):
        with pytest.raises(ValueError):
            simplify_to_budget([(0, 0), (1, 1)], 1)

    def test_subset_keeps_cumulative_values(self):
        route = straight_route(n=101)
        sub = route.subset([0, 50, 100])
        assert sub.dist_m == [route.dist_m[0], route.dist_m[50], route.dist_m[100]]
        assert sub.time_s[-1] == route.time_s[-1]


class TestSimilarity:
    def test_identical_routes(self):
        samples = straight_route().resample(1_000)
        assert route_similarity(samples, samples) == 1.0

    def test_disjoint_routes(self):
        a = straight_route(start=(39.0, -105.0)).resample(1_000)
        b = straight_route(start=(41.0, -105.0)).resample(1_000)
        assert route_similarity(a, b) == 0.0

    def test_half_shared(self):
        a = straight_route(start=(39.0, -105.0), length_m=100_000).resample(1_000)
        b = straight_route(start=(39.0, -105.0), length_m=50_000).resample(1_000)
        b += straight_route(start=(39.5, -104.4), length_m=50_000).resample(1_000)
        assert 0.4 < route_similarity(a, b) < 0.7

    def test_empty(self):
        assert route_similarity([], [(0, 0)]) == 0.0
