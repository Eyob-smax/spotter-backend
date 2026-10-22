import pytest

from planner.domain.geo import METERS_PER_MILE
from planner.domain.sampling import place_checkpoints

from .factories import straight_route


def route_of_miles(miles, speed_mps=25.0):
    return straight_route(length_m=miles * METERS_PER_MILE, n=201, speed_mps=speed_mps)


@pytest.mark.parametrize("interval", [10, 25, 50])
def test_spacing_and_destination(interval):
    route = route_of_miles(120)
    cps = place_checkpoints(route, interval)
    miles = [c.mile for c in cps]
    assert miles[0] == 0
    assert miles[-1] == pytest.approx(120)
    for a, b in zip(miles[:-2], miles[1:-1]):
        assert b - a == pytest.approx(interval)


@pytest.mark.parametrize("length, interval", [(120, 25), (100, 25), (7, 10), (0.4, 50), (50.2, 25)])
def test_segments_sum_to_route_length(length, interval):
    cps = place_checkpoints(route_of_miles(length), interval)
    assert sum(c.segment_mi for c in cps) == pytest.approx(length)


def test_exact_multiple_has_no_duplicate_destination():
    cps = place_checkpoints(route_of_miles(100), 25)
    assert [round(c.mile, 6) for c in cps] == [0, 25, 50, 75, 100]


def test_near_destination_checkpoint_snaps_to_destination():
    cps = place_checkpoints(route_of_miles(50.2), 25)
    assert [round(c.mile, 1) for c in cps] == [0, 25, 50.2]


def test_short_route_gets_origin_and_destination():
    cps = place_checkpoints(route_of_miles(0.3), 50)
    assert len(cps) == 2
    assert cps[-1].mile == pytest.approx(0.3)


def test_eta_follows_route_timing():
    route = route_of_miles(100, speed_mps=20)
    cps = place_checkpoints(route, 25)
    for c in cps:
        assert c.elapsed_s == pytest.approx(c.mile * METERS_PER_MILE / 20, rel=1e-6)
    elapsed = [c.elapsed_s for c in cps]
    assert elapsed == sorted(elapsed)


def test_progress_and_indices():
    cps = place_checkpoints(route_of_miles(100), 25)
    assert [c.index for c in cps] == list(range(len(cps)))
    assert cps[0].progress == 0 and cps[-1].progress == pytest.approx(1)


def test_segment_is_half_way_to_neighbours():
    cps = place_checkpoints(route_of_miles(100), 25)
    assert [round(c.segment_mi, 6) for c in cps] == [12.5, 25, 25, 25, 12.5]


def test_rejects_non_positive_interval():
    with pytest.raises(ValueError):
        place_checkpoints(route_of_miles(10), 0)
