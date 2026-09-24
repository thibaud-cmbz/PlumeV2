from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from plume_core.asof import AsOf
from plume_core.config import Settings
from plume_core.models import (
    ChannelProfile,
    DemandObservation,
    DemandSeries,
    Topic,
    TopicVideo,
    TrackedChannel,
    User,
    Video,
    VideoObservation,
)
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

T = datetime(2026, 1, 15, 12, tzinfo=UTC)
BEFORE, LONG_BEFORE, AFTER = T - timedelta(days=1), T - timedelta(days=5), T + timedelta(days=1)
LATER = datetime(2100, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def _migrated() -> None:
    command.upgrade(Config(toml_file=Path(__file__).parents[2] / "pyproject.toml"), "head")


@pytest.fixture
def session() -> Iterator[Session]:
    # Tout est annulé en fin de test : la base reste vide.
    engine = create_engine(Settings().sqlalchemy_url)
    with engine.connect() as connection, Session(bind=connection) as session:
        yield session
        session.rollback()
    engine.dispose()


@dataclass
class Dataset:
    known: Video  # découverte avant T
    late: Video  # publiée avant T, découverte après T
    series: DemandSeries
    profile_key: UUID


@pytest.fixture
def dataset(session: Session) -> Dataset:
    session.execute(text("SELECT create_video_observation_partition('2026-01-01')"))
    user = User(email="asof@example.org")
    channel = TrackedChannel(
        platform="youtube",
        external_id="UC_asof",
        first_seen_at=LONG_BEFORE,
        active=True,
        expires_at=LATER,
    )
    session.add_all([user, channel])
    session.flush()

    def video(external_id: str, first_seen_at: datetime) -> Video:
        return Video(
            platform="youtube",
            external_id=external_id,
            channel_id=channel.id,
            published_at=LONG_BEFORE,
            first_seen_at=first_seen_at,
            text_expires_at=LATER,
        )

    known, late = video("v_known", LONG_BEFORE), video("v_late", AFTER)
    series = DemandSeries(source="google_trends", term="terme", region="FR", granularity="day")
    topic = Topic(label="sujet", clustering_version="v1", created_at=LONG_BEFORE, expires_at=LATER)
    session.add_all([known, late, series, topic])
    session.flush()

    profile_key = uuid4()
    session.add_all(
        [
            *(
                VideoObservation(video_id=known.id, observed_at=at, views=views, expires_at=LATER)
                for at, views in ((LONG_BEFORE, 10), (BEFORE, 20), (AFTER, 30))
            ),
            VideoObservation(video_id=late.id, observed_at=AFTER, views=99, expires_at=LATER),
            # Même période, révisée avant puis après T.
            *(
                DemandObservation(
                    series_id=series.id, period_start=LONG_BEFORE, observed_at=at, value=value
                )
                for at, value in ((LONG_BEFORE, 40.0), (BEFORE, 50.0), (AFTER, 60.0))
            ),
            DemandObservation(
                series_id=series.id, period_start=AFTER, observed_at=AFTER, value=70.0
            ),
            *(
                ChannelProfile(
                    user_id=user.id,
                    profile_key=profile_key,
                    version=version,
                    is_current=version == 2,
                    name=f"p{version}",
                    niche_label="niche",
                    language="fr",
                    region="FR",
                    editorial_rules=[],
                    target_platforms=["youtube"],
                    target_duration_s={"youtube": 60},
                    created_at=created_at,
                )
                for version, created_at in ((1, LONG_BEFORE), (2, AFTER))
            ),
            TopicVideo(
                topic_id=topic.id,
                video_id=known.id,
                similarity=0.9,
                assigned_at=BEFORE,
                expires_at=LATER,
            ),
            TopicVideo(
                topic_id=topic.id,
                video_id=late.id,
                similarity=0.8,
                assigned_at=AFTER,
                expires_at=LATER,
            ),
        ]
    )
    session.flush()
    return Dataset(known=known, late=late, series=series, profile_key=profile_key)


def test_latest_video_observations(session: Session, dataset: Dataset) -> None:
    rows = session.scalars(AsOf(T).latest_video_observations()).all()
    assert [(r.video_id, r.observed_at, r.views) for r in rows] == [(dataset.known.id, BEFORE, 20)]


def test_known_videos_ignores_published_at(session: Session, dataset: Dataset) -> None:
    videos = session.scalars(AsOf(T).known_videos()).all()
    assert [v.id for v in videos] == [dataset.known.id]


def test_demand_revision_after_t_does_not_replace_value(session: Session, dataset: Dataset) -> None:
    rows = session.scalars(AsOf(T).demand_series_values()).all()
    assert [(r.period_start, r.observed_at, r.value) for r in rows] == [(LONG_BEFORE, BEFORE, 50.0)]
    # Après T, la révision est bien visible : c'est T qui la masque.
    later = session.scalars(AsOf(AFTER).demand_series_values()).all()
    assert [r.value for r in later] == [60.0, 70.0]


def test_active_profile(session: Session, dataset: Dataset) -> None:
    profile = session.scalars(AsOf(T).active_profile(dataset.profile_key)).one()
    assert profile.version == 1
    assert session.scalars(AsOf(AFTER).active_profile(dataset.profile_key)).one().version == 2
    assert (
        session.scalars(
            AsOf(LONG_BEFORE - timedelta(days=1)).active_profile(dataset.profile_key)
        ).first()
        is None
    )


def test_topic_assignments(session: Session, dataset: Dataset) -> None:
    rows = session.scalars(AsOf(T).topic_assignments("v1")).all()
    assert [(r.video_id, r.assigned_at) for r in rows] == [(dataset.known.id, BEFORE)]
    assert session.scalars(AsOf(T).topic_assignments("v2")).all() == []
