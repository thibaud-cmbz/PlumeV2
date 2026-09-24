from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from plume_core.config import Settings
from plume_core.models import YOUTUBE_EXPIRY_COLUMNS

ALEMBIC = Config(toml_file=Path(__file__).parents[2] / "pyproject.toml")
LATER = datetime.now(UTC) + timedelta(days=365)


@pytest.fixture(scope="module", autouse=True)
def _migrated() -> None:
    command.upgrade(ALEMBIC, "head")


@pytest.fixture
def db() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    # Tout est annulé en fin de test : la base reste vide.
    with psycopg.connect(str(Settings().database_url)) as connection:
        yield connection
        connection.rollback()


def _tables(db: psycopg.Connection[tuple[object, ...]]) -> set[object]:
    rows = db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    return {row[0] for row in rows}


def test_upgrade_downgrade_upgrade() -> None:
    command.upgrade(ALEMBIC, "head")
    command.downgrade(ALEMBIC, "base")
    with psycopg.connect(str(Settings().database_url)) as connection:
        assert _tables(connection) == {"alembic_version"}
    command.upgrade(ALEMBIC, "head")
    with psycopg.connect(str(Settings().database_url)) as connection:
        assert {"video_observation", "recommendation"} <= _tables(connection)


@pytest.mark.parametrize(("table", "column"), YOUTUBE_EXPIRY_COLUMNS.items())
def test_youtube_table_has_non_null_expiry(
    db: psycopg.Connection[tuple[object, ...]], table: str, column: str
) -> None:
    row = db.execute(
        "SELECT is_nullable, data_type FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    assert row == ("NO", "timestamp with time zone")


def _insert_video(db: psycopg.Connection[tuple[object, ...]]) -> object:
    channel = db.execute(
        "INSERT INTO tracked_channel (id, platform, external_id, first_seen_at, active, expires_at)"
        " VALUES (gen_random_uuid(), 'youtube', 'UC_test', now(), true, %s) RETURNING id",
        (LATER,),
    ).fetchone()
    assert channel is not None
    video = db.execute(
        "INSERT INTO video (id, platform, external_id, channel_id, published_at, first_seen_at,"
        " text_expires_at) VALUES (gen_random_uuid(), 'youtube', 'v_test', %s, now(), now(), %s)"
        " RETURNING id",
        (channel[0], LATER),
    ).fetchone()
    assert video is not None
    return video[0]


def test_video_observation_is_partitioned_by_month(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    partitioned = db.execute(
        "SELECT 1 FROM pg_partitioned_table WHERE partrelid = 'video_observation'::regclass"
    ).fetchone()
    assert partitioned is not None

    video_id = _insert_video(db)
    now = datetime.now(UTC)
    next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=2)
    for observed_at in (now, next_month):
        db.execute(
            "INSERT INTO video_observation (video_id, observed_at, views, expires_at)"
            " VALUES (%s, %s, 10, %s)",
            (video_id, observed_at, LATER),
        )
    partitions = db.execute(
        "SELECT tableoid::regclass::text FROM video_observation WHERE video_id = %s"
        " ORDER BY observed_at",
        (video_id,),
    ).fetchall()
    assert partitions == [
        (f"video_observation_p{now:%Y_%m}",),
        (f"video_observation_p{next_month:%Y_%m}",),
    ]

    # La fonction crée la partition du mois suivant, sans erreur si elle existe déjà.
    created = db.execute("SELECT create_video_observation_partition()").fetchone()
    assert created == (f"video_observation_p{next_month:%Y_%m}",)


def test_video_observation_is_append_only(db: psycopg.Connection[tuple[object, ...]]) -> None:
    video_id = _insert_video(db)
    db.execute(
        "INSERT INTO video_observation (video_id, observed_at, views, expires_at)"
        " VALUES (%s, now(), 10, %s)",
        (video_id, LATER),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("UPDATE video_observation SET views = 11 WHERE video_id = %s", (video_id,))


def test_recommendation_is_immutable_but_survives_topic_purge(
    db: psycopg.Connection[tuple[object, ...]],
) -> None:
    ids = db.execute(
        """
        WITH u AS (
            INSERT INTO "user" (id, email) VALUES (gen_random_uuid(), 'a@example.org') RETURNING id
        ), p AS (
            INSERT INTO channel_profile (id, user_id, profile_key, version, is_current, name,
                niche_label, language, region, editorial_rules, target_platforms,
                target_duration_s)
            SELECT gen_random_uuid(), u.id, gen_random_uuid(), 1, true, 'p', 'niche', 'fr', 'FR',
                '[]', '["youtube"]', '{"youtube": 60}' FROM u RETURNING id, user_id
        ), t AS (
            INSERT INTO topic (id, label, clustering_version, expires_at)
            VALUES (gen_random_uuid(), 'sujet', 'v0', %s) RETURNING id
        )
        INSERT INTO recommendation (id, user_id, profile_id, profile_version, topic_id, method,
            model_version, as_of, rank, score, reasons)
        SELECT gen_random_uuid(), p.user_id, p.id, 1, t.id, 'plume', 'v0', now(), 1, 0.5, '[]'
        FROM p, t RETURNING id, topic_id
        """,
        (LATER,),
    ).fetchone()
    assert ids is not None
    recommendation_id, topic_id = ids

    db.execute("DELETE FROM topic WHERE id = %s", (topic_id,))
    row = db.execute(
        "SELECT topic_id, score FROM recommendation WHERE id = %s", (recommendation_id,)
    ).fetchone()
    assert row == (None, 0.5)

    with pytest.raises(psycopg.errors.RaiseException, match="immuable"):
        db.execute("UPDATE recommendation SET score = 1 WHERE id = %s", (recommendation_id,))
