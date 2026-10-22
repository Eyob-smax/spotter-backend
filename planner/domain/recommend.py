"""Route risk summaries and the recommendation.

Ranking follows the brief's order, with No Travel miles placed first
(a route you may not legally/safely drive is worse than any Severe mile):

    1. fewest No Travel miles   2. fewest Severe miles   3. fewest High miles
    4. lowest mile-weighted average risk   5. shortest travel time

Average risk uses each checkpoint's continuous score (0.0-4.0, whose integer
part is the level), so routes that are all "Low" still differ by how close
their weather comes to the Moderate thresholds instead of all averaging 0.

Miles are compared at 0.1 mi so floating-point noise never decides.
Average risks within AVG_RISK_TOLERANCE (a quarter of a level) count as
equal, so travel time decides between routes whose weather is effectively
the same - otherwise a calm route could lose hours to a marginally calmer
one. This is how the ranking "balances weather risk and travel time".
"""

from __future__ import annotations

from dataclasses import dataclass

from .risk import UNKNOWN, Level

CRITERIA = ("no_travel_mi", "severe_mi", "high_mi", "avg_risk", "duration_s")
UNKNOWN_AVG_SENTINEL = float(Level.NO_TRAVEL)
AVG_RISK_TOLERANCE = 0.25


@dataclass(frozen=True)
class ScoredSegment:
    mile: float
    segment_mi: float
    level: Level | None  # None = forecast unavailable
    score: float | None = None  # continuous 0-4 score; falls back to the level when absent


@dataclass(frozen=True)
class RouteSummary:
    route_id: str
    duration_s: float
    miles_by_risk: dict[str, float]
    avg_risk: float | None
    max_risk: str
    first_blocked_mile: float | None
    checkpoint_count: int
    unknown_count: int

    @property
    def no_travel_mi(self) -> float:
        return self.miles_by_risk[Level.NO_TRAVEL.id]

    @property
    def severe_mi(self) -> float:
        return self.miles_by_risk[Level.SEVERE.id]

    @property
    def high_mi(self) -> float:
        return self.miles_by_risk[Level.HIGH.id]

    @property
    def blocked(self) -> bool:
        return self.first_blocked_mile is not None

    def miles_key(self) -> tuple[float, float, float]:
        return (round(self.no_travel_mi, 1), round(self.severe_mi, 1), round(self.high_mi, 1))

    @property
    def avg_for_ranking(self) -> float:
        return UNKNOWN_AVG_SENTINEL if self.avg_risk is None else self.avg_risk

    def to_dict(self) -> dict:
        return {
            "miles_by_risk": {k: round(v, 1) for k, v in self.miles_by_risk.items()},
            "no_travel_mi": round(self.no_travel_mi, 1),
            "severe_mi": round(self.severe_mi, 1),
            "high_mi": round(self.high_mi, 1),
            "avg_risk": None if self.avg_risk is None else round(self.avg_risk, 2),
            "max_risk": self.max_risk,
            "blocked": self.blocked,
            "first_blocked_mile": None if self.first_blocked_mile is None else round(self.first_blocked_mile, 1),
            "checkpoint_count": self.checkpoint_count,
            "unknown_count": self.unknown_count,
        }


def summarize(route_id: str, duration_s: float, segments: list[ScoredSegment]) -> RouteSummary:
    miles = {lv.id: 0.0 for lv in Level} | {UNKNOWN: 0.0}
    weighted = 0.0
    known_mi = 0.0
    first_blocked: float | None = None
    max_level: Level | None = None
    unknown_count = 0
    for seg in segments:
        if seg.level is None:
            miles[UNKNOWN] += seg.segment_mi
            unknown_count += 1
            continue
        miles[seg.level.id] += seg.segment_mi
        weighted += (seg.score if seg.score is not None else int(seg.level)) * seg.segment_mi
        known_mi += seg.segment_mi
        max_level = seg.level if max_level is None else max(max_level, seg.level)
        if seg.level is Level.NO_TRAVEL and first_blocked is None:
            first_blocked = seg.mile
    return RouteSummary(
        route_id=route_id,
        duration_s=duration_s,
        miles_by_risk=miles,
        avg_risk=(weighted / known_mi) if known_mi > 0 else None,
        max_risk=max_level.id if max_level is not None else UNKNOWN,
        first_blocked_mile=first_blocked,
        checkpoint_count=len(segments),
        unknown_count=unknown_count,
    )


@dataclass(frozen=True)
class Recommendation:
    route_id: str
    ranking: list[str]
    deciding_factor: str | None
    compared_to: str | None
    reason: str
    all_blocked: bool
    comparison: list[dict]

    def to_dict(self) -> dict:
        return {
            "route_id": self.route_id,
            "ranking": self.ranking,
            "deciding_factor": self.deciding_factor,
            "compared_to": self.compared_to,
            "reason": self.reason,
            "all_blocked": self.all_blocked,
            "comparison": self.comparison,
        }


def _fmt_mi(value: float) -> str:
    value = round(value, 1)
    if value == 0:
        return "0 mi"
    return f"{value:.1f} mi" if value < 10 else f"{value:.0f} mi"


def _fmt_duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min"


def _reason(winner: RouteSummary, runner_up: RouteSummary, factor: str) -> str:
    other = f"Route {runner_up.route_id}"
    if factor == "no_travel_mi":
        return f"Fewest No-Travel miles: {_fmt_mi(winner.no_travel_mi)} vs {_fmt_mi(runner_up.no_travel_mi)} on {other}"
    if factor == "severe_mi":
        return f"Fewest severe-risk miles: {_fmt_mi(winner.severe_mi)} vs {_fmt_mi(runner_up.severe_mi)} on {other}"
    if factor == "high_mi":
        return f"Fewest high-risk miles: {_fmt_mi(winner.high_mi)} vs {_fmt_mi(runner_up.high_mi)} on {other}"
    if factor == "avg_risk":
        w = "n/a" if winner.avg_risk is None else f"{winner.avg_risk:.2f}"
        r = "n/a" if runner_up.avg_risk is None else f"{runner_up.avg_risk:.2f}"
        return f"Lowest average risk: {w} vs {r} on {other}"
    saved = _fmt_duration(runner_up.duration_s - winner.duration_s)
    return f"Same weather risk as {other}, and {saved} faster"


def _pick_best(candidates: list[RouteSummary]) -> RouteSummary:
    """Fewest No-Travel, then Severe, then High miles; among those, every route within the
    average-risk tolerance of the lowest counts as equally safe and the fastest wins
    (input order breaks exact ties)."""
    best_miles = min(c.miles_key() for c in candidates)
    group = [c for c in candidates if c.miles_key() == best_miles]
    lowest = min(c.avg_for_ranking for c in group)
    equally_safe = [c for c in group if c.avg_for_ranking - lowest < AVG_RISK_TOLERANCE]
    return min(equally_safe, key=lambda c: round(c.duration_s))


def _deciding_factor(winner: RouteSummary, other: RouteSummary) -> str | None:
    """The first criterion on which the two routes differ, using the same rules as the ranking."""
    for name, w, o in zip(CRITERIA[:3], winner.miles_key(), other.miles_key()):
        if w != o:
            return name
    if abs(winner.avg_for_ranking - other.avg_for_ranking) >= AVG_RISK_TOLERANCE:
        return "avg_risk"
    if round(winner.duration_s) != round(other.duration_s):
        return "duration_s"
    return None


def rank(summaries: list[RouteSummary]) -> Recommendation:
    """Order routes by the ranking rules and explain the choice.

    The explanation compares the winner with the route a driver would otherwise
    take: the fastest one. If the winner *is* the fastest, it is compared with
    the runner-up instead.
    """
    if not summaries:
        raise ValueError("Cannot rank an empty list of routes.")
    remaining = list(summaries)
    ordered: list[RouteSummary] = []
    while remaining:
        best = _pick_best(remaining)
        ordered.append(best)
        remaining.remove(best)
    winner = ordered[0]
    fastest = min(summaries, key=lambda s: round(s.duration_s))

    deciding: str | None = None
    compared_to: str | None = None
    if len(ordered) == 1:
        reason = "Only one route is available"
    else:
        other = ordered[1] if fastest is winner else fastest
        compared_to = other.route_id
        deciding = _deciding_factor(winner, other)
        if deciding is None:
            reason = f"Routes {winner.route_id} and {other.route_id} are equivalent on risk and time"
        else:
            reason = _reason(winner, other, deciding)
            extra = winner.duration_s - other.duration_s
            if other is fastest and deciding != "duration_s":
                reason += " (fastest)" if extra < 60 else f" (fastest); adds {_fmt_duration(extra)}"

    comparison = [
        {
            "route_id": s.route_id,
            "rank": i + 1,
            "no_travel_mi": round(s.no_travel_mi, 1),
            "severe_mi": round(s.severe_mi, 1),
            "high_mi": round(s.high_mi, 1),
            "avg_risk": None if s.avg_risk is None else round(s.avg_risk, 2),
            "duration_s": round(s.duration_s),
        }
        for i, s in enumerate(ordered)
    ]
    return Recommendation(
        route_id=winner.route_id,
        ranking=[s.route_id for s in ordered],
        deciding_factor=deciding,
        compared_to=compared_to,
        reason=reason,
        all_blocked=all(s.blocked for s in summaries),
        comparison=comparison,
    )
