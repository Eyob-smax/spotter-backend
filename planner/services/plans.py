"""Trip plans: route search, weather/risk analysis and the corridor heatmap.

A plan is the result of a route search, cached for ``PLAN_TTL_S``. Its id is
derived from the normalised inputs, so repeating the same search reuses the
cached routes, analyses and heatmap instead of calling the providers again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from django.conf import settings
from django.core.cache import cache

from planner.domain.forecast import HOUR, HourlySeries
from planner.domain.geo import LatLon, Polyline
from planner.domain.heatgrid import corridor_cells
from planner.domain.recommend import ScoredSegment, rank, summarize
from planner.domain.risk import UNKNOWN, classify
from planner.domain.sampling import place_checkpoints
from planner.exceptions import PlanExpired, UpstreamUnavailable

from .http import get_session
from .routes import find_routes
from .tomtom import TomTomClient
from .weather import ForecastResult, WeatherService, snap

logger = logging.getLogger(__name__)

PLAN_TTL_S = 60 * 60
HEATMAP_HOURS = 48
# Each Open-Meteo location counts as one API call and the free tier allows
# 600/min, so one full search (analysis + heatmap) is kept to ~340 locations;
# long trips sample weather on a coarser grid instead of exceeding it.
MAX_CHECKPOINT_CELLS = 120
CHECKPOINT_CELL_SIZES = (0.1, 0.2, 0.3, 0.4, 0.5)
MAX_HEATMAP_CELLS = 220
RATE_LIMIT_RETRY_S = 60


@dataclass(frozen=True)
class TripRequest:
    origin: dict  # {"lat", "lon", "label"}
    destination: dict
    departure: datetime  # timezone-aware, UTC
    load_lb: int

    def plan_id(self) -> str:
        key = json.dumps(
            [
                round(self.origin["lat"], 5),
                round(self.origin["lon"], 5),
                round(self.destination["lat"], 5),
                round(self.destination["lon"], 5),
                self.departure.strftime("%Y-%m-%dT%H:%M"),
                self.load_lb,
            ]
        )
        return "pl_" + hashlib.sha256(key.encode()).hexdigest()[:20]


def iso(ts: float | datetime) -> str:
    dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, tz=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan_key(plan_id: str) -> str:
    return f"plan:v1:{plan_id}"


def tomtom_client() -> TomTomClient:
    return TomTomClient(settings.TOMTOM_API_KEY, settings.TOMTOM_BASE_URL, get_session())


def weather_service() -> WeatherService:
    return WeatherService(settings.OPEN_METEO_URL, get_session("weather"), cache, settings.OPEN_METEO_API_KEY)


def _require_weather(result: ForecastResult) -> None:
    if not result.all_failed:
        return
    if result.rate_limited:
        raise UpstreamUnavailable(
            "The weather provider's per-minute limit was reached. Please try again in a minute.",
            retry_after=RATE_LIMIT_RETRY_S,
        )
    raise UpstreamUnavailable("The weather service is unavailable right now. Please try again.")


def _weather_notice(result: ForecastResult) -> str | None:
    if result.complete:
        return None
    reason = (
        "the weather provider's per-minute limit was reached" if result.rate_limited else "the weather provider failed"
    )
    return f"Some forecasts could not be loaded because {reason}; retry in a minute for full coverage."


def _line_to_store(line: Polyline) -> dict:
    return {
        "points": [[round(lat, 6), round(lon, 6)] for lat, lon in line.points],
        "dist_m": [round(d, 1) for d in line.dist_m],
        "time_s": [round(t, 1) for t in line.time_s],
    }


def _line_from_store(data: dict) -> Polyline:
    return Polyline(points=[tuple(p) for p in data["points"]], dist_m=data["dist_m"], time_s=data["time_s"])


# --- Route search -----------------------------------------------------------------------


def create_plan(trip: TripRequest) -> dict:
    plan_id = trip.plan_id()
    cached = cache.get(_plan_key(plan_id))
    if cached is not None:
        return public_plan(cached)

    origin: LatLon = (trip.origin["lat"], trip.origin["lon"])
    destination: LatLon = (trip.destination["lat"], trip.destination["lon"])
    routes, notices = find_routes(tomtom_client(), origin, destination, load_lb=trip.load_lb, depart_at=trip.departure)
    departure_ts = trip.departure.timestamp()
    created = datetime.now(UTC).timestamp()
    plan = {
        "plan_id": plan_id,
        "created_at": created,
        "expires_at": created + PLAN_TTL_S,
        "origin": trip.origin,
        "destination": trip.destination,
        "departure_ts": departure_ts,
        "load_lb": trip.load_lb,
        "notices": notices,
        "routes": [
            {
                "id": r.id,
                "name": r.name,
                "source": r.source,
                "distance_m": round(r.parsed.distance_m),
                "duration_s": round(r.parsed.duration_s),
                "traffic_delay_s": round(r.parsed.traffic_delay_s),
                "line": _line_to_store(r.line),
            }
            for r in routes
        ],
    }
    cache.set(_plan_key(plan_id), plan, timeout=PLAN_TTL_S)
    logger.info("Plan %s created with %d routes", plan_id, len(routes))
    return public_plan(plan)


def load_plan(plan_id: str) -> dict:
    plan = cache.get(_plan_key(plan_id))
    if plan is None:
        raise PlanExpired()
    return plan


def public_plan(plan: dict) -> dict:
    departure = plan["departure_ts"]
    routes = []
    for r in plan["routes"]:
        line = r["line"]
        lats = [p[0] for p in line["points"]]
        lons = [p[1] for p in line["points"]]
        routes.append(
            {
                "id": r["id"],
                "name": r["name"],
                "source": r["source"],
                "distance_m": r["distance_m"],
                "duration_s": r["duration_s"],
                "traffic_delay_s": r["traffic_delay_s"],
                "departure_time": iso(departure),
                "arrival_time": iso(departure + r["duration_s"]),
                "bbox": [min(lons), min(lats), max(lons), max(lats)],
                "geometry": {"type": "LineString", "coordinates": [[p[1], p[0]] for p in line["points"]]},
                "cumulative": {"distance_m": line["dist_m"], "time_s": line["time_s"]},
            }
        )
    return {
        "plan_id": plan["plan_id"],
        "expires_at": iso(plan["expires_at"]),
        "origin": plan["origin"],
        "destination": plan["destination"],
        "departure_time": iso(departure),
        "load_weight_lb": plan["load_lb"],
        "routes": routes,
        "notices": plan["notices"],
    }


# --- Weather & risk analysis -------------------------------------------------------------


def _checkpoint_cell_size(points: list[LatLon]) -> float:
    for size in CHECKPOINT_CELL_SIZES:
        if len({snap(p, size) for p in points}) <= MAX_CHECKPOINT_CELLS:
            return size
    return CHECKPOINT_CELL_SIZES[-1]


def _assess(series: HourlySeries | None, eta_ts: float, load_lb: float):
    sample = series.sample(eta_ts) if series is not None else None
    if sample is None:
        return None, None
    return sample, classify(sample.wind_mph, sample.rain_in_hr, sample.snow_in_hr, load_lb)


def analyze(plan_id: str, interval_mi: int) -> dict:
    plan = load_plan(plan_id)
    cache_key = f"{_plan_key(plan_id)}:analysis:{interval_mi}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    departure = plan["departure_ts"]
    load_lb = plan["load_lb"]
    lines = {r["id"]: _line_from_store(r["line"]) for r in plan["routes"]}
    checkpoints = {rid: place_checkpoints(line, interval_mi) for rid, line in lines.items()}

    all_points = [cp.point for cps in checkpoints.values() for cp in cps]
    cell_size = _checkpoint_cell_size(all_points)
    until = departure + max(line.duration_s for line in lines.values()) + 2 * HOUR
    weather = weather_service().forecasts((snap(p, cell_size) for p in all_points), until_ts=until)
    _require_weather(weather)
    forecasts = weather.series

    routes_out, summaries = [], []
    for r in plan["routes"]:
        cps_out, segments = [], []
        for cp in checkpoints[r["id"]]:
            eta = departure + cp.elapsed_s
            sample, assessment = _assess(forecasts.get(snap(cp.point, cell_size)), eta, load_lb)
            conditions = None
            if sample is not None:
                conditions = sample.to_dict()
                conditions["forecast_time"] = iso(sample.forecast_time)
            cps_out.append(
                {
                    "index": cp.index,
                    "mile": round(cp.mile, 2),
                    "segment_mi": round(cp.segment_mi, 2),
                    "lat": round(cp.point[0], 5),
                    "lon": round(cp.point[1], 5),
                    "eta": iso(eta),
                    "progress": round(cp.progress, 5),
                    "weather": conditions,
                    "risk": assessment.level.id if assessment else UNKNOWN,
                    "score": assessment.score if assessment else None,
                    "factors": [f.to_dict() for f in assessment.factors] if assessment else [],
                }
            )
            segments.append(
                ScoredSegment(mile=cp.mile, segment_mi=cp.segment_mi, level=assessment.level if assessment else None)
            )
        summary = summarize(r["id"], r["duration_s"], segments)
        summaries.append(summary)
        routes_out.append({"id": r["id"], "checkpoints": cps_out, "summary": summary.to_dict()})

    notices = [n for n in (_weather_notice(weather),) if n]
    unknown = sum(s.unknown_count for s in summaries)
    if unknown:
        notices.append(
            f"Forecast unavailable for {unknown} checkpoint{'s' if unknown != 1 else ''}; "
            f"{'they are' if unknown != 1 else 'it is'} excluded from scoring."
        )
    if cell_size > 0.1:
        notices.append(f"Long trip: weather was sampled on a {cell_size:.1f}° grid to stay within provider limits.")

    result = {
        "plan_id": plan_id,
        "interval_mi": interval_mi,
        "generated_at": iso(datetime.now(UTC)),
        "weather_cell_deg": cell_size,
        "routes": routes_out,
        "recommendation": rank(summaries).to_dict(),
        "notices": notices,
    }
    if weather.complete:  # never pin a partial result: a retry should fetch the missing forecasts
        cache.set(cache_key, result, timeout=_remaining_ttl(plan))
    return result


# --- Corridor heatmap --------------------------------------------------------------------


def heatmap(plan_id: str) -> dict:
    plan = load_plan(plan_id)
    cache_key = f"{_plan_key(plan_id)}:heatmap"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    start = math.floor(plan["departure_ts"] / HOUR) * HOUR
    hours = HEATMAP_HOURS + 1
    lines = [_line_from_store(r["line"]) for r in plan["routes"]]
    size, centres = corridor_cells(lines, max_cells=MAX_HEATMAP_CELLS)
    weather = weather_service().forecasts(centres, until_ts=start + HEATMAP_HOURS * HOUR)
    _require_weather(weather)
    forecasts = weather.series

    scores = []
    for cell in centres:
        series = forecasts.get(cell)
        row = []
        for h in range(hours):
            _, assessment = _assess(series, start + h * HOUR, plan["load_lb"])
            row.append(assessment.score if assessment else None)
        scores.append(row)

    result = {
        "plan_id": plan_id,
        "start_time": iso(start),
        "hours": hours,
        "step_s": HOUR,
        "cell_size_deg": size,
        "cells": [[lon, lat] for lat, lon in centres],
        "scores": scores,
        "complete": weather.complete,
    }
    if weather.complete:
        cache.set(cache_key, result, timeout=_remaining_ttl(plan))
    return result


def _remaining_ttl(plan: dict) -> int:
    return max(60, int(plan["expires_at"] - datetime.now(UTC).timestamp()))
