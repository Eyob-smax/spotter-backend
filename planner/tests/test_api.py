"""End-to-end API tests: real views, services and domain code; only the HTTP providers are faked."""

from datetime import UTC, datetime, timedelta

import pytest
from django.test import override_settings
from rest_framework.throttling import ScopedRateThrottle

from planner.services import plans

from .fakes import calm

ORIGIN = {"lat": 39.74, "lon": -104.99, "label": "Denver, CO"}
DEST = {"lat": 40.76, "lon": -111.89, "label": "Salt Lake City, UT"}


def departure(hours=1):
    return (datetime.now(UTC) + timedelta(hours=hours)).replace(microsecond=0)


def trip(**overrides):
    body = {
        "origin": ORIGIN,
        "destination": DEST,
        "departure_time": departure().isoformat().replace("+00:00", "Z"),
        "load_weight_lb": 42_000,
    }
    body.update(overrides)
    return body


def storm_on_fastest_route(wind):
    """High wind on the straight (fastest) fake route's middle stretch; calm elsewhere."""

    def weather(lat, lon, ts):
        if 40.1 <= lat <= 40.4 and -109.0 <= lon <= -107.8:
            return (wind, wind + 10, 0.0, 0.0, 0.0)
        return calm(lat, lon, ts)

    return weather


def create(api, **overrides):
    resp = api.post("/api/routes", trip(**overrides), format="json")
    assert resp.status_code == 200, resp.json()
    return resp.json()


class TestRoutes:
    def test_happy_path(self, api, tomtom):
        body = create(api)
        assert body["plan_id"].startswith("pl_") and len(body["plan_id"]) == 23
        assert [r["id"] for r in body["routes"]] == ["A", "B", "C"]
        route = body["routes"][0]
        coords = route["geometry"]["coordinates"]
        assert coords[0] == pytest.approx([ORIGIN["lon"], ORIGIN["lat"]], abs=1e-5)  # GeoJSON is [lon, lat]
        assert len(coords) == len(route["cumulative"]["distance_m"]) == len(route["cumulative"]["time_s"])
        assert route["cumulative"]["time_s"][-1] == pytest.approx(route["duration_s"], abs=1)
        dep = datetime.fromisoformat(route["departure_time"].replace("Z", "+00:00"))
        arr = datetime.fromisoformat(route["arrival_time"].replace("Z", "+00:00"))
        assert (arr - dep).total_seconds() == pytest.approx(route["duration_s"], abs=1)
        assert route["bbox"][0] <= ORIGIN["lon"] <= route["bbox"][2]
        assert body["load_weight_lb"] == 42_000 and body["notices"] == []

    def test_same_search_is_served_from_cache(self, api, tomtom):
        dep = departure().isoformat()
        first = create(api, departure_time=dep)
        second = create(api, departure_time=dep)
        assert first["plan_id"] == second["plan_id"]
        assert tomtom.route_calls == 1

    def test_recent_past_departure_is_clamped_to_now(self, api, tomtom):
        body = create(api, departure_time=(datetime.now(UTC) - timedelta(minutes=10)).isoformat())
        dep = datetime.fromisoformat(body["departure_time"].replace("Z", "+00:00"))
        assert abs((dep - datetime.now(UTC)).total_seconds()) < 120

    def test_naive_departure_is_utc(self, api, tomtom):
        naive = (datetime.now(UTC) + timedelta(hours=3)).replace(tzinfo=None, second=0, microsecond=0)
        body = create(api, departure_time=naive.isoformat())
        assert body["departure_time"] == naive.strftime("%Y-%m-%dT%H:%M:00Z")

    @pytest.mark.parametrize(
        "overrides, field",
        [
            ({"departure_time": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}, "departure_time"),
            ({"departure_time": (datetime.now(UTC) + timedelta(days=8)).isoformat()}, "departure_time"),
            ({"load_weight_lb": 999}, "load_weight_lb"),
            ({"load_weight_lb": 120_001}, "load_weight_lb"),
            ({"origin": {"lat": 91, "lon": 0}}, "origin.lat"),
            ({"destination": {"lat": 39.7401, "lon": -104.9901}}, "destination"),
            ({"departure_time": "tomorrow"}, "departure_time"),
        ],
    )
    def test_validation(self, api, tomtom, overrides, field):
        resp = api.post("/api/routes", trip(**overrides), format="json")
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert error["code"] == "validation_error"
        assert field in error["fields"]
        assert tomtom.route_calls == 0

    def test_unroutable_origin(self, api, tomtom):
        tomtom.error = (400, {"detailedError": {"code": "MAP_MATCHING_FAILURE", "message": "Origin (35, -140)"}})
        resp = api.post("/api/routes", trip(), format="json")
        assert resp.status_code == 422
        assert resp.json()["error"] == {
            "code": "point_not_routable",
            "message": "No truck-accessible road near the origin.",
            "field": "origin",
        }

    def test_malformed_json(self, api):
        resp = api.post("/api/routes", "{not json", content_type="application/json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "bad_request"


class TestAnalysis:
    def test_recommends_slower_route_around_storm(self, api, tomtom, open_meteo):
        open_meteo.weather_fn = storm_on_fastest_route(wind=38)  # High; Severe for loads > 40,000 lb
        plan = create(api, load_weight_lb=42_000)
        resp = api.post(f"/api/plans/{plan['plan_id']}/analysis", {"interval_mi": 25}, format="json")
        assert resp.status_code == 200
        body = resp.json()
        a = next(r for r in body["routes"] if r["id"] == "A")
        assert a["summary"]["severe_mi"] > 0 and a["summary"]["max_risk"] == "severe"
        rec = body["recommendation"]
        assert rec["route_id"] == "B"
        assert rec["deciding_factor"] == "severe_mi"
        assert rec["reason"].startswith("Fewest severe-risk miles: 0 mi vs")
        stormy = next(cp for cp in a["checkpoints"] if cp["risk"] == "severe")
        load_factor = next(f for f in stormy["factors"] if f["type"] == "load")
        assert "42,000 lb" in load_factor["message"] and load_factor["decisive"]

    def test_lighter_load_changes_the_deciding_factor(self, api, tomtom, open_meteo):
        open_meteo.weather_fn = storm_on_fastest_route(wind=38)
        plan = create(api, load_weight_lb=30_000)
        body = api.post(f"/api/plans/{plan['plan_id']}/analysis", {"interval_mi": 25}, format="json").json()
        assert body["recommendation"]["route_id"] == "B"
        assert body["recommendation"]["deciding_factor"] == "high_mi"

    def test_no_travel_marks_route_blocked(self, api, tomtom, open_meteo):
        open_meteo.weather_fn = storm_on_fastest_route(wind=56)
        plan = create(api)
        body = api.post(f"/api/plans/{plan['plan_id']}/analysis", {}, format="json").json()
        a = next(r for r in body["routes"] if r["id"] == "A")
        assert a["summary"]["blocked"] and a["summary"]["first_blocked_mile"] > 0
        assert body["recommendation"]["deciding_factor"] == "no_travel_mi"
        assert body["recommendation"]["all_blocked"] is False

    def test_checkpoint_shape_and_spacing(self, api, tomtom, open_meteo):
        plan = create(api)
        body = api.post(f"/api/plans/{plan['plan_id']}/analysis", {"interval_mi": 50}, format="json").json()
        assert body["interval_mi"] == 50
        cps = body["routes"][0]["checkpoints"]
        assert [round(c["mile"]) for c in cps[:3]] == [0, 50, 100]
        assert cps[0]["eta"] == plan["departure_time"]
        assert set(cps[1]) == {
            "index",
            "mile",
            "segment_mi",
            "lat",
            "lon",
            "eta",
            "progress",
            "weather",
            "risk",
            "score",
            "factors",
        }
        assert set(cps[1]["weather"]) == {"wind_mph", "gust_mph", "rain_in_hr", "snow_in_hr", "forecast_time"}
        etas = [c["eta"] for c in cps]
        assert etas == sorted(etas)
        total_mi = plan["routes"][0]["distance_m"] / 1609.344
        assert sum(c["segment_mi"] for c in cps) == pytest.approx(total_mi, abs=0.1)

    def test_default_interval_is_25(self, api, tomtom, open_meteo):
        plan = create(api)
        assert api.post(f"/api/plans/{plan['plan_id']}/analysis", {}, format="json").json()["interval_mi"] == 25

    def test_invalid_interval(self, api, tomtom, open_meteo):
        plan = create(api)
        resp = api.post(f"/api/plans/{plan['plan_id']}/analysis", {"interval_mi": 15}, format="json")
        assert resp.status_code == 400 and "interval_mi" in resp.json()["error"]["fields"]

    def test_expired_plan(self, api):
        resp = api.post("/api/plans/pl_0123456789abcdef0123/analysis", {}, format="json")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "plan_expired"

    def test_result_is_cached(self, api, tomtom, open_meteo):
        plan = create(api)
        api.post(f"/api/plans/{plan['plan_id']}/analysis", {}, format="json")
        fetched = open_meteo.locations_fetched
        api.post(f"/api/plans/{plan['plan_id']}/analysis", {}, format="json")
        assert open_meteo.locations_fetched == fetched

    def test_weather_down_is_502(self, api, tomtom, open_meteo):
        plan = create(api)
        open_meteo.status = 500
        resp = api.post(f"/api/plans/{plan['plan_id']}/analysis", {}, format="json")
        assert resp.status_code == 502 and resp.json()["error"]["code"] == "upstream_unavailable"

    def test_weather_rate_limit_asks_to_retry(self, api, tomtom, open_meteo):
        plan = create(api)
        open_meteo.status = 429
        resp = api.post(f"/api/plans/{plan['plan_id']}/analysis", {}, format="json")
        assert resp.status_code == 502
        assert resp.json()["error"]["retry_after"] == plans.RATE_LIMIT_RETRY_S

    def test_partial_weather_is_reported_and_not_cached(self, api, tomtom, open_meteo, monkeypatch):
        # Small sequential batches; the second batch of the first analysis fails.
        monkeypatch.setattr("planner.services.weather.BATCH_SIZE", 5)
        monkeypatch.setattr("planner.services.weather.PARALLEL_BATCHES", 1)
        plan = create(api)
        svc = plans.weather_service()
        original = svc._fetch_batch
        state = {"n": 0}

        def failing_once(cells, days):
            state["n"] += 1
            return [None] * len(cells) if state["n"] == 2 else original(cells, days)

        monkeypatch.setattr(plans, "weather_service", lambda: svc)
        monkeypatch.setattr(svc, "_fetch_batch", failing_once)
        body = api.post(f"/api/plans/{plan['plan_id']}/analysis", {}, format="json").json()
        assert any("could not be loaded" in n for n in body["notices"])
        assert sum(r["summary"]["unknown_count"] for r in body["routes"]) > 0
        unknown = next(cp for r in body["routes"] for cp in r["checkpoints"] if cp["risk"] == "unknown")
        assert unknown["weather"] is None and unknown["score"] is None
        again = api.post(f"/api/plans/{plan['plan_id']}/analysis", {}, format="json").json()
        assert all(r["summary"]["unknown_count"] == 0 for r in again["routes"])  # refetched, not cached


class TestHeatmap:
    def test_shape_and_scores(self, api, tomtom, open_meteo):
        open_meteo.weather_fn = storm_on_fastest_route(wind=60)
        plan = create(api)
        resp = api.get(f"/api/plans/{plan['plan_id']}/heatmap")
        assert resp.status_code == 200
        body = resp.json()
        assert body["hours"] == 49 and body["step_s"] == 3600 and body["complete"] is True
        assert len(body["scores"]) == len(body["cells"]) <= plans.MAX_HEATMAP_CELLS
        assert all(len(row) == 49 for row in body["scores"])
        flat = [v for row in body["scores"] for v in row]
        assert max(flat) == 4.0 and min(flat) < 1
        start = datetime.fromisoformat(body["start_time"].replace("Z", "+00:00"))
        assert start.minute == 0 and start.second == 0

    def test_heatmap_expired(self, api):
        assert api.get("/api/plans/pl_0123456789abcdef0123/heatmap").status_code == 404


class TestGeocode:
    def test_search_validation(self, api):
        resp = api.get("/api/geocode/search", {"q": "a"})
        assert resp.status_code == 400 and "q" in resp.json()["error"]["fields"]
        resp = api.get("/api/geocode/search", {"q": "denver", "lat": 1})
        assert resp.status_code == 400

    def test_search_is_cached(self, api, rsps):
        import re

        import responses

        rsps.add(
            responses.GET,
            re.compile(r".*/search/2/search/.*"),
            json={
                "results": [
                    {
                        "type": "Geography",
                        "id": "g",
                        "address": {"freeformAddress": "Denver, CO"},
                        "position": {"lat": 39.7, "lon": -105.0},
                    }
                ]
            },
        )
        first = api.get("/api/geocode/search", {"q": "Denver "}).json()
        second = api.get("/api/geocode/search", {"q": "denver"}).json()
        assert first == second and first["results"][0]["label"] == "Denver, CO"
        assert len(rsps.calls) == 1

    def test_reverse(self, api, rsps):
        import re

        import responses

        rsps.add(responses.GET, re.compile(r".*/reverseGeocode/.*"), json={"addresses": []})
        resp = api.get("/api/geocode/reverse", {"lat": 39.7, "lon": -105})
        assert resp.status_code == 200 and resp.json()["label"] == "39.70000, -105.00000"
        assert api.get("/api/geocode/reverse", {"lat": 100, "lon": 0}).status_code == 400


class TestPlatform:
    def test_meta(self, api):
        body = api.get("/api/meta").json()
        assert body["ranking"][0] == "no_travel_mi"
        assert body["intervals_mi"] == [10, 25, 50]
        assert body["thresholds"]["wind_mph"]["no_travel"] == 55

    def test_health(self, api):
        assert api.get("/api/health").json() == {"status": "ok", "cache": "ok"}

    def test_request_id_header(self, api):
        resp = api.get("/api/meta", HTTP_X_REQUEST_ID="abc123")
        assert resp["X-Request-ID"] == "abc123"
        assert len(api.get("/api/meta")["X-Request-ID"]) == 12

    @override_settings(DEBUG=False)
    def test_unknown_url_is_json_404(self, api):
        resp = api.get("/api/does-not-exist")
        assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"

    def test_method_not_allowed(self, api):
        resp = api.get("/api/routes")
        assert resp.status_code == 405 and resp.json()["error"]["code"] == "method_not_allowed"

    def test_throttling(self, api, monkeypatch):
        monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"geocode": "2/min"})
        for _ in range(2):
            api.get("/api/geocode/search", {"q": "a"})
        resp = api.get("/api/geocode/search", {"q": "a"})
        assert resp.status_code == 429
        error = resp.json()["error"]
        assert error["code"] == "rate_limited" and error["retry_after"] >= 1
        assert resp["Retry-After"] == str(error["retry_after"])

    def test_openapi_schema(self, api):
        resp = api.get("/api/schema")
        assert resp.status_code == 200
        assert b"/api/routes" in resp.content
