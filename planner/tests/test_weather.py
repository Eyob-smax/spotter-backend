import time

import pytest
from django.core.cache import cache

from planner.services.http import build_session
from planner.services.weather import (
    BATCH_SIZE,
    COOLDOWN_KEY,
    WeatherService,
    days_needed,
    snap,
)

from .fakes import OPEN_METEO

NOW = time.time()


@pytest.fixture
def service():
    return WeatherService(OPEN_METEO, build_session(retry_statuses=()), cache)


def cells(n, lat0=30.0):
    return [(round(lat0 + (i // 50) * 0.1, 1), round(-100 + (i % 50) * 0.1, 1)) for i in range(n)]


def test_snap():
    assert snap((39.7392, -104.9903)) == (39.7, -105.0)
    assert snap((-0.04, 0.04)) == (0.0, 0.0)
    assert str(snap((-0.04, 0.0))[0]) == "0.0"  # no negative zero in cache keys
    assert snap((39.74, -104.99), 0.5) == (39.5, -105.0)


@pytest.mark.parametrize(
    "hours_ahead, expected",
    [(1, 3), (40, 3), (24 * 5, 7), (24 * 8, 10), (24 * 12, 16), (24 * 40, 16)],
)
def test_days_needed_buckets(hours_ahead, expected):
    midnight = NOW // 86_400 * 86_400
    assert days_needed(midnight + hours_ahead * 3600, midnight) == expected


def test_batches_and_maps_in_order(service, open_meteo):
    wanted = cells(BATCH_SIZE * 2 + 20)
    result = service.forecasts(wanted, until_ts=NOW + 6 * 3600)
    assert sorted(open_meteo.requests) == [20, BATCH_SIZE, BATCH_SIZE]
    assert result.complete and not result.rate_limited
    assert set(result.series) == set(wanted)
    series = result.series[wanted[0]]
    assert series.wind_mph[0] == 8.0 and series.end >= NOW + 6 * 3600


def test_duplicates_fetched_once(service, open_meteo):
    service.forecasts([(39.7, -105.0)] * 5, until_ts=NOW + 3600)
    assert open_meteo.requests == [1]


def test_cache_hit_avoids_http(service, open_meteo):
    wanted = cells(10)
    service.forecasts(wanted, until_ts=NOW + 3600)
    service.forecasts(wanted, until_ts=NOW + 3600)
    assert open_meteo.requests == [10]


def test_longer_cached_bucket_serves_shorter_request(service, open_meteo):
    wanted = cells(3)
    service.forecasts(wanted, until_ts=NOW + 5 * 86_400)  # 7-day bucket
    service.forecasts(wanted, until_ts=NOW + 3600)  # needs 3 days: served from the 7-day entry
    assert open_meteo.requests == [3]


def test_server_error_marks_cells_failed_and_is_not_cached(service, open_meteo):
    open_meteo.status = 500
    result = service.forecasts(cells(4), until_ts=NOW + 3600)
    assert result.failed == 4 and not result.complete and result.all_failed
    assert all(v is None for v in result.series.values())
    open_meteo.status = 200
    assert service.forecasts(cells(4), until_ts=NOW + 3600).complete


def test_rate_limit_sets_cooldown_and_stops_calling(service, open_meteo):
    open_meteo.status = 429
    first = service.forecasts(cells(4), until_ts=NOW + 3600)
    assert first.rate_limited and first.failed == 4
    assert cache.get(COOLDOWN_KEY)
    open_meteo.status = 200
    second = service.forecasts(cells(4, lat0=40.0), until_ts=NOW + 3600)
    assert second.rate_limited and second.failed == 4
    assert open_meteo.requests == [4]  # no call during cooldown


def test_cached_cells_still_served_during_cooldown(service, open_meteo):
    wanted = cells(2)
    service.forecasts(wanted, until_ts=NOW + 3600)
    cache.set(COOLDOWN_KEY, True, 60)
    result = service.forecasts(wanted, until_ts=NOW + 3600)
    assert result.complete


def test_mismatched_response_count(service, rsps):
    import responses

    rsps.add(responses.GET, OPEN_METEO, json=[{"hourly": {}}])
    result = service.forecasts(cells(3), until_ts=NOW + 3600)
    assert result.failed == 3


def test_unparseable_item(service, rsps):
    import responses

    rsps.add(responses.GET, OPEN_METEO, json={"hourly": {"time": [0, 7200]}})
    result = service.forecasts(cells(1), until_ts=NOW + 3600)
    assert result.failed == 1


def test_api_key_is_sent_when_configured(open_meteo, rsps):
    svc = WeatherService(OPEN_METEO, build_session(retry_statuses=()), cache, api_key="secret")
    svc.forecasts(cells(1), until_ts=NOW + 3600)
    assert "apikey=secret" in rsps.calls[-1].request.url


def test_empty_request(service, open_meteo):
    result = service.forecasts([], until_ts=NOW)
    assert result.series == {} and result.complete and not result.all_failed
    assert open_meteo.requests == []
