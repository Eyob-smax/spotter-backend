"""TomTom Routing and Search API adapter.

Only this module knows TomTom's request/response formats; the rest of the app
works with ``ParsedRoute`` and plain dicts.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

import requests

from planner.domain.geo import LB_PER_KG, LatLon, Polyline, cumulative_distances, haversine_m
from planner.exceptions import NoRouteFound, PointNotRoutable, UpstreamTimeout, UpstreamUnavailable

logger = logging.getLogger(__name__)

ROUTE_TIMEOUT = (5, 45)  # long-haul alternatives take up to ~20 s to compute
SEARCH_TIMEOUT = (3, 8)
_DIRECTION_SUFFIX = re.compile(r"\s+[NSEW]$")


@dataclass(frozen=True)
class ParsedRoute:
    line: Polyline
    distance_m: float
    duration_s: float
    traffic_delay_s: float
    departure_time: str
    arrival_time: str
    road_distances: dict[str, float] = field(default_factory=dict)

    @property
    def main_roads(self) -> list[str]:
        """Road numbers ordered by distance driven on them."""
        return [name for name, _ in sorted(self.road_distances.items(), key=lambda kv: -kv[1])]


def lb_to_kg(lb: float) -> int:
    return round(lb / LB_PER_KG)


def _fmt_point(p: LatLon) -> str:
    return f"{p[0]:.6f},{p[1]:.6f}"


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _per_vertex_progress(points: list[LatLon], progress: list[dict]) -> tuple[list[float], list[float]]:
    """Expand TomTom's sparse progress list into cumulative distance/time for every vertex.

    TomTom reports exact cumulative distance and time at selected point indices
    (where speed changes); between them speed is constant, so we split each run
    by the haversine share of its vertices.
    """
    n = len(points)
    anchors = sorted(
        (int(p["pointIndex"]), float(p.get("distanceInMeters", 0.0)), float(p.get("travelTimeInSeconds", 0.0)))
        for p in progress
        if 0 <= int(p.get("pointIndex", -1)) < n
    )
    if not anchors or anchors[0][0] != 0:
        anchors.insert(0, (0, 0.0, 0.0))

    dist = [0.0] * n
    time = [0.0] * n
    for (i0, d0, t0), (i1, d1, t1) in zip(anchors, anchors[1:]):
        hops = [haversine_m(points[k], points[k + 1]) for k in range(i0, i1)]
        total = sum(hops)
        acc = 0.0
        for k in range(i0, i1 + 1):
            share = (acc / total) if total > 0 else ((k - i0) / max(i1 - i0, 1))
            dist[k] = d0 + (d1 - d0) * share
            time[k] = t0 + (t1 - t0) * share
            if k < i1:
                acc += hops[k - i0]

    # Vertices after the last anchor (shouldn't happen): extrapolate at the last known speed.
    last_i, last_d, last_t = anchors[-1]
    if last_i < n - 1:
        speed = (last_d / last_t) if last_t > 0 else 20.0
        for k in range(last_i + 1, n):
            dist[k] = dist[k - 1] + haversine_m(points[k - 1], points[k])
            time[k] = time[k - 1] + (dist[k] - dist[k - 1]) / speed

    # Guard against tiny non-monotonic rounding in upstream data.
    for k in range(1, n):
        dist[k] = max(dist[k], dist[k - 1])
        time[k] = max(time[k], time[k - 1])
    return dist, time


def _road_distances(instructions: list[dict], total_m: float) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for cur, nxt in zip(instructions, instructions[1:] + [{"routeOffsetInMeters": total_m}]):
        numbers = cur.get("roadNumbers") or []
        if not numbers:
            continue
        length = float(nxt.get("routeOffsetInMeters", total_m)) - float(cur.get("routeOffsetInMeters", 0))
        if length > 0:
            out[_DIRECTION_SUFFIX.sub("", numbers[0])] += length
    return dict(out)


def parse_route(raw: dict) -> ParsedRoute:
    points: list[LatLon] = [(float(p["latitude"]), float(p["longitude"])) for leg in raw["legs"] for p in leg["points"]]
    dist, time = _per_vertex_progress(points, raw.get("progress") or [])
    summary = raw["summary"]
    total_m = float(summary["lengthInMeters"])
    total_s = float(summary["travelTimeInSeconds"])
    if dist[-1] <= 0 or not raw.get("progress"):
        # No progress data: fall back to geometry distance and proportional time.
        dist = cumulative_distances(points)
        time = [total_s * d / dist[-1] if dist[-1] else 0.0 for d in dist]
    instructions = (raw.get("guidance") or {}).get("instructions") or []
    return ParsedRoute(
        line=Polyline(points=points, dist_m=dist, time_s=time),
        distance_m=total_m,
        duration_s=total_s,
        traffic_delay_s=float(summary.get("trafficDelayInSeconds", 0)),
        departure_time=summary.get("departureTime", ""),
        arrival_time=summary.get("arrivalTime", ""),
        road_distances=_road_distances(instructions, total_m),
    )


class TomTomClient:
    def __init__(self, api_key: str, base_url: str, session: requests.Session):
        if not api_key:
            raise UpstreamUnavailable("Routing is not configured on the server.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = session

    # -- transport -------------------------------------------------------------
    def _get(self, path: str, params: list[tuple[str, str]], timeout) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(url, params=[*params, ("key", self.api_key)], timeout=timeout)
        except requests.Timeout as exc:
            logger.warning("TomTom timeout on %s", path.split("/")[1:3])
            raise UpstreamTimeout("The routing service took too long to respond.") from exc
        except requests.RequestException as exc:
            logger.warning("TomTom connection error: %s", type(exc).__name__)
            raise UpstreamUnavailable("The routing service is unreachable.") from exc

        if resp.status_code == 200:
            return resp.json()
        self._raise_for(resp)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _raise_for(resp: requests.Response) -> None:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        detail = body.get("detailedError") or {}
        code = detail.get("code") or (body.get("error") or {}).get("code") or ""
        message = detail.get("message") or body.get("errorText") or ""

        if code == "MAP_MATCHING_FAILURE":
            lowered = message.lower()
            if "origin" in lowered:
                raise PointNotRoutable("No truck-accessible road near the origin.", field="origin")
            if "destination" in lowered:
                raise PointNotRoutable("No truck-accessible road near the destination.", field="destination")
            raise PointNotRoutable("No truck-accessible road near a waypoint.", field="waypoint")
        if code in {"NO_ROUTE_FOUND", "CANNOT_RESTORE_BASEROUTE"} or "no route" in message.lower():
            raise NoRouteFound()
        if resp.status_code in (401, 403):
            logger.error("TomTom rejected the API key (HTTP %s)", resp.status_code)
            raise UpstreamUnavailable("The routing service rejected our credentials.")
        if resp.status_code == 429:
            raise UpstreamUnavailable("The routing service is rate limiting us. Please try again shortly.")
        logger.error("TomTom error HTTP %s code=%s message=%s", resp.status_code, code, message[:200])
        raise UpstreamUnavailable()

    # -- routing ---------------------------------------------------------------
    def calculate_routes(
        self,
        locations: list[LatLon],
        *,
        load_lb: float,
        depart_at: datetime,
        max_alternatives: int,
    ) -> list[ParsedRoute]:
        path = "/routing/1/calculateRoute/" + ":".join(_fmt_point(p) for p in locations) + "/json"
        params = [
            ("travelMode", "truck"),
            ("vehicleCommercial", "true"),
            ("vehicleWeight", str(lb_to_kg(load_lb))),
            ("routeType", "fastest"),
            ("traffic", "true"),
            ("departAt", _fmt_time(depart_at)),
            ("maxAlternatives", str(max_alternatives)),
            ("computeTravelTimeFor", "all"),
            ("instructionsType", "coded"),
            ("extendedRouteRepresentation", "distance"),
            ("extendedRouteRepresentation", "travelTime"),
        ]
        body = self._get(path, params, ROUTE_TIMEOUT)
        routes = body.get("routes") or []
        if not routes:
            raise NoRouteFound()
        return [parse_route(r) for r in routes]

    # -- search ----------------------------------------------------------------
    def search(self, query: str, *, bias: LatLon | None = None, limit: int = 6) -> list[dict]:
        params = [("typeahead", "true"), ("limit", str(limit)), ("language", "en-US")]
        if bias:
            params += [("lat", f"{bias[0]:.4f}"), ("lon", f"{bias[1]:.4f}")]
        body = self._get(f"/search/2/search/{quote(query, safe='')}.json", params, SEARCH_TIMEOUT)
        return [self._search_result(r) for r in body.get("results", []) if "position" in r]

    def reverse(self, point: LatLon) -> dict:
        body = self._get(f"/search/2/reverseGeocode/{point[0]:.6f},{point[1]:.6f}.json", [], SEARCH_TIMEOUT)
        addresses = body.get("addresses") or []
        label = (addresses[0].get("address") or {}).get("freeformAddress") if addresses else None
        return {"label": label or f"{point[0]:.5f}, {point[1]:.5f}", "lat": point[0], "lon": point[1]}

    @staticmethod
    def _search_result(r: dict) -> dict:
        address = r.get("address") or {}
        kind = r.get("type", "")
        region = ", ".join(
            x
            for x in (
                address.get("countrySubdivisionName") or address.get("countrySubdivision"),
                address.get("country"),
            )
            if x
        )
        if kind == "POI":
            label = (r.get("poi") or {}).get("name") or address.get("freeformAddress", "")
            secondary = address.get("freeformAddress", "")
            result_type = "poi"
        elif kind == "Geography":
            label = address.get("freeformAddress") or address.get("municipality", "")
            secondary = region
            result_type = "city" if r.get("entityType") in {"Municipality", "MunicipalitySubdivision"} else "region"
        else:
            label = address.get("freeformAddress", "")
            secondary = region
            result_type = "street" if kind in {"Street", "Cross Street"} else "address"
        return {
            "id": r.get("id", ""),
            "label": label,
            "secondary": secondary,
            "lat": float(r["position"]["lat"]),
            "lon": float(r["position"]["lon"]),
            "type": result_type,
        }
