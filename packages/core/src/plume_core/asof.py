"""Lecture « à la date T » : seul accès aux tables autorisé pour discovery.

Chaque requête ne renvoie que ce que Plume connaissait à T (colonnes de connaissance,
jamais de dates métier comme published_at).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select

from plume_core.models import (
    ChannelProfile,
    DemandObservation,
    Topic,
    TopicVideo,
    Video,
    VideoObservation,
)


class AsOf:
    def __init__(self, t: datetime) -> None:
        if t.utcoffset() is None:
            raise ValueError("t doit être timezone-aware")
        self.t = t

    def latest_video_observations(self) -> Select[tuple[VideoObservation]]:
        return (
            select(VideoObservation)
            .where(VideoObservation.observed_at <= self.t)
            .distinct(VideoObservation.video_id)
            .order_by(VideoObservation.video_id, VideoObservation.observed_at.desc())
        )

    def known_videos(self) -> Select[tuple[Video]]:
        return select(Video).where(Video.first_seen_at <= self.t)

    def demand_series_values(self) -> Select[tuple[DemandObservation]]:
        return (
            select(DemandObservation)
            .where(DemandObservation.observed_at <= self.t)
            .distinct(DemandObservation.series_id, DemandObservation.period_start)
            .order_by(
                DemandObservation.series_id,
                DemandObservation.period_start,
                DemandObservation.observed_at.desc(),
            )
        )

    def active_profile(self, profile_key: UUID) -> Select[tuple[ChannelProfile]]:
        return (
            select(ChannelProfile)
            .where(ChannelProfile.profile_key == profile_key, ChannelProfile.created_at <= self.t)
            .order_by(ChannelProfile.version.desc())
            .limit(1)
        )

    def topic_assignments(self, clustering_version: str) -> Select[tuple[TopicVideo]]:
        return (
            select(TopicVideo)
            .join(Topic)
            .where(
                Topic.clustering_version == clustering_version,
                TopicVideo.assigned_at <= self.t,
            )
        )
