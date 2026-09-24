"""Schéma initial de la phase 1.

Revision ID: 0001
Revises:
Create Date: 2026-09-24 14:28:43.101522

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Partitions mensuelles, bornes en UTC. Sans argument : le mois suivant (à planifier).
# Pas de partition par défaut : ses lignes empêcheraient de créer la partition du mois concerné.
PARTITION_SQL = """
CREATE FUNCTION create_video_observation_partition(
    month date DEFAULT (now() AT TIME ZONE 'UTC' + interval '1 month')::date
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    start_at timestamp := date_trunc('month', month);
    partition_name text := 'video_observation_p' || to_char(start_at, 'YYYY_MM');
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF video_observation FOR VALUES FROM (%L) TO (%L)',
        partition_name,
        start_at AT TIME ZONE 'UTC',
        (start_at + interval '1 month') AT TIME ZONE 'UTC'
    );
    RETURN partition_name;
END $$;

SELECT create_video_observation_partition((now() AT TIME ZONE 'UTC')::date);
SELECT create_video_observation_partition();
"""

APPEND_ONLY_SQL = """
CREATE FUNCTION forbid_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% est append-only : insérer une nouvelle ligne', TG_TABLE_NAME;
END $$;

CREATE TRIGGER video_observation_append_only BEFORE UPDATE ON video_observation
    FOR EACH ROW EXECUTE FUNCTION forbid_update();
CREATE TRIGGER demand_observation_append_only BEFORE UPDATE ON demand_observation
    FOR EACH ROW EXECUTE FUNCTION forbid_update();

-- Seule modification tolérée : topic_id mis à NULL par la purge du topic (ON DELETE SET NULL).
CREATE FUNCTION forbid_recommendation_update() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    purged recommendation := OLD;
BEGIN
    purged.topic_id := NULL;
    IF NEW IS DISTINCT FROM purged THEN
        RAISE EXCEPTION 'recommendation est immuable après création';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER recommendation_immutable BEFORE UPDATE ON recommendation
    FOR EACH ROW EXECUTE FUNCTION forbid_recommendation_update();
"""

# Les triggers disparaissent avec leurs tables ; les fonctions doivent partir avant.
DROP_SQL = """
DROP TRIGGER recommendation_immutable ON recommendation;
DROP TRIGGER demand_observation_append_only ON demand_observation;
DROP TRIGGER video_observation_append_only ON video_observation;
DROP FUNCTION forbid_recommendation_update();
DROP FUNCTION forbid_update();
DROP FUNCTION create_video_observation_partition(date);
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "collection_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quota_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection_run")),
    )
    op.create_table(
        "demand_series",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=False),
        sa.Column("granularity", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demand_series")),
        sa.UniqueConstraint(
            "source",
            "term",
            "region",
            "granularity",
            name=op.f("uq_demand_series_source_term_region_granularity"),
        ),
    )
    op.create_table(
        "topic",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(), nullable=True),
        sa.Column("clustering_version", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topic")),
    )
    op.create_table(
        "tracked_channel",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracked_channel")),
        sa.UniqueConstraint(
            "platform", "external_id", name=op.f("uq_tracked_channel_platform_external_id")
        ),
    )
    op.create_table(
        "user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user")),
        sa.UniqueConstraint("email", name=op.f("uq_user_email")),
    )
    op.create_table(
        "channel_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("profile_key", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("niche_label", sa.Text(), nullable=False),
        sa.Column("topic_description", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=False),
        sa.Column("tone", sa.Text(), nullable=True),
        sa.Column("editorial_rules", postgresql.JSONB(), nullable=False),
        sa.Column("target_platforms", postgresql.JSONB(), nullable=False),
        sa.Column("target_duration_s", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_channel_profile_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_profile")),
        sa.UniqueConstraint(
            "profile_key", "version", name=op.f("uq_channel_profile_profile_key_version")
        ),
    )
    op.create_index(
        "uq_channel_profile_current",
        "channel_profile",
        ["profile_key"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "demand_observation",
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["demand_series.id"],
            name=op.f("fk_demand_observation_series_id_demand_series"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "series_id", "period_start", "observed_at", name=op.f("pk_demand_observation")
        ),
    )
    op.create_table(
        "video",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_s", sa.Integer(), nullable=True),
        sa.Column("is_vertical", sa.Boolean(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("text_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["tracked_channel.id"],
            name=op.f("fk_video_channel_id_tracked_channel"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_video")),
        sa.UniqueConstraint("platform", "external_id", name=op.f("uq_video_platform_external_id")),
    )
    op.create_table(
        "calendar_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("recurrence", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["channel_profile.id"],
            name=op.f("fk_calendar_event_profile_id_channel_profile"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_event")),
    )
    op.create_table(
        "profile_seed_term",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["channel_profile.id"],
            name=op.f("fk_profile_seed_term_profile_id_channel_profile"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("profile_id", "term", name=op.f("pk_profile_seed_term")),
    )
    op.create_table(
        "profile_tracked_channel",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("tracked_channel_id", sa.Uuid(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "source IN ('manual', 'discovered')", name=op.f("ck_profile_tracked_channel_source")
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["channel_profile.id"],
            name=op.f("fk_profile_tracked_channel_profile_id_channel_profile"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tracked_channel_id"],
            ["tracked_channel.id"],
            name=op.f("fk_profile_tracked_channel_tracked_channel_id_tracked_channel"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "profile_id", "tracked_channel_id", name=op.f("pk_profile_tracked_channel")
        ),
    )
    op.create_table(
        "recommendation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=True),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("saturation_at_t", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "method IN ('plume', 'b0', 'b1', 'b2')", name=op.f("ck_recommendation_method")
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["channel_profile.id"],
            name=op.f("fk_recommendation_profile_id_channel_profile"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topic.id"],
            name=op.f("fk_recommendation_topic_id_topic"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_recommendation_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation")),
    )
    op.create_table(
        "topic_video",
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("video_id", sa.Uuid(), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topic.id"],
            name=op.f("fk_topic_video_topic_id_topic"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["video.id"],
            name=op.f("fk_topic_video_video_id_video"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("topic_id", "video_id", name=op.f("pk_topic_video")),
    )
    op.create_table(
        "video_observation",
        sa.Column("video_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("views", sa.BigInteger(), nullable=True),
        sa.Column("likes", sa.BigInteger(), nullable=True),
        sa.Column("comments", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["video.id"],
            name=op.f("fk_video_observation_video_id_video"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("video_id", "observed_at", name=op.f("pk_video_observation")),
        postgresql_partition_by="RANGE (observed_at)",
    )
    op.create_table(
        "recommendation_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recommendation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('kept', 'ignored', 'rejected')",
            name=op.f("ck_recommendation_feedback_action"),
        ),
        sa.ForeignKeyConstraint(
            ["recommendation_id"],
            ["recommendation.id"],
            name=op.f("fk_recommendation_feedback_recommendation_id_recommendation"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_recommendation_feedback_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_feedback")),
    )
    op.create_table(
        "recommendation_outcome",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recommendation_id", sa.Uuid(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("worked", sa.Boolean(), nullable=False),
        sa.Column("median_ratio", sa.Float(), nullable=True),
        sa.Column("video_count", sa.Integer(), nullable=False),
        sa.Column("peak_date", sa.Date(), nullable=True),
        sa.Column("lead_days", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["recommendation_id"],
            ["recommendation.id"],
            name=op.f("fk_recommendation_outcome_recommendation_id_recommendation"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_outcome")),
    )
    op.execute(PARTITION_SQL)
    op.execute(APPEND_ONLY_SQL)


def downgrade() -> None:
    op.execute(DROP_SQL)
    op.drop_table("recommendation_outcome")
    op.drop_table("recommendation_feedback")
    op.drop_table("video_observation")
    op.drop_table("topic_video")
    op.drop_table("recommendation")
    op.drop_table("profile_tracked_channel")
    op.drop_table("profile_seed_term")
    op.drop_table("calendar_event")
    op.drop_table("video")
    op.drop_table("demand_observation")
    op.drop_index(
        "uq_channel_profile_current",
        table_name="channel_profile",
        postgresql_where=sa.text("is_current"),
    )
    op.drop_table("channel_profile")
    op.drop_table("user")
    op.drop_table("tracked_channel")
    op.drop_table("topic")
    op.drop_table("demand_series")
    op.drop_table("collection_run")
    op.execute("DROP EXTENSION IF EXISTS vector")
