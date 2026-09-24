import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from plume_core.http import make_client
from plume_core.models import DemandSeries
from plume_sources.demand import BudgetExhausted, DemandPoint, date_windows
from plume_sources.trends import GoogleTrendsUnofficial
from plume_sources.wikipedia import WikipediaPageviews, user_agent
from tenacity import wait_none

FIXTURES = Path(__file__).parents[2] / "fixtures"
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
SEPT_1, SEPT_10 = date(2026, 9, 1), date(2026, 9, 10)
SERIES = DemandSeries(term="Tour Eiffel")

Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def recording(handler: Handler) -> tuple[httpx.Client, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return make_client(transport=httpx.MockTransport(record), wait=wait_none()), calls


def wikipedia_ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=fixture("wikipedia/pageviews_Tour_Eiffel.json"))


def wikipedia(
    handler: Handler, tmp_path: Path, budget: int = 500, clock: Callable[[], datetime] = lambda: NOW
) -> tuple[WikipediaPageviews, list[httpx.Request]]:
    http, calls = recording(handler)
    return WikipediaPageviews(http, "contact@example.test", budget, tmp_path, clock), calls


def trends_handler(multiline: str | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/explore"):
            return httpx.Response(200, text=fixture("trends/explore.txt"))
        if path.endswith("/multiline"):
            return httpx.Response(200, text=multiline or fixture("trends/multiline.txt"))
        return httpx.Response(200, text="<html></html>")

    return handler


def test_date_windows_are_contiguous_and_bounded() -> None:
    assert date_windows(date(2026, 1, 1), date(2026, 1, 10), 4) == [
        (date(2026, 1, 1), date(2026, 1, 4)),
        (date(2026, 1, 5), date(2026, 1, 8)),
        (date(2026, 1, 9), date(2026, 1, 10)),
    ]
    assert date_windows(SEPT_1, SEPT_1, 90) == [(SEPT_1, SEPT_1)]
    assert date_windows(SEPT_10, SEPT_1, 90) == []


def test_wikipedia_parses_daily_points(tmp_path: Path) -> None:
    source, calls = wikipedia(wikipedia_ok, tmp_path)
    points = source.fetch(SERIES, SEPT_1, SEPT_10)
    assert len(points) == 10
    assert points[0] == DemandPoint(datetime(2026, 9, 1, tzinfo=UTC), 814.0)
    assert calls[0].url.path == (
        "/api/rest_v1/metrics/pageviews/per-article/fr.wikipedia/all-access/user/"
        "Tour_Eiffel/daily/2026090100/2026091000"
    )


def test_wikipedia_paginates_dates(tmp_path: Path) -> None:
    source, calls = wikipedia(wikipedia_ok, tmp_path)
    source.fetch(SERIES, date(2025, 1, 1), date(2026, 2, 4))  # 400 jours
    assert [c.url.path.rsplit("/", 2)[1:] for c in calls] == [
        ["2025010100", "2026010100"],
        ["2026010200", "2026020400"],
    ]


def test_user_agent_on_every_wikipedia_request(tmp_path: Path) -> None:
    source, calls = wikipedia(wikipedia_ok, tmp_path)
    source.fetch(SERIES, date(2024, 1, 1), SEPT_10)
    source.fetch(DemandSeries(term="Mont-Saint-Michel"), SEPT_1, SEPT_10)
    assert len(calls) == 4
    expected = user_agent("contact@example.test")
    assert expected.startswith("Plume/0.") and expected.endswith(" (contact@example.test)")
    assert {c.headers["User-Agent"] for c in calls} == {expected}


def test_wikipedia_requires_a_contact(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        WikipediaPageviews(make_client(), None, 500, tmp_path)


def test_wikipedia_cache_lasts_one_day(tmp_path: Path) -> None:
    now = [NOW]
    source, calls = wikipedia(wikipedia_ok, tmp_path, clock=lambda: now[0])
    first = source.fetch(SERIES, SEPT_1, SEPT_10)
    assert source.fetch(SERIES, SEPT_1, SEPT_10) == first
    assert (len(calls), source.requests_made) == (1, 1)
    now[0] += timedelta(days=1)
    source.fetch(SERIES, SEPT_1, SEPT_10)
    assert len(calls) == 2


def test_wikipedia_backs_off_on_429(tmp_path: Path) -> None:
    responses = [httpx.Response(429), wikipedia_ok(httpx.Request("GET", "/"))]
    source, calls = wikipedia(lambda _: responses.pop(0), tmp_path)
    assert len(source.fetch(SERIES, SEPT_1, SEPT_10)) == 10
    assert len(calls) == 2


def test_wikipedia_unknown_article_is_empty(tmp_path: Path) -> None:
    source, _ = wikipedia(
        lambda _: httpx.Response(404, text=fixture("wikipedia/not_found.json")), tmp_path
    )
    assert source.fetch(SERIES, SEPT_1, SEPT_10) == []
    assert not list(tmp_path.rglob("*.json"))  # une 404 n'est pas mise en cache


def test_wikipedia_budget_refuses_before_sending(tmp_path: Path) -> None:
    source, calls = wikipedia(wikipedia_ok, tmp_path, budget=1)
    with pytest.raises(BudgetExhausted):
        source.fetch(SERIES, date(2025, 1, 1), date(2026, 2, 4))
    assert len(calls) == 1


def test_trends_parses_relative_index() -> None:
    http, calls = recording(trends_handler())
    points = GoogleTrendsUnofficial(http, 50).fetch(SERIES, SEPT_1, SEPT_10)
    assert len(points) == 10
    assert points[0] == DemandPoint(datetime(2026, 9, 1, tzinfo=UTC), 23.0)
    assert all(0 <= p.value <= 100 for p in points)
    explore = json.loads(calls[1].url.params["req"])
    assert explore["comparisonItem"] == [
        {"keyword": "Tour Eiffel", "geo": "FR", "time": "2026-09-01 2026-09-10"}
    ]
    assert calls[2].url.params["token"]


def test_trends_skips_points_without_data() -> None:
    data = json.loads(fixture("trends/multiline.txt").split("\n", 1)[1])
    data["default"]["timelineData"][0]["hasData"] = [False]
    http, _ = recording(trends_handler(")]}',\n" + json.dumps(data)))
    points = GoogleTrendsUnofficial(http, 50).fetch(SERIES, SEPT_1, SEPT_10)
    assert [p.period_start.day for p in points] == list(range(2, 11))


def test_trends_paginates_dates_and_gets_cookie_once() -> None:
    http, calls = recording(trends_handler())
    source = GoogleTrendsUnofficial(http, 50)
    source.fetch(SERIES, date(2026, 1, 1), date(2026, 7, 19))  # 200 jours
    times = [
        json.loads(c.url.params["req"])["comparisonItem"][0]["time"]
        for c in calls
        if c.url.path.endswith("/explore")
    ]
    assert times == ["2026-01-01 2026-03-31", "2026-04-01 2026-06-29", "2026-06-30 2026-07-19"]
    assert [c.url.path for c in calls].count("/trends/") == 1
    assert source.requests_made == len(calls) == 7
