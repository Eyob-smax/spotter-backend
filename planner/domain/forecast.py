"""Hourly forecast series and ETA → forecast-hour alignment.

Open-Meteo semantics (verified against its docs):
* ``wind_speed_10m`` is an instantaneous value at the timestamp → use the hour nearest the ETA.
* ``rain``, ``showers``, ``snowfall`` and ``wind_gusts_10m`` aggregate the *preceding* hour
  (the value stamped T covers T-1h..T) → use the hour whose window contains the ETA,
  i.e. the first timestamp at or after it.
Rain rate is ``rain + showers`` (``rain`` alone excludes convective showers).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

HOUR = 3600


def hour_ending_at_or_after(ts: float) -> int:
    return int(math.ceil(ts / HOUR) * HOUR)


def nearest_hour(ts: float) -> int:
    return int(math.floor(ts / HOUR + 0.5) * HOUR)


@dataclass(frozen=True)
class WeatherSample:
    wind_mph: float
    gust_mph: float | None
    rain_in_hr: float
    snow_in_hr: float
    forecast_time: int  # unix seconds of the precipitation hour used

    def to_dict(self) -> dict:
        return {
            "wind_mph": round(self.wind_mph, 1),
            "gust_mph": None if self.gust_mph is None else round(self.gust_mph, 1),
            "rain_in_hr": round(self.rain_in_hr, 3),
            "snow_in_hr": round(self.snow_in_hr, 3),
            "forecast_time": self.forecast_time,
        }


@dataclass(frozen=True)
class HourlySeries:
    """Regular hourly series starting at ``start`` (unix seconds, on the hour)."""

    start: int
    wind_mph: list[float | None]
    gust_mph: list[float | None]
    rain_in: list[float | None]
    showers_in: list[float | None]
    snow_in: list[float | None]

    @property
    def end(self) -> int:
        return self.start + (len(self.wind_mph) - 1) * HOUR

    def _value(self, values: list[float | None], ts: int) -> float | None:
        if ts % HOUR:
            raise ValueError("timestamp must be on the hour")
        i = (ts - self.start) // HOUR
        if 0 <= i < len(values):
            return values[i]
        return None

    def sample(self, eta: float) -> WeatherSample | None:
        """Conditions a truck meets at ``eta``; None if the forecast does not cover it."""
        precip_ts = hour_ending_at_or_after(eta)
        wind = self._value(self.wind_mph, nearest_hour(eta))
        rain = self._value(self.rain_in, precip_ts)
        showers = self._value(self.showers_in, precip_ts)
        snow = self._value(self.snow_in, precip_ts)
        if wind is None or rain is None or snow is None:
            return None
        return WeatherSample(
            wind_mph=wind,
            gust_mph=self._value(self.gust_mph, precip_ts),
            rain_in_hr=rain + (showers or 0.0),
            snow_in_hr=snow,
            forecast_time=precip_ts,
        )
