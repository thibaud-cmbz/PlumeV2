"""Modèles de la phase 1.

La niche est une donnée : aucune liste fermée de niches, ni ici ni en base.
Les états sont des VARCHAR, bornés par des CHECK quand l'ensemble est fermé.
"""

from datetime import date, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from pydantic import TypeAdapter
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from plume_core.schemas import RecommendationReason, TargetDurations

# Tables alimentées par l'API YouTube → colonne d'expiration obligatoire (non nulle).
YOUTUBE_EXPIRY_COLUMNS: dict[str, str] = {
    "tracked_channel": "expires_at",
    "video": "text_expires_at",
    "video_observation": "expires_at",
    "topic": "expires_at",
    "topic_video": "expires_at",
}


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


class PydanticJSON(TypeDecorator[Any]):
    """JSONB validé par un TypeAdapter Pydantic à l'écriture comme à la lecture."""

    impl = JSONB
    cache_ok = True

    def __init__(self, adapter: TypeAdapter[Any]) -> None:
        super().__init__()
        self.adapter = adapter

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        return self.adapter.dump_python(self.adapter.validate_python(value), mode="json")

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        return None if value is None else self.adapter.validate_python(value)


STRING_LIST = PydanticJSON(TypeAdapter(list[str]))


def _pk() -> Mapped[UUID]:
    return mapped_column(primary_key=True, default=uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def _fk(target: str, ondelete: str = "CASCADE", **kwargs: Any) -> Any:
    return mapped_column(ForeignKey(target, ondelete=ondelete), **kwargs)


# --- Zone privée (propriétaire) ---------------------------------------------------------


class User(Base):
    __tablename__ = "user"

    id: Mapped[UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    created_at: Mapped[datetime] = _now()


class ChannelProfile(Base):
    """Versionné par copie : une modification crée une ligne avec version + 1."""

    __tablename__ = "channel_profile"
    __table_args__ = (
        UniqueConstraint("profile_key", "version"),
        Index(
            "uq_channel_profile_current",
            "profile_key",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[UUID] = _pk()
    user_id: Mapped[UUID] = _fk("user.id")
    profile_key: Mapped[UUID]  # commun à toutes les versions d'un même profil
    version: Mapped[int]
    is_current: Mapped[bool]
    name: Mapped[str] = mapped_column(Text)
    niche_label: Mapped[str] = mapped_column(Text)
    topic_description: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16))
    region: Mapped[str] = mapped_column(String(16))
    tone: Mapped[str | None] = mapped_column(Text)
    editorial_rules: Mapped[list[str]] = mapped_column(STRING_LIST)
    target_platforms: Mapped[list[str]] = mapped_column(STRING_LIST)
    target_duration_s: Mapped[TargetDurations] = mapped_column(
        PydanticJSON(TypeAdapter(TargetDurations))
    )
    created_at: Mapped[datetime] = _now()


class ProfileSeedTerm(Base):
    __tablename__ = "profile_seed_term"

    profile_id: Mapped[UUID] = _fk("channel_profile.id", primary_key=True)
    term: Mapped[str] = mapped_column(Text, primary_key=True)
    weight: Mapped[float] = mapped_column(Float)


class ProfileTrackedChannel(Base):
    __tablename__ = "profile_tracked_channel"
    __table_args__ = (CheckConstraint("source IN ('manual', 'discovered')", name="source"),)

    profile_id: Mapped[UUID] = _fk("channel_profile.id", primary_key=True)
    tracked_channel_id: Mapped[UUID] = _fk("tracked_channel.id", primary_key=True)
    added_at: Mapped[datetime] = _now()
    source: Mapped[str] = mapped_column(String(16))


class CalendarEvent(Base):
    __tablename__ = "calendar_event"

    id: Mapped[UUID] = _pk()
    profile_id: Mapped[UUID] = _fk("channel_profile.id")
    title: Mapped[str] = mapped_column(Text)
    starts_on: Mapped[date]
    ends_on: Mapped[date | None]
    recurrence: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)


# --- Catalogue partagé (sans propriétaire) ----------------------------------------------


class TrackedChannel(Base):
    __tablename__ = "tracked_channel"
    __table_args__ = (UniqueConstraint("platform", "external_id"),)

    id: Mapped[UUID] = _pk()
    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime]
    active: Mapped[bool]
    expires_at: Mapped[datetime]


class Video(Base):
    __tablename__ = "video"
    __table_args__ = (UniqueConstraint("platform", "external_id"),)

    id: Mapped[UUID] = _pk()
    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    channel_id: Mapped[UUID] = _fk("tracked_channel.id")
    published_at: Mapped[datetime]
    duration_s: Mapped[int | None]
    is_vertical: Mapped[bool | None]
    language: Mapped[str | None] = mapped_column(String(16))
    first_seen_at: Mapped[datetime]
    # Textes purgeables : l'identifiant survit à leur expiration.
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    text_expires_at: Mapped[datetime]


class VideoObservation(Base):
    """Append-only, partitionnée par mois sur observed_at (partitions créées en SQL)."""

    __tablename__ = "video_observation"
    __table_args__ = ({"postgresql_partition_by": "RANGE (observed_at)"},)

    video_id: Mapped[UUID] = _fk("video.id", primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(primary_key=True)
    views: Mapped[int | None] = mapped_column(BigInteger)
    likes: Mapped[int | None] = mapped_column(BigInteger)
    comments: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime]


class DemandSeries(Base):
    __tablename__ = "demand_series"
    __table_args__ = (UniqueConstraint("source", "term", "region", "granularity"),)

    id: Mapped[UUID] = _pk()
    source: Mapped[str] = mapped_column(String(32))
    term: Mapped[str] = mapped_column(Text)
    region: Mapped[str] = mapped_column(String(16))
    granularity: Mapped[str] = mapped_column(String(16))


class DemandObservation(Base):
    """Append-only : chaque révision d'une même période est une nouvelle ligne."""

    __tablename__ = "demand_observation"

    series_id: Mapped[UUID] = _fk("demand_series.id", primary_key=True)
    period_start: Mapped[datetime] = mapped_column(primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(primary_key=True)
    value: Mapped[float] = mapped_column(Float)


class CollectionRun(Base):
    __tablename__ = "collection_run"

    id: Mapped[UUID] = _pk()
    source: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None]
    quota_used: Mapped[int] = mapped_column(Integer, server_default="0")
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)


class Topic(Base):
    __tablename__ = "topic"

    id: Mapped[UUID] = _pk()
    label: Mapped[str] = mapped_column(Text)
    # ponytail: sans dimension ni index tant que le modèle d'embeddings n'est pas choisi.
    embedding: Mapped[Any] = mapped_column(Vector(), nullable=True)
    clustering_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime]


class TopicVideo(Base):
    __tablename__ = "topic_video"

    topic_id: Mapped[UUID] = _fk("topic.id", primary_key=True)
    video_id: Mapped[UUID] = _fk("video.id", primary_key=True)
    similarity: Mapped[float] = mapped_column(Float)
    assigned_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime]


# --- Recommandations (privé) ------------------------------------------------------------


class Recommendation(Base):
    """Jamais mise à jour après création (trigger), sauf topic_id → NULL à la purge du topic."""

    __tablename__ = "recommendation"
    __table_args__ = (CheckConstraint("method IN ('plume', 'b0', 'b1', 'b2')", name="method"),)

    id: Mapped[UUID] = _pk()
    user_id: Mapped[UUID] = _fk("user.id")
    profile_id: Mapped[UUID] = _fk("channel_profile.id")
    profile_version: Mapped[int]
    topic_id: Mapped[UUID | None] = _fk("topic.id", ondelete="SET NULL")
    method: Mapped[str] = mapped_column(String(16))
    model_version: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime]
    rank: Mapped[int]
    score: Mapped[float] = mapped_column(Float)
    reasons: Mapped[list[RecommendationReason]] = mapped_column(
        PydanticJSON(TypeAdapter(list[RecommendationReason]))
    )
    saturation_at_t: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = _now()


class RecommendationOutcome(Base):
    __tablename__ = "recommendation_outcome"

    id: Mapped[UUID] = _pk()
    recommendation_id: Mapped[UUID] = _fk("recommendation.id")
    evaluated_at: Mapped[datetime]
    worked: Mapped[bool]
    median_ratio: Mapped[float | None] = mapped_column(Float)
    video_count: Mapped[int]
    peak_date: Mapped[date | None]
    lead_days: Mapped[int | None]


class RecommendationFeedback(Base):
    __tablename__ = "recommendation_feedback"
    __table_args__ = (CheckConstraint("action IN ('kept', 'ignored', 'rejected')", name="action"),)

    id: Mapped[UUID] = _pk()
    recommendation_id: Mapped[UUID] = _fk("recommendation.id")
    user_id: Mapped[UUID] = _fk("user.id")
    action: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
