import pytest
import responses
from django.core.cache import cache
from rest_framework.test import APIClient

from .fakes import FakeOpenMeteo, FakeTomTom


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def rsps():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def tomtom(rsps):
    return FakeTomTom(rsps)


@pytest.fixture
def open_meteo(rsps):
    return FakeOpenMeteo(rsps)


@pytest.fixture
def api():
    return APIClient()
