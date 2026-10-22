import pytest

from planner.domain.recommend import ScoredSegment, rank, summarize
from planner.domain.risk import Level

L, M, H, S, N = Level.LOW, Level.MODERATE, Level.HIGH, Level.SEVERE, Level.NO_TRAVEL


def make(route_id, levels, duration_s=10_000, seg=10.0):
    segments = [ScoredSegment(mile=i * seg, segment_mi=seg, level=lv) for i, lv in enumerate(levels)]
    return summarize(route_id, duration_s, segments)


class TestSummarize:
    def test_miles_by_risk_and_average(self):
        s = make("A", [L, L, H, S])
        assert s.miles_by_risk["low"] == 20 and s.high_mi == 10 and s.severe_mi == 10
        assert s.avg_risk == pytest.approx((0 + 0 + 2 + 3) / 4)
        assert s.max_risk == "severe"
        assert not s.blocked and s.first_blocked_mile is None

    def test_blocked_and_first_blocked_mile(self):
        s = make("A", [L, N, M, N])
        assert s.blocked and s.first_blocked_mile == 10 and s.no_travel_mi == 20

    def test_average_is_mile_weighted(self):
        segments = [ScoredSegment(0, 90, L), ScoredSegment(90, 10, S)]
        assert summarize("A", 1, segments).avg_risk == pytest.approx(0.3)

    def test_average_uses_continuous_scores_when_present(self):
        # Two all-Low routes still differ: 0.1 vs 0.8 on the 0-4 scale.
        calm = summarize("A", 1, [ScoredSegment(0, 10, L, score=0.1), ScoredSegment(10, 10, L, score=0.1)])
        breezy = summarize("B", 1, [ScoredSegment(0, 10, L, score=0.8), ScoredSegment(10, 10, L, score=0.8)])
        assert calm.avg_risk == pytest.approx(0.1) and breezy.avg_risk == pytest.approx(0.8)
        assert rank([breezy, calm]).route_id == "A"

    def test_unknown_excluded_from_average(self):
        s = make("A", [L, None, H])
        assert s.avg_risk == pytest.approx(1.0)
        assert s.miles_by_risk["unknown"] == 10 and s.unknown_count == 1

    def test_all_unknown(self):
        s = make("A", [None, None])
        assert s.avg_risk is None and s.max_risk == "unknown"
        assert s.to_dict()["avg_risk"] is None


class TestRank:
    def test_fewest_no_travel_first(self):
        a = make("A", [N, L, L], duration_s=100)
        b = make("B", [S, S, S], duration_s=200)
        rec = rank([a, b])
        assert rec.route_id == "B" and rec.deciding_factor == "no_travel_mi"

    def test_severe_miles_decide(self):
        a = make("A", [S, L, L], duration_s=100)
        b = make("B", [H, H, H], duration_s=200)
        rec = rank([a, b])
        assert rec.route_id == "B" and rec.deciding_factor == "severe_mi"
        assert rec.reason == "Fewest severe-risk miles: 0 mi vs 10 mi on Route A (fastest); adds 2 min"
        assert rec.compared_to == "A"

    def test_high_miles_decide_when_severe_equal(self):
        a = make("A", [H, L, L], duration_s=100)
        b = make("B", [M, M, M], duration_s=200)
        rec = rank([a, b])
        assert rec.route_id == "B" and rec.deciding_factor == "high_mi"

    def test_average_risk_decides(self):
        a = make("A", [M, M, L], duration_s=100)
        b = make("B", [M, L, L], duration_s=200)
        rec = rank([a, b])
        assert rec.route_id == "B" and rec.deciding_factor == "avg_risk"
        assert rec.reason.startswith("Lowest average risk: 0.33 vs 0.67")

    def test_duration_decides_on_equal_risk(self):
        a = make("A", [L, M], duration_s=12_000)
        b = make("B", [M, L], duration_s=9_480)
        rec = rank([a, b])
        assert rec.route_id == "B" and rec.deciding_factor == "duration_s"
        assert rec.reason == "Same weather risk as Route A, and 42 min faster"

    def test_float_noise_does_not_decide(self):
        a = summarize("A", 100, [ScoredSegment(0, 10.000001, S)])
        b = summarize("B", 200, [ScoredSegment(0, 10.0, S)])
        rec = rank([b, a])
        assert rec.route_id == "A" and rec.deciding_factor == "duration_s"

    def test_full_tie_keeps_input_order(self):
        a = make("A", [L])
        b = make("B", [L])
        rec = rank([a, b])
        assert rec.route_id == "A" and rec.deciding_factor is None
        assert "equivalent" in rec.reason

    def test_single_route(self):
        rec = rank([make("A", [L])])
        assert rec.route_id == "A" and rec.reason == "Only one route is available"

    def test_all_blocked_still_recommends_least_bad(self):
        a = make("A", [N, N, L])
        b = make("B", [N, L, L])
        rec = rank([a, b])
        assert rec.all_blocked and rec.route_id == "B"

    def test_unknown_average_ranks_after_known(self):
        a = make("A", [None], duration_s=100)
        b = make("B", [M], duration_s=200)
        assert rank([a, b]).route_id == "B"

    def test_comparison_and_ranking_order(self):
        rec = rank([make("A", [S]), make("B", [L]), make("C", [H])])
        assert rec.ranking == ["B", "C", "A"]
        assert [c["rank"] for c in rec.comparison] == [1, 2, 3]
        assert rec.comparison[0]["route_id"] == "B"
        assert set(rec.to_dict()) == {
            "route_id",
            "ranking",
            "deciding_factor",
            "compared_to",
            "reason",
            "all_blocked",
            "comparison",
        }

    def test_slower_winner_is_explained_against_the_fastest_route(self):
        fastest = make("A", [N, L, L], duration_s=10_000)
        safe = make("B", [L, L, L], duration_s=12_700)
        also_safe = make("C", [L, L, L], duration_s=13_000)
        rec = rank([fastest, safe, also_safe])
        assert rec.route_id == "B"
        assert rec.compared_to == "A" and rec.deciding_factor == "no_travel_mi"
        assert rec.reason == "Fewest No-Travel miles: 0 mi vs 10 mi on Route A (fastest); adds 45 min"

    def test_fastest_winner_is_explained_against_runner_up(self):
        rec = rank([make("A", [L], duration_s=100), make("B", [H], duration_s=200)])
        assert rec.compared_to == "B" and rec.deciding_factor == "high_mi"
        assert "(fastest)" not in rec.reason

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            rank([])

    def test_long_duration_formatting(self):
        rec = rank([make("A", [L], duration_s=10_000), make("B", [L], duration_s=13_900)])
        assert rec.reason == "Same weather risk as Route B, and 1 h 05 min faster"
