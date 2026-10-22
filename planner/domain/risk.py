"""Weather risk classification for a single checkpoint.

Implements the assessment's risk table and load rules. Boundary convention
(documented in the README): every band is lower-bound inclusive, so where the
table repeats a value (e.g. rain 0.25 in both Moderate and High) the higher,
safer band wins. The No Travel cut is inclusive for wind (">= 55 mph") and
exclusive for rain and snow ("> 1.00", "> 3.0"), exactly as written in the brief.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Level(IntEnum):
    LOW = 0
    MODERATE = 1
    HIGH = 2
    SEVERE = 3
    NO_TRAVEL = 4

    @property
    def id(self) -> str:
        return self.name.lower()

    @property
    def label(self) -> str:
        return "No Travel" if self is Level.NO_TRAVEL else self.name.title()


UNKNOWN = "unknown"


@dataclass(frozen=True)
class Thresholds:
    metric: str
    unit: str
    # Lower bounds of Moderate, High, Severe and No Travel.
    cuts: tuple[float, float, float, float]
    no_travel_inclusive: bool
    # Band descriptions as written in the brief, Low..No Travel.
    bands: tuple[str, str, str, str, str]

    def level(self, value: float) -> Level:
        level = Level.LOW
        for i, cut in enumerate(self.cuts[:3], start=1):
            if value >= cut:
                level = Level(i)
        nt = self.cuts[3]
        if value > nt or (self.no_travel_inclusive and value == nt):
            level = Level.NO_TRAVEL
        return level

    def score(self, value: float) -> float:
        """Continuous score in [0, 4]: level plus position inside the band (capped below the next level)."""
        level = self.level(value)
        if level is Level.NO_TRAVEL:
            return 4.0
        lo = 0.0 if level is Level.LOW else self.cuts[level - 1]
        hi = self.cuts[level]
        frac = (value - lo) / (hi - lo) if hi > lo else 0.0
        return level + min(max(frac, 0.0), 0.99)


WIND = Thresholds(
    metric="wind",
    unit="mph",
    cuts=(25, 35, 45, 55),
    no_travel_inclusive=True,
    bands=("< 25 mph", "25–34 mph", "35–44 mph", "45–54 mph", "≥ 55 mph"),
)
RAIN = Thresholds(
    metric="rain",
    unit="in/hr",
    cuts=(0.10, 0.25, 0.50, 1.00),
    no_travel_inclusive=False,
    bands=("< 0.10 in/hr", "0.10–0.25 in/hr", "0.25–0.50 in/hr", "0.50–1.00 in/hr", "> 1.00 in/hr"),
)
SNOW = Thresholds(
    metric="snow",
    unit="in/hr",
    cuts=(0.5, 1.0, 2.0, 3.0),
    no_travel_inclusive=False,
    bands=("< 0.5 in/hr", "0.5–1.0 in/hr", "1.0–2.0 in/hr", "2.0–3.0 in/hr", "> 3.0 in/hr"),
)
METRICS = (WIND, RAIN, SNOW)


@dataclass(frozen=True)
class LoadRule:
    """Applies when wind_min <= wind < wind_max (no upper bound if None) and load > load_over_lb."""

    wind_min_mph: float
    wind_max_mph: float | None
    load_over_lb: float
    result: Level

    def applies(self, wind_mph: float, load_lb: float) -> bool:
        in_band = wind_mph >= self.wind_min_mph and (self.wind_max_mph is None or wind_mph < self.wind_max_mph)
        return in_band and load_lb > self.load_over_lb


LOAD_RULES = (
    LoadRule(wind_min_mph=55, wind_max_mph=None, load_over_lb=0, result=Level.NO_TRAVEL),
    LoadRule(wind_min_mph=45, wind_max_mph=55, load_over_lb=30_000, result=Level.NO_TRAVEL),
    LoadRule(wind_min_mph=35, wind_max_mph=45, load_over_lb=40_000, result=Level.SEVERE),
)


@dataclass(frozen=True)
class Factor:
    type: str  # "wind" | "rain" | "snow" | "load"
    level: Level
    value: float
    message: str
    decisive: bool = False

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "level": self.level.id,
            "value": self.value,
            "message": self.message,
            "decisive": self.decisive,
        }


@dataclass(frozen=True)
class Assessment:
    level: Level
    score: float
    factors: list[Factor] = field(default_factory=list)


def _fmt(value: float, unit: str) -> str:
    if unit == "mph":
        return f"{value:.0f} mph" if float(value).is_integer() else f"{value:.1f} mph"
    return f"{value:.2f} {unit}"


def classify(wind_mph: float, rain_in_hr: float, snow_in_hr: float, load_lb: float) -> Assessment:
    """Risk level for one checkpoint: the worst of wind, rain, snow and any applicable load rule."""
    if load_lb < 0:
        raise ValueError("load_lb must be non-negative.")
    values = {"wind": max(wind_mph, 0.0), "rain": max(rain_in_hr, 0.0), "snow": max(snow_in_hr, 0.0)}

    factors: list[Factor] = []
    score = 0.0
    for t in METRICS:
        value = values[t.metric]
        level = t.level(value)
        score = max(score, t.score(value))
        message = f"{t.metric.title()} {_fmt(value, t.unit)} is in the {level.label} band ({t.bands[level]})"
        factors.append(Factor(type=t.metric, level=level, value=value, message=message))

    wind = values["wind"]
    for rule in LOAD_RULES[1:]:  # the >= 55 mph rule is already the wind table's No Travel band
        if rule.applies(wind, load_lb):
            message = (
                f"Wind {_fmt(wind, 'mph')} with a {load_lb:,.0f} lb load (> {rule.load_over_lb:,.0f} lb) "
                f"→ {rule.result.label}"
            )
            factors.append(Factor(type="load", level=rule.result, value=load_lb, message=message))
            score = max(score, float(rule.result))

    level = max(f.level for f in factors)
    if level is Level.NO_TRAVEL:
        score = 4.0
    score = round(score, 2)
    factors = [
        Factor(f.type, f.level, f.value, f.message, decisive=(f.level == level and level > Level.LOW)) for f in factors
    ]
    return Assessment(level=level, score=score, factors=factors)


def describe_rules() -> dict:
    """Machine-readable rule tables for the frontend (single source of truth)."""
    return {
        "risk_levels": [{"id": lv.id, "label": lv.label, "score": int(lv)} for lv in Level],
        "thresholds": {
            f"{t.metric}_{t.unit.replace('/', '_')}": {
                "moderate": t.cuts[0],
                "high": t.cuts[1],
                "severe": t.cuts[2],
                "no_travel": t.cuts[3],
                "no_travel_inclusive": t.no_travel_inclusive,
                "bands": dict(zip([lv.id for lv in Level], t.bands)),
            }
            for t in METRICS
        },
        "load_rules": [
            {
                "wind_min_mph": r.wind_min_mph,
                "wind_max_mph": r.wind_max_mph,
                "load_over_lb": r.load_over_lb,
                "result": r.result.id,
            }
            for r in LOAD_RULES
        ],
    }
