import pytest

from planner.domain.wmo import describe


@pytest.mark.parametrize(
    "code, text",
    [(0, "Clear"), (3, "Overcast"), (45, "Fog"), (63, "Rain"), (73, "Snow"), (95, "Thunderstorm"), (42, "Code 42")],
)
def test_describe(code, text):
    assert describe(code) == text


def test_describe_none():
    assert describe(None) is None
