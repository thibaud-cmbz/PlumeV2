import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from plume_core.asof import AsOf
from plume_core.config import Settings
from plume_core.http import make_client
from plume_core.models import (
    ChannelProfile,
    CollectionRun,
    DemandObservation,
    DemandSeries,
    ProfileSeedTerm,
    User,
)
from plume_sources.demand import DemandSource, collect_demand
from plume_sources.trends import GoogleTrendsUnofficial
from plume_sources.wikipedia import WikipediaPageviews, profile_articles
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from tenacity import wait_none

FIXTURES = Path(__file__).parents[2] / "fixtures"
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
START, END = date(2026, 9, 1), date(2026, 9, 10)
ARTICLES = ["Tour Eiffel", "Mont-Saint-Michel"]
TERMS = ["terme un", "terme deux"]


@pytest.fixture(scope="module", autouse=True)
def _migrated() -> None:
    command.upgrade(Config(toml_file=Path(__file__).parents[2] / "pyproject.toml"), "head")


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(Settings().sqlalchemy_url)
    with engine.connect() as connection, Session(bind=connection) as session:
        yield session
        session.rollback()
    engine.dispose()


def wikipedia_handler(bump: float = 0) -> httpx.MockTransport:
    """Sert la fixture de l'article demandé ; `bump` révise la valeur du 1er septembre."""

    def handler(request: httpx.Request) -> httpx.Response:
        article = request.url.path.split("/")[-4]
        data = json.loads((FIXTURES / "wikipedia" / f"pageviews_{article}.json").read_text())
        data["items"][0]["views"] += bump
        return httpx.Response(200, json=data)

    return httpx.MockTransport(handler)


def trends_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/explore"):
        return httpx.Response(200, text=(FIXTURES / "trends" / "explore.txt").read_text())
    if request.url.path.endswith("/multiline"):
        return httpx.Response(200, text=(FIXTURES / "trends" / "multiline.txt").read_text())
    return httpx.Response(200)


def wikipedia(
    tmp_path: Path, transport: httpx.MockTransport, now: datetime = NOW
) -> WikipediaPageviews:
    http = make_client(transport=transport, wait=wait_none())
    return WikipediaPageviews(http, "contact@example.test", 500, tmp_path, lambda: now)


def trends(transport: httpx.MockTransport) -> GoogleTrendsUnofficial:
    return GoogleTrendsUnofficial(make_client(transport=transport, wait=wait_none()), 50)


def run(
    session: Session, jobs: list[tuple[DemandSource, list[str]]], now: datetime = NOW
) -> dict[str, CollectionRun]:
    return {r.source: r for r in collect_demand(session, jobs, START, END, now)}


def observations(session: Session, source: str) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(DemandObservation)
            .join(DemandSeries)
            .filter_by(source=source)
        )
        or 0
    )


def test_daily_series_are_written_for_terms_and_articles(session: Session, tmp_path: Path) -> None:
    jobs: list[tuple[DemandSource, list[str]]] = [
        (wikipedia(tmp_path, wikipedia_handler()), ARTICLES),
        (trends(httpx.MockTransport(trends_handler)), TERMS),
    ]
    runs = run(session, jobs)
    assert {name: r.status for name, r in runs.items()} == {
        "wikipedia": "ok",
        "google_trends": "ok",
    }
    assert (runs["wikipedia"].quota_used, runs["google_trends"].quota_used) == (2, 5)
    series = session.execute(select(DemandSeries.source, DemandSeries.term)).tuples().all()
    assert sorted(series) == sorted(
        [("wikipedia", a) for a in ARTICLES] + [("google_trends", t) for t in TERMS]
    )
    assert {s.granularity for s in session.scalars(select(DemandSeries))} == {"daily"}
    assert (observations(session, "wikipedia"), observations(session, "google_trends")) == (20, 20)

    # Même valeurs le lendemain : ni nouvelle série ni nouvelle observation.
    run(session, jobs, NOW + timedelta(days=1))
    assert session.scalar(select(func.count()).select_from(DemandSeries)) == 4
    assert observations(session, "wikipedia") == 20


def test_revision_adds_an_observation_without_overwriting(session: Session, tmp_path: Path) -> None:
    run(session, [(wikipedia(tmp_path, wikipedia_handler()), ARTICLES[:1])])
    later = NOW + timedelta(days=1)
    # Le lendemain : le cache du jour précédent ne sert plus.
    revised = wikipedia(tmp_path, wikipedia_handler(bump=5), later)
    run(session, [(revised, ARTICLES[:1])], later)

    sept_1 = datetime(2026, 9, 1, tzinfo=UTC)
    history = session.execute(
        select(DemandObservation.observed_at, DemandObservation.value)
        .where(DemandObservation.period_start == sept_1)
        .order_by(DemandObservation.observed_at)
    ).tuples()
    assert list(history) == [(NOW, 814.0), (later, 819.0)]
    assert observations(session, "wikipedia") == 11

    def value_at(t: datetime) -> float:
        rows = session.scalars(AsOf(t).demand_series_values()).all()
        return next(r.value for r in rows if r.period_start == sept_1)

    assert (value_at(NOW), value_at(later)) == (814.0, 819.0)


def test_trends_failure_leaves_wikipedia_complete(session: Session, tmp_path: Path) -> None:
    def broken(_: httpx.Request) -> httpx.Response:
        raise RuntimeError("Google a changé son format")

    runs = run(
        session,
        [
            (trends(httpx.MockTransport(broken)), TERMS),
            (wikipedia(tmp_path, wikipedia_handler()), ARTICLES),
        ],
    )
    assert runs["google_trends"].status == "failed"
    assert runs["google_trends"].error == "RuntimeError: Google a changé son format"
    assert runs["wikipedia"].status == "ok"
    assert observations(session, "wikipedia") == 20
    assert observations(session, "google_trends") == 0
    statuses = session.execute(select(CollectionRun.source, CollectionRun.status)).tuples()
    assert sorted(statuses) == [("google_trends", "failed"), ("wikipedia", "ok")]


def test_profile_provides_wikipedia_titles(session: Session) -> None:
    user = User(email=f"{uuid4()}@example.test")
    session.add(user)
    session.flush()
    profile = ChannelProfile(
        user_id=user.id,
        profile_key=uuid4(),
        version=1,
        is_current=True,
        name="p",
        niche_label="niche",
        language="fr",
        region="FR",
        editorial_rules=[],
        target_platforms=["youtube"],
        target_duration_s={"youtube": 60},
    )
    session.add(profile)
    session.flush()
    session.add_all(
        [
            ProfileSeedTerm(profile_id=profile.id, term="a", weight=1, wikipedia_title=ARTICLES[0]),
            ProfileSeedTerm(profile_id=profile.id, term="b", weight=1),
        ]
    )
    session.flush()
    assert profile_articles(session, profile.id) == ARTICLES[:1]
