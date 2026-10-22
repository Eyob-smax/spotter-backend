import json
import re
from datetime import UTC, datetime

import pytest
import requests
import responses

from planner.exceptions import NoRouteFound, PointNotRoutable, UpstreamTimeout, UpstreamUnavailable
from planner.services.http import build_session
from planner.services.tomtom import TomTomClient, _per_vertex_progress, lb_to_kg, parse_route

from .fakes import TOMTOM, build_route

DEPART = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
ORIGIN, DEST = (39.74, -104.99), (40.76, -111.89)


@pytest.fixture
def client():
    return TomTomClient("k", TOMTOM, build_session())


def test_lb_to_kg():
    assert lb_to_kg(42_000) == 19_051
    assert lb_to_kg(80_000) == 36_287


def test_missing_key_rejected():
    with pytest.raises(UpstreamUnavailable):
        TomTomClient("", TOMTOM, build_session())


class TestCalculateRoutes:
    def test_request_parameters(self, client, tomtom):
        client.calculate_routes([ORIGIN, DEST], load_lb=42_000, depart_at=DEPART, max_alternatives=2)
        params = tomtom.calls[0]["params"]
        assert params["travelMode"] == ["truck"]
        assert params["vehicleWeight"] == ["19051"]
        assert params["vehicleCommercial"] == ["true"]
        assert params["departAt"] == ["2026-10-23T14:00:00Z"]
        assert params["maxAlternatives"] == ["2"]
        assert sorted(params["extendedRouteRepresentation"]) == ["distance", "travelTime"]
        assert params["key"] == ["k"]
        assert tomtom.calls[0]["locations"] == [ORIGIN, DEST]

    def test_returns_parsed_alternatives(self, client, tomtom):
        routes = client.calculate_routes([ORIGIN, DEST], load_lb=42_000, depart_at=DEPART, max_alternatives=2)
        assert len(routes) == 3
        assert routes[0].main_roads[0] == "I-80"

    @pytest.mark.parametrize(
        "message, field",
        [
            ("MAP_MATCHING_FAILURE: Origin (35, -140)", "origin"),
            ("MAP_MATCHING_FAILURE: Destination (35, -140)", "destination"),
            ("MAP_MATCHING_FAILURE: Waypoint 1", "waypoint"),
        ],
    )
    def test_unroutable_point(self, client, tomtom, message, field):
        tomtom.error = (400, {"detailedError": {"code": "MAP_MATCHING_FAILURE", "message": message}})
        with pytest.raises(PointNotRoutable) as exc:
            client.calculate_routes([ORIGIN, DEST], load_lb=1, depart_at=DEPART, max_alternatives=0)
        assert exc.value.field == field

    def test_no_route(self, client, tomtom):
        tomtom.error = (400, {"detailedError": {"code": "NO_ROUTE_FOUND", "message": "No route found."}})
        with pytest.raises(NoRouteFound):
            client.calculate_routes([ORIGIN, DEST], load_lb=1, depart_at=DEPART, max_alternatives=0)

    @pytest.mark.parametrize("status", [401, 403, 429, 500])
    def test_upstream_errors(self, client, tomtom, status):
        tomtom.error = (status, {})
        with pytest.raises(UpstreamUnavailable):
            client.calculate_routes([ORIGIN, DEST], load_lb=1, depart_at=DEPART, max_alternatives=0)

    def test_empty_routes(self, client, rsps):
        rsps.add(responses.GET, re.compile(r".*calculateRoute.*"), json={"routes": []})
        with pytest.raises(NoRouteFound):
            client.calculate_routes([ORIGIN, DEST], load_lb=1, depart_at=DEPART, max_alternatives=0)

    def test_timeout(self, client, rsps):
        rsps.add(responses.GET, re.compile(r".*calculateRoute.*"), body=requests.Timeout())
        with pytest.raises(UpstreamTimeout):
            client.calculate_routes([ORIGIN, DEST], load_lb=1, depart_at=DEPART, max_alternatives=0)

    def test_connection_error(self, client, rsps):
        rsps.add(responses.GET, re.compile(r".*calculateRoute.*"), body=requests.ConnectionError())
        with pytest.raises(UpstreamUnavailable):
            client.calculate_routes([ORIGIN, DEST], load_lb=1, depart_at=DEPART, max_alternatives=0)


class TestParseRoute:
    def test_cumulative_values_match_progress_anchors(self):
        raw = build_route([ORIGIN, DEST])
        route = parse_route(raw)
        for anchor in raw["progress"]:
            i = anchor["pointIndex"]
            assert route.line.dist_m[i] == pytest.approx(anchor["distanceInMeters"])
            assert route.line.time_s[i] == pytest.approx(anchor["travelTimeInSeconds"])
        assert route.line.dist_m[-1] == pytest.approx(raw["summary"]["lengthInMeters"])
        assert route.line.time_s == sorted(route.line.time_s)

    def test_legs_are_concatenated_as_indexed_by_progress(self):
        via = (40.0, -108.0)
        raw = build_route([ORIGIN, via, DEST])
        route = parse_route(raw)
        assert len(route.line.points) == sum(len(leg["points"]) for leg in raw["legs"])
        assert raw["progress"][-1]["pointIndex"] == len(route.line.points) - 1

    def test_road_names_drop_direction_suffix(self):
        route = parse_route(build_route([ORIGIN, DEST], road="I-70 E"))
        assert route.main_roads == ["I-70", "CO-2"]
        assert route.road_distances["I-70"] > route.road_distances["CO-2"]

    def test_without_progress_falls_back_to_geometry(self):
        raw = build_route([ORIGIN, DEST])
        raw["progress"] = []
        route = parse_route(raw)
        assert route.line.time_s[-1] == pytest.approx(raw["summary"]["travelTimeInSeconds"])
        assert route.line.dist_m[-1] > 0

    def test_per_vertex_progress_extrapolates_after_last_anchor(self):
        points = [(0.0, 0.0), (0.0, 0.01), (0.0, 0.02), (0.0, 0.03)]
        dist, time = _per_vertex_progress(
            points,
            [
                {"pointIndex": 0, "distanceInMeters": 0, "travelTimeInSeconds": 0},
                {"pointIndex": 2, "distanceInMeters": 2000, "travelTimeInSeconds": 100},
            ],
        )
        assert dist[1] == pytest.approx(1000, rel=0.01)
        assert time[1] == pytest.approx(50, rel=0.01)
        assert dist[3] > dist[2] and time[3] > time[2]


class TestSearch:
    def test_maps_result_types(self, client, rsps):
        rsps.add(
            responses.GET,
            re.compile(r".*/search/2/search/.*"),
            json={
                "results": [
                    {
                        "type": "Geography",
                        "entityType": "Municipality",
                        "id": "g1",
                        "address": {
                            "freeformAddress": "Denver, CO",
                            "countrySubdivisionName": "Colorado",
                            "country": "United States",
                        },
                        "position": {"lat": 39.74, "lon": -104.99},
                    },
                    {
                        "type": "POI",
                        "id": "p1",
                        "poi": {"name": "Truck Stop"},
                        "address": {"freeformAddress": "1 Main St, Denver, CO"},
                        "position": {"lat": 39.7, "lon": -105.0},
                    },
                    {
                        "type": "Point Address",
                        "id": "a1",
                        "address": {
                            "freeformAddress": "2 Main St",
                            "countrySubdivision": "CO",
                            "country": "United States",
                        },
                        "position": {"lat": 39.71, "lon": -105.01},
                    },
                    {"type": "Street", "id": "nopos", "address": {}},
                ]
            },
        )
        results = client.search("den ver", bias=(39.7, -105.0))
        assert [r["type"] for r in results] == ["city", "poi", "address"]
        assert results[0] == {
            "id": "g1",
            "label": "Denver, CO",
            "secondary": "Colorado, United States",
            "lat": 39.74,
            "lon": -104.99,
            "type": "city",
        }
        assert results[1]["label"] == "Truck Stop" and results[1]["secondary"] == "1 Main St, Denver, CO"
        sent = rsps.calls[0].request.url
        assert "den%20ver.json" in sent and "typeahead=true" in sent and "lat=39.7000" in sent

    def test_reverse_with_and_without_address(self, client, rsps):
        rsps.add(
            responses.GET,
            re.compile(r".*/reverseGeocode/.*"),
            body=json.dumps({"addresses": [{"address": {"freeformAddress": "1437 N Bannock St, Denver"}}]}),
        )
        assert client.reverse((39.7392, -104.9903))["label"] == "1437 N Bannock St, Denver"
        rsps.replace(responses.GET, re.compile(r".*/reverseGeocode/.*"), json={"addresses": []})
        assert client.reverse((10.5, 20.25))["label"] == "10.50000, 20.25000"
