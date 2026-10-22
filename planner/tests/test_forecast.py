import pytest

from planner.domain.forecast import HOUR, HourlySeries, hour_ending_at_or_after, nearest_hour

T0 = 1_792_540_800  # on the hour


def series(hours=6, **overrides):
    data = {
        "wind_mph": [float(10 + i) for i in range(hours)],
        "gust_mph": [float(20 + i) for i in range(hours)],
        "rain_in": [0.01 * i for i in range(hours)],
        "showers_in": [0.1] * hours,
        "snow_in": [0.0] * hours,
    }
    data.update(overrides)
    return HourlySeries(start=T0, **data)


@pytest.mark.parametrize(
    "ts, expected",
    [(T0, T0), (T0 + 1, T0 + HOUR), (T0 + 1799, T0 + HOUR), (T0 + 3599, T0 + HOUR), (T0 + HOUR, T0 + HOUR)],
)
def test_hour_ending_at_or_after(ts, expected):
    assert hour_ending_at_or_after(ts) == expected


@pytest.mark.parametrize(
    "ts, expected",
    [(T0, T0), (T0 + 1799, T0), (T0 + 1800, T0 + HOUR), (T0 + 3599, T0 + HOUR)],
)
def test_nearest_hour(ts, expected):
    assert nearest_hour(ts) == expected


def test_sample_uses_nearest_hour_for_wind_and_hour_ending_for_precip():
    s = series()
    sample = s.sample(T0 + HOUR + 20 * 60)  # 01:20 -> wind @01:00, precipitation @02:00
    assert sample.wind_mph == 11.0
    assert sample.gust_mph == 22.0
    assert sample.rain_in_hr == pytest.approx(0.02 + 0.1)  # rain + showers
    assert sample.forecast_time == T0 + 2 * HOUR


def test_sample_on_the_hour():
    sample = series().sample(T0 + 2 * HOUR)
    assert sample.wind_mph == 12.0
    assert sample.forecast_time == T0 + 2 * HOUR


def test_sample_outside_range_is_none():
    s = series(hours=3)
    assert s.sample(T0 - HOUR) is None
    assert s.sample(T0 + 5 * HOUR) is None


def test_sample_with_missing_values_is_none():
    s = series(wind_mph=[None] * 6)
    assert s.sample(T0 + HOUR) is None


def test_missing_showers_treated_as_zero_and_missing_gust_allowed():
    s = series(showers_in=[None] * 6, gust_mph=[None] * 6)
    sample = s.sample(T0 + HOUR)
    assert sample.rain_in_hr == pytest.approx(0.01)
    assert sample.gust_mph is None


def test_end():
    assert series(hours=4).end == T0 + 3 * HOUR


def test_off_hour_lookup_rejected():
    with pytest.raises(ValueError):
        series()._value([1.0], T0 + 1)
