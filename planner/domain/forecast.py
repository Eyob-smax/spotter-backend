"""Hourly forecast series and ETA → forecast-hour alignment.

Open-Meteo semantics (verified against its docs):
* ``wind_speed_10m`` is an instantaneous value at the timestamp → use the hour nearest the ETA.
* ``rain``, ``showers``, ``snowfall`` and ``wind_gusts_10m`` aggregate the *preceding* hour
  (the value stamped T covers T-1h..T) → use the hour whose window contains the ETA,
  i.e. the first timestamp at or after it.
Rain rate is ``rain + showers`` (``rain`` alone excludes convective showers).

Temperature, visibility and the weather code are instantaneous (nearest hour);
precipitation probability covers the preceding hour. These extra fields are
informational only: risk is scored from wind, rain and snow, as the brief defines.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .wmo import describe

HOUR = 3600
FEET_PER_MILE = 5280


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
    temperature_f: float | None = None
    precip_probability: float | None = None  # percent
    visibility_mi: float | None = None
    weather_code: int | None = None

    def to_dict(self) -> dict:
        def r(v: float | None, n: int) -> float | None:
            return None if v is None else round(v, n)

        return {
            "wind_mph": round(self.wind_mph, 1),
            "gust_mph": r(self.gust_mph, 1),
            "rain_in_hr": round(self.rain_in_hr, 3),
            "snow_in_hr": round(self.snow_in_hr, 3),
            "forecast_time": self.forecast_time,
            "temperature_f": r(self.temperature_f, 1),
            "precip_probability": None if self.precip_probability is None else round(self.precip_probability),
            "visibility_mi": r(self.visibility_mi, 1),
            "weather_code": self.weather_code,
            "conditions": describe(self.weather_code),
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
    temperature_f: list[float | None] = field(default_factory=list)
    precip_probability: list[float | None] = field(default_factory=list)
    visibility_ft: list[float | None] = field(default_factory=list)
    weather_code: list[float | None] = field(default_factory=list)

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
        instant_ts = nearest_hour(eta)
        visibility_ft = self._value(self.visibility_ft, instant_ts)
        code = self._value(self.weather_code, instant_ts)
        return WeatherSample(
            wind_mph=wind,
            gust_mph=self._value(self.gust_mph, precip_ts),
            rain_in_hr=rain + (showers or 0.0),
            snow_in_hr=snow,
            forecast_time=precip_ts,
            temperature_f=self._value(self.temperature_f, instant_ts),
            precip_probability=self._value(self.precip_probability, precip_ts),
            visibility_mi=None if visibility_ft is None else visibility_ft / FEET_PER_MILE,
            weather_code=None if code is None else int(code),
        )
