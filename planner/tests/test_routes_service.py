from datetime import UTC, datetime

import pytest

from planner.domain.geo import haversine_m
from planner.services.http import build_session
from planner.services.routes import (
    GEOMETRY_POINT_BUDGET,
    _name_routes,
    detour_offset_m,
    detour_waypoint,
    find_routes,
)
from planner.services.tomtom import TomTomClient, parse_route

from .factories import straight_route
from .fakes import TOMTOM, build_route

DEPART = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
ORIGIN, DEST = (39.74, -104.99), (40.76, -111.89)


@pytest.fixture
def client():
    return TomTomClient("k", TOMTOM, build_session())


def run(client):
    return find_routes(client, ORIGIN, DEST, load_lb=42_000, depart_at=DEPART)


def test_three_provider_routes_need_no_detour(client, tomtom):
    routes, notices = run(client)
    assert [r.id for r in routes] == ["A", "B", "C"]
    assert all(r.source == "tomtom" for r in routes)
    assert tomtom.route_calls == 1
    assert notices == []
    durations = [r.parsed.duration_s for r in routes]
    assert durations == sorted(durations)
    assert [r.name for r in routes] == ["via I-80", "via I-70", "via I-60"]


def test_detours_fill_missing_routes(client, tomtom):
    tomtom.alternatives = 1
    routes, notices = run(client)
    assert len(routes) == 3
    assert [r.source for r in routes].count("tomtom_via") == 2
    detour_calls = [c for c in tomtom.calls if len(c["locations"]) == 3]
    assert detour_calls and all(c["params"]["maxAlternatives"] == ["0"] for c in detour_calls)
    assert "returned 1 distinct route;" in notices[0]
    assert "were generated through a detour waypoint" in notices[0]


def test_failed_detours_leave_fewer_routes_with_notice(client, tomtom):
    tomtom.alternatives = 1
    tomtom.detour_error = (400, {"detailedError": {"code": "MAP_MATCHING_FAILURE", "message": "Waypoint 1"}})
    routes, notices = run(client)
    assert len(routes) == 1
    assert notices == ["Only 1 distinct truck route could be found."]
    assert len([c for c in tomtom.calls if len(c["locations"]) == 3]) == 6  # every candidate tried


def test_duplicate_provider_routes_are_removed(client, rsps):
    import json
    import re

    import responses

    straight = build_route([ORIGIN, DEST])
    rsps.add(
        responses.GET,
        re.compile(r".*calculateRoute/[^:]+:[^:/]+/json.*"),
        body=json.dumps({"routes": [straight, straight, build_route([ORIGIN, DEST], bulge_deg=0.5)]}),
    )
    rsps.add(responses.GET, re.compile(r".*calculateRoute/[^/]+:[^/]+:[^/]+/json.*"), json={"routes": []})
    routes, notices = run(client)
    assert len(routes) == 2
    assert notices[-1] == "Only 2 distinct truck routes could be found."


def test_geometry_is_simplified_with_aligned_arrays(client, tomtom):
    routes, _ = run(client)
    for r in routes:
        assert len(r.line.points) <= GEOMETRY_POINT_BUDGET
        assert len(r.line.points) == len(r.line.dist_m) == len(r.line.time_s)
        assert r.line.points[0] == r.parsed.line.points[0]
        assert r.line.time_s[-1] == r.parsed.line.time_s[-1]


def test_detour_offset_is_clamped():
    assert detour_offset_m((0, 0), (0, 0.01)) == 15_000
    assert detour_offset_m((30, -120), (40, -75)) == 120_000
    mid = detour_offset_m((39.0, -105.0), (39.0, -101.0))
    assert 15_000 < mid < 120_000


def test_detour_waypoint_is_sideways():
    route = straight_route(start=(39.0, -105.0), bearing=90, length_m=200_000)
    right = detour_waypoint(route, 0.5, 1, 30_000)
    left = detour_waypoint(route, 0.5, -1, 30_000)
    mid, _ = route.at_distance(100_000)
    assert haversine_m(mid, right) == pytest.approx(30_000, rel=0.01)
    assert right[0] < mid[0] < left[0]  # travelling east: right is south


def test_names_disambiguate_shared_main_road():
    class C:
        def __init__(self, roads):
            raw = build_route([ORIGIN, DEST])
            self.parsed = parse_route(raw)
            total = self.parsed.distance_m
            object.__setattr__(self.parsed, "road_distances", {r: total * share for r, share in roads.items()})

    a = C({"I-80": 0.7, "I-76": 0.3})
    b = C({"I-80": 0.6, "I-90": 0.4})
    c = C({})
    assert _name_routes([a, b, c]) == ["via I-80 · I-76", "via I-80 · I-90", "Route C"]


def test_identical_names_fall_back_to_letters():
    class C:
        def __init__(self):
            self.parsed = parse_route(build_route([ORIGIN, DEST], road="I-80 W"))

    assert _name_routes([C(), C()]) == ["Route A", "Route B"]
