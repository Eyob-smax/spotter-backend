"""Open-Meteo hourly forecasts for many locations, batched and cached per grid cell.

* Locations are snapped to a grid cell (multiples of 0.1°); each cell is fetched once.
* Cache key: ``wx:v2:{lat}:{lon}:d{days}``; a cached series is reused while it
  still covers the requested end time.
* Misses are fetched 150 locations per request (≈2.5 KB URL), up to 4 requests in parallel.
* A failed batch does not fail the whole request: those cells come back as ``None``.
* On HTTP 429 (free tier: 600 locations/min) we stop calling for 60 s instead of
  retrying, and report ``rate_limited`` so the API can ask the user to retry.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests
from django.core.cache import BaseCache

from planner.domain.forecast import HOUR, HourlySeries
from planner.domain.geo import LatLon

logger = logging.getLogger(__name__)

# Risk uses wind, gusts, rain, showers and snowfall; the rest is shown to the user.
# 9 variables stay under Open-Meteo's 10-variable weighting step (1 call per location).
HOURLY_VARIABLES = (
    "wind_speed_10m",
    "wind_gusts_10m",
    "rain",
    "showers",
    "snowfall",
    "temperature_2m",
    "precipitation_probability",
    "visibility",
    "weather_code",
)
DAY_BUCKETS = (3, 7, 10, 16)
BATCH_SIZE = 150
PARALLEL_BATCHES = 4
CACHE_TTL_S = 30 * 60
TIMEOUT = (4, 12)  # fail fast; the UI offers "Retry weather check"
DAY = 86_400
COOLDOWN_KEY = "wx:cooldown"
COOLDOWN_S = 60

Cell = tuple[float, float]


def snap(point: LatLon, size_deg: float = 0.1) -> Cell:
    lat = round(round(point[0] / size_deg) * size_deg, 1) + 0.0  # + 0.0 turns -0.0 into 0.0
    lon = round(round(point[1] / size_deg) * size_deg, 1) + 0.0
    return (lat, lon)


def days_needed(until_ts: float, now_ts: float) -> int:
    """Open-Meteo forecasts start at today 00:00 UTC; how many days must be requested to reach until_ts."""
    midnight = math.floor(now_ts / DAY) * DAY
    needed = math.floor((until_ts - midnight) / DAY) + 1
    return next((b for b in DAY_BUCKETS if b >= needed), DAY_BUCKETS[-1])


def _cache_key(cell: Cell, days: int) -> str:
    return f"wx:v2:{cell[0]:.1f}:{cell[1]:.1f}:d{days}"


def parse_series(item: dict) -> HourlySeries:
    hourly = item["hourly"]
    times = hourly["time"]
    if not times or any(b - a != HOUR for a, b in zip(times, times[1:])):
        raise ValueError("Forecast times are not a regular hourly series.")
    return HourlySeries(
        start=int(times[0]),
        wind_mph=hourly["wind_speed_10m"],
        gust_mph=hourly.get("wind_gusts_10m") or [None] * len(times),
        rain_in=hourly["rain"],
        showers_in=hourly.get("showers") or [None] * len(times),
        snow_in=hourly["snowfall"],
        temperature_f=hourly.get("temperature_2m") or [],
        precip_probability=hourly.get("precipitation_probability") or [],
        visibility_ft=hourly.get("visibility") or [],
        weather_code=hourly.get("weather_code") or [],
    )


@dataclass
class ForecastResult:
    series: dict[Cell, HourlySeries | None]
    failed: int = 0  # cells we tried to fetch but could not
    rate_limited: bool = False

    @property
    def complete(self) -> bool:
        return self.failed == 0

    @property
    def all_failed(self) -> bool:
        return bool(self.series) and all(v is None for v in self.series.values())


class _RateLimited:
    """Sentinel returned by a batch that hit the provider's rate limit."""

    pass


class WeatherService:
    def __init__(self, url: str, session: requests.Session, cache: BaseCache, api_key: str = ""):
        self.url = url
        self.session = session
        self.cache = cache
        self.api_key = api_key

    def forecasts(self, cells: Iterable[Cell], until_ts: float, now_ts: float | None = None) -> ForecastResult:
        now_ts = time.time() if now_ts is None else now_ts
        unique = list(dict.fromkeys(cells))
        if not unique:
            return ForecastResult(series={})
        days = days_needed(until_ts, now_ts)
        result: dict[Cell, HourlySeries | None] = {}

        # Any cached bucket that is at least as long as needed can serve the request.
        for bucket in (b for b in DAY_BUCKETS if b >= days):
            pending = [c for c in unique if c not in result]
            if not pending:
                break
            keys = {_cache_key(c, bucket): c for c in pending}
            for key, series in self.cache.get_many(list(keys)).items():
                if series is not None and series.end >= until_ts:
                    result[keys[key]] = series

        misses = [c for c in unique if c not in result]
        if not misses:
            return ForecastResult(series=result)

        if self.cache.get(COOLDOWN_KEY):
            # The provider told us to back off; don't spend more of the per-minute quota.
            result.update(dict.fromkeys(misses))
            return ForecastResult(series=result, failed=len(misses), rate_limited=True)

        rate_limited = False
        batches = [misses[i : i + BATCH_SIZE] for i in range(0, len(misses), BATCH_SIZE)]
        with ThreadPoolExecutor(max_workers=min(PARALLEL_BATCHES, len(batches))) as pool:
            for batch, fetched in zip(batches, pool.map(lambda b: self._fetch_batch(b, days), batches)):
                if isinstance(fetched, _RateLimited):
                    rate_limited = True
                    fetched = [None] * len(batch)
                fresh = {}
                for cell, series in zip(batch, fetched):
                    result[cell] = series
                    if series is not None:
                        fresh[_cache_key(cell, days)] = series
                if fresh:
                    self.cache.set_many(fresh, timeout=CACHE_TTL_S)
        if rate_limited:
            self.cache.set(COOLDOWN_KEY, True, timeout=COOLDOWN_S)
        failed = sum(1 for c in misses if result.get(c) is None)
        return ForecastResult(series=result, failed=failed, rate_limited=rate_limited)

    def _fetch_batch(self, cells: list[Cell], days: int) -> list[HourlySeries | None] | _RateLimited:
        params = {
            "latitude": ",".join(f"{c[0]:.1f}" for c in cells),
            "longitude": ",".join(f"{c[1]:.1f}" for c in cells),
            "hourly": ",".join(HOURLY_VARIABLES),
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "temperature_unit": "fahrenheit",
            "timeformat": "unixtime",
            "timezone": "GMT",
            "forecast_days": days,
        }
        if self.api_key:
            params["apikey"] = self.api_key
        try:
            resp = self.session.get(self.url, params=params, timeout=TIMEOUT)
            if resp.status_code == 429:
                logger.warning("Open-Meteo rate limit hit for %d cells", len(cells))
                return _RateLimited()
            if resp.status_code != 200:
                logger.warning("Open-Meteo HTTP %s for %d cells: %s", resp.status_code, len(cells), resp.text[:200])
                return [None] * len(cells)
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Open-Meteo request failed for %d cells: %s", len(cells), type(exc).__name__)
            return [None] * len(cells)

        items = body if isinstance(body, list) else [body]
        if len(items) != len(cells):
            logger.warning("Open-Meteo returned %d results for %d cells", len(items), len(cells))
            return [None] * len(cells)
        out: list[HourlySeries | None] = []
        for item in items:
            try:
                out.append(parse_series(item))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Unparseable Open-Meteo item: %s", exc)
                out.append(None)
        return out
