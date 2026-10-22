import math

import pytest

from planner.domain.risk import LOAD_RULES, RAIN, SNOW, WIND, Level, classify, describe_rules

L, M, H, S, N = Level.LOW, Level.MODERATE, Level.HIGH, Level.SEVERE, Level.NO_TRAVEL
LIGHT = 10_000  # below every load-rule threshold


def wind_level(mph, load=LIGHT):
    return classify(mph, 0.0, 0.0, load).level


@pytest.mark.parametrize(
    "mph, expected",
    [
        (0, L),
        (24.9, L),
        (24.99, L),
        (25, M),
        (30, M),
        (34, M),
        (34.99, M),
        (35, H),
        (44, H),
        (44.99, H),
        (45, S),
        (54, S),
        (54.99, S),
        (55, N),
        (55.01, N),
        (80, N),
    ],
)
def test_wind_bands(mph, expected):
    assert wind_level(mph) is expected


@pytest.mark.parametrize(
    "inches, expected",
    [
        (0, L),
        (0.0999, L),
        (0.10, M),
        (0.2499, M),
        (0.25, H),  # shared boundary in the brief's table -> higher (safer) band
        (0.4999, H),
        (0.50, S),
        (0.75, S),
        (1.00, S),  # "0.50-1.00" is Severe, No Travel is "> 1.00"
        (1.0001, N),
        (2.5, N),
    ],
)
def test_rain_bands(inches, expected):
    assert classify(0, inches, 0, LIGHT).level is expected


@pytest.mark.parametrize(
    "inches, expected",
    [
        (0, L),
        (0.49, L),
        (0.5, M),
        (0.99, M),
        (1.0, H),
        (1.99, H),
        (2.0, S),
        (3.0, S),  # "2.0-3.0" is Severe, No Travel is "> 3.0"
        (3.0001, N),
        (5, N),
    ],
)
def test_snow_bands(inches, expected):
    assert classify(0, 0, inches, LIGHT).level is expected


class TestLoadRules:
    @pytest.mark.parametrize("load", [1_000, 30_000, 40_000, 80_000])
    def test_55_mph_is_no_travel_for_any_load(self, load):
        assert wind_level(55, load) is N

    @pytest.mark.parametrize(
        "mph, load, expected",
        [
            (45, 30_000, S),  # not > 30,000
            (45, 30_001, N),
            (50, 35_000, N),
            (54.99, 30_001, N),
            (44.99, 30_001, H),  # below the 45 mph band; 30,001 lb is also not > 40,000
            (55, 30_001, N),
        ],
    )
    def test_45_to_54_mph_over_30k_is_no_travel(self, mph, load, expected):
        assert wind_level(mph, load) is expected

    @pytest.mark.parametrize(
        "mph, load, expected",
        [
            (35, 40_000, H),  # not > 40,000
            (35, 40_001, S),
            (40, 80_000, S),
            (44.99, 40_001, S),
            (34.99, 40_001, M),  # below the 35 mph band
        ],
    )
    def test_35_to_44_mph_over_40k_is_severe(self, mph, load, expected):
        assert wind_level(mph, load) is expected

    def test_heaviest_rule_wins_for_45_mph_over_40k(self):
        # 45-54 mph + >30,000 lb (No Travel) also covers loads > 40,000 lb.
        assert wind_level(47, 42_000) is N

    def test_load_rule_never_lowers_weather_level(self):
        # 40 mph + 45,000 lb -> Severe from load, but heavy snow already says No Travel.
        assert classify(40, 0, 3.5, 45_000).level is N

    def test_load_rule_reported_as_factor(self):
        result = classify(47, 0, 0, 42_000)
        load_factors = [f for f in result.factors if f.type == "load"]
        assert len(load_factors) == 1
        assert load_factors[0].level is N
        assert "42,000 lb" in load_factors[0].message and "30,000 lb" in load_factors[0].message
        assert load_factors[0].decisive

    def test_no_load_factor_when_rule_does_not_apply(self):
        assert not [f for f in classify(40, 0, 0, 40_000).factors if f.type == "load"]


class TestCombination:
    def test_worst_metric_wins(self):
        assert classify(26, 0.3, 0.6, LIGHT).level is H  # moderate wind, high rain, moderate snow

    def test_all_three_factors_always_reported(self):
        types = [f.type for f in classify(10, 0, 0, LIGHT).factors]
        assert types == ["wind", "rain", "snow"]

    def test_decisive_flags_mark_the_worst_factors(self):
        result = classify(36, 0.3, 0, LIGHT)  # wind High, rain High
        decisive = {f.type for f in result.factors if f.decisive}
        assert decisive == {"wind", "rain"}

    def test_nothing_is_decisive_when_all_low(self):
        assert not any(f.decisive for f in classify(5, 0, 0, LIGHT).factors)

    def test_negative_inputs_are_clamped(self):
        assert classify(-3, -0.1, -1, LIGHT).level is L

    def test_negative_load_rejected(self):
        with pytest.raises(ValueError):
            classify(10, 0, 0, -1)

    def test_messages_quote_the_band(self):
        wind = classify(47, 0, 0, LIGHT).factors[0]
        assert wind.message == "Wind 47 mph is in the Severe band (45–54 mph)"


class TestScore:
    @pytest.mark.parametrize("mph", [0, 10, 24.99, 25, 30, 34.99, 35, 44.99, 45, 54.99, 55, 70])
    @pytest.mark.parametrize("load", [LIGHT, 35_000, 45_000])
    def test_floor_of_score_is_level(self, mph, load):
        result = classify(mph, 0, 0, load)
        assert math.floor(result.score) == int(result.level)

    @pytest.mark.parametrize("rain", [0, 0.05, 0.1, 0.2, 0.25, 0.49, 0.5, 0.99, 1.0, 1.01])
    def test_floor_of_score_is_level_for_rain(self, rain):
        result = classify(0, rain, 0, LIGHT)
        assert math.floor(result.score) == int(result.level)

    def test_score_increases_with_wind(self):
        scores = [classify(m, 0, 0, LIGHT).score for m in range(0, 60, 2)]
        assert scores == sorted(scores)

    def test_no_travel_scores_four(self):
        assert classify(60, 0, 0, LIGHT).score == 4.0
        assert classify(47, 0, 0, 42_000).score == 4.0

    def test_load_rule_sets_severe_floor(self):
        assert classify(36, 0, 0, 45_000).score == 3.0


def test_thresholds_match_the_brief():
    assert WIND.cuts == (25, 35, 45, 55) and WIND.no_travel_inclusive
    assert RAIN.cuts == (0.10, 0.25, 0.50, 1.00) and not RAIN.no_travel_inclusive
    assert SNOW.cuts == (0.5, 1.0, 2.0, 3.0) and not SNOW.no_travel_inclusive
    assert [(r.wind_min_mph, r.wind_max_mph, r.load_over_lb, r.result) for r in LOAD_RULES] == [
        (55, None, 0, N),
        (45, 55, 30_000, N),
        (35, 45, 40_000, S),
    ]


def test_describe_rules_shape():
    rules = describe_rules()
    assert [lv["id"] for lv in rules["risk_levels"]] == ["low", "moderate", "high", "severe", "no_travel"]
    assert set(rules["thresholds"]) == {"wind_mph", "rain_in_hr", "snow_in_hr"}
    assert rules["thresholds"]["rain_in_hr"]["no_travel"] == 1.0
    assert rules["load_rules"][1] == {
        "wind_min_mph": 45,
        "wind_max_mph": 55,
        "load_over_lb": 30_000,
        "result": "no_travel",
    }
