"""HTTP-level fakes for TomTom and Open-Meteo, driven through the `responses` library."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import responses

from planner.domain.geo import LatLon, destination_point, haversine_m

TOMTOM = "https://api.tomtom.com"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
ROUTE_RE = re.compile(r"https://api\.tomtom\.com/routing/1/calculateRoute/(?P<locs>[^/]+)/json")


def _leg_points(a: LatLon, b: LatLon, bulge_deg: float, n: int = 60) -> list[LatLon]:
    """Points from a to b, bowed sideways by `bulge_deg` at the middle (0 = straight)."""
    pts = []
    for k in range(n):
        f = k / (n - 1)
        lat = a[0] + (b[0] - a[0]) * f + bulge_deg * math.sin(math.pi * f)
        lon = a[1] + (b[1] - a[1]) * f
        pts.append((lat, lon))
    return pts


def build_route(locations: list[LatLon], bulge_deg: float = 0.0, speed_mps: float = 25.0, road: str = "I-80 W"):
    legs, all_points = [], []
    for a, b in zip(locations, locations[1:]):
        pts = _leg_points(a, b, bulge_deg)
        legs.append({"points": [{"latitude": la, "longitude": lo} for la, lo in pts]})
        all_points.extend(pts)
    dist = [0.0]
    for p, q in zip(all_points, all_points[1:]):
        dist.append(dist[-1] + haversine_m(p, q))
    total = dist[-1]
    progress = [
        {"pointIndex": i, "distanceInMeters": round(dist[i]), "travelTimeInSeconds": round(dist[i] / speed_mps)}
        for i in range(0, len(all_points), 10)
    ]
    if progress[-1]["pointIndex"] != len(all_points) - 1:
        progress.append(
            {
                "pointIndex": len(all_points) - 1,
                "distanceInMeters": round(total),
                "travelTimeInSeconds": round(total / speed_mps),
            }
        )
    return {
        "summary": {
            "lengthInMeters": round(total),
            "travelTimeInSeconds": round(total / speed_mps),
            "trafficDelayInSeconds": 0,
            "departureTime": "2026-10-23T14:00:00Z",
            "arrivalTime": "2026-10-23T20:00:00Z",
        },
        "legs": legs,
        "progress": progress,
        "guidance": {
            "instructions": [
                {"routeOffsetInMeters": 0, "roadNumbers": [road]},
                {"routeOffsetInMeters": round(total * 0.9), "roadNumbers": ["CO-2"]},
                {"routeOffsetInMeters": round(total)},
            ]
        },
    }


class FakeTomTom:
    """Answers calculateRoute with `alternatives` distinct bowed routes (fewer if asked)."""

    def __init__(self, rsps: responses.RequestsMock, alternatives: int = 3):
        self.alternatives = alternatives
        self.calls: list[dict] = []
        self.error: tuple[int, dict] | None = None
        self.detour_error: tuple[int, dict] | None = None
        rsps.add_callback(responses.GET, ROUTE_RE, callback=self._route)

    def _route(self, request):
        parsed = urlparse(request.url)
        params = parse_qs(parsed.query)
        locs = [tuple(map(float, p.split(","))) for p in ROUTE_RE.match(request.url)["locs"].split(":")]
        self.calls.append({"locations": locs, "params": params})
        is_detour = len(locs) > 2
        error = self.detour_error if is_detour else self.error
        if error:
            return error[0], {}, json.dumps(error[1])
        if is_detour:
            routes = [build_route(locs, speed_mps=24.0, road="US-40 W")]
        else:
            count = min(self.alternatives, int(params.get("maxAlternatives", ["0"])[0]) + 1)
            routes = [
                build_route(locs, bulge_deg=0.4 * k, speed_mps=25.0 - k, road=f"I-{80 - 10 * k} W")
                for k in range(count)
            ]
        return 200, {}, json.dumps({"formatVersion": "0.0.12", "routes": routes})

    @property
    def route_calls(self) -> int:
        return len(self.calls)


WeatherFn = Callable[[float, float, int], tuple[float, float, float, float, float]]


def calm(lat: float, lon: float, ts: int):
    return (8.0, 14.0, 0.0, 0.0, 0.0)


class FakeOpenMeteo:
    """Returns hourly series from today 00:00 UTC for every requested location, using `weather_fn`."""

    def __init__(self, rsps: responses.RequestsMock, weather_fn: WeatherFn = calm):
        self.weather_fn = weather_fn
        self.requests: list[int] = []  # number of locations per request
        self.status = 200
        rsps.add_callback(responses.GET, OPEN_METEO, callback=self._forecast)

    def _forecast(self, request):
        params = parse_qs(urlparse(request.url).query)
        lats = [float(x) for x in params["latitude"][0].split(",")]
        lons = [float(x) for x in params["longitude"][0].split(",")]
        self.requests.append(len(lats))
        if self.status != 200:
            return self.status, {}, json.dumps({"error": True, "reason": "fake failure"})
        days = int(params["forecast_days"][0])
        start = int(time.time() // 86_400 * 86_400)
        times = [start + h * 3600 for h in range(days * 24)]
        items = []
        for lat, lon in zip(lats, lons):
            rows = [self.weather_fn(lat, lon, t) for t in times]
            items.append(
                {
                    "latitude": lat,
                    "longitude": lon,
                    "hourly_units": {"wind_speed_10m": "mp/h", "rain": "inch", "snowfall": "inch"},
                    "hourly": {
                        "time": times,
                        "wind_speed_10m": [r[0] for r in rows],
                        "wind_gusts_10m": [r[1] for r in rows],
                        "rain": [r[2] for r in rows],
                        "showers": [r[3] for r in rows],
                        "snowfall": [r[4] for r in rows],
                    },
                }
            )
        body = items if len(items) > 1 else items[0]
        return 200, {}, json.dumps(body)

    @property
    def locations_fetched(self) -> int:
        return sum(self.requests)


def offset(point: LatLon, bearing: float, km: float) -> LatLon:
    return destination_point(point, bearing, km * 1000)
