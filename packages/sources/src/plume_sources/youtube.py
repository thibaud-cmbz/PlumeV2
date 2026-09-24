"""Source YouTube Data API v3 : panel de chaînes suivi via les playlists d'uploads.

Données publiques uniquement (clé d'API, jamais d'OAuth), aucun commentaire ni donnée de
spectateur. Chaque appel est débité du grand livre de quota avant d'être envoyé.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from itertools import batched
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from plume_core.models import CollectionRun, TrackedChannel, Video, VideoObservation
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SOURCE = PLATFORM = "youtube"
API_URL = "https://www.googleapis.com/youtube/v3/"
COSTS = {"search": 100, "channels": 1, "playlistItems": 1, "videos": 1}
BATCH_SIZE = 50  # maximum d'identifiants ou de résultats par appel
PACIFIC = ZoneInfo("America/Los_Angeles")  # le quota est remis à zéro à minuit, heure du Pacifique
MAX_TEXT_RETENTION = timedelta(days=30)
QUOTA_EXCEEDED_STATUS = "quota_exceeded"
OK_STATUS = "ok"

Json = dict[str, Any]


class QuotaExceeded(Exception):
    """Budget du jour atteint, ou quota refusé par YouTube : toute la source s'arrête."""


def quota_day_start(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(PACIFIC).date(), time(), PACIFIC)


def next_quota_reset(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(PACIFIC).date() + timedelta(days=1), time(), PACIFIC)


@dataclass
class QuotaLedger:
    budget: int
    used: int = 0
    blocked_until: datetime | None = None

    def charge(self, method: str, now: datetime) -> None:
        if self.blocked_until is not None and now < self.blocked_until:
            raise QuotaExceeded(f"quota YouTube épuisé jusqu'à {self.blocked_until.isoformat()}")
        cost = COSTS[method]
        if self.used + cost > self.budget:
            raise QuotaExceeded(
                f"{method} ({cost} u) dépasserait le budget ({self.used}/{self.budget})"
            )
        self.used += cost

    def block(self, now: datetime) -> None:
        self.blocked_until = next_quota_reset(now)


def ledger_for_today(session: Session, budget: int, now: datetime) -> QuotaLedger:
    """Grand livre repris des collection_run du jour de quota en cours."""
    used, exceeded = session.execute(
        select(
            func.coalesce(func.sum(CollectionRun.quota_used), 0),
            func.bool_or(CollectionRun.status == QUOTA_EXCEEDED_STATUS),
        ).where(CollectionRun.source == SOURCE, CollectionRun.started_at >= quota_day_start(now))
    ).one()
    return QuotaLedger(budget, used, next_quota_reset(now) if exceeded else None)


def _is_quota_error(response: httpx.Response) -> bool:
    try:
        errors = response.json()["error"]["errors"]
    except (ValueError, KeyError, TypeError):
        return False
    return any(error.get("reason") == "quotaExceeded" for error in errors)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


class YouTubeClient:
    def __init__(self, http: httpx.Client, api_key: str, ledger: QuotaLedger) -> None:
        self._http = http
        self._api_key = api_key
        self.ledger = ledger

    def _get(self, method: str, **params: Any) -> Json:
        self.ledger.charge(method, datetime.now(UTC))
        # Clé en en-tête plutôt qu'en paramètre : elle n'apparaît pas dans les URL journalisées.
        response = self._http.get(
            API_URL + method, params=params, headers={"X-Goog-Api-Key": self._api_key}
        )
        if response.status_code == 403 and _is_quota_error(response):
            self.ledger.block(datetime.now(UTC))
            raise QuotaExceeded("quotaExceeded renvoyé par YouTube")
        response.raise_for_status()
        result: Json = response.json()
        return result

    def get_upload_playlists(self, channel_ids: list[str]) -> list[Json]:
        items: list[Json] = []
        for batch in batched(channel_ids, BATCH_SIZE):
            page = self._get(
                "channels", part="contentDetails,snippet", id=",".join(batch), maxResults=BATCH_SIZE
            )
            items += page.get("items", [])
        return items

    def list_recent_uploads(self, playlist_id: str, since: datetime) -> list[str]:
        """Identifiants des vidéos publiées depuis `since`, du plus récent au plus ancien."""
        video_ids: list[str] = []
        token: dict[str, str] = {}
        while True:
            page = self._get(
                "playlistItems",
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=BATCH_SIZE,
                **token,
            )
            for item in page.get("items", []):
                details = item["contentDetails"]
                published = details.get("videoPublishedAt")  # absent : vidéo privée ou supprimée
                if published is None:
                    continue
                # La playlist d'uploads est triée par date de publication décroissante.
                if _parse_time(published) < since:
                    return video_ids
                video_ids.append(details["videoId"])
            if "nextPageToken" not in page:
                return video_ids
            token = {"pageToken": page["nextPageToken"]}

    def get_videos(self, video_ids: list[str]) -> list[Json]:
        items: list[Json] = []
        for batch in batched(video_ids, BATCH_SIZE):
            page = self._get(
                "videos",
                part="snippet,contentDetails,statistics,player",
                id=",".join(batch),
                # Sans maxHeight, embedWidth/embedHeight sont absents de la réponse.
                maxHeight=1080,
                maxResults=BATCH_SIZE,
            )
            items += page.get("items", [])
        return items

    def search_channels(
        self, query: str, region: str, language: str, max_results: int = BATCH_SIZE
    ) -> list[Json]:
        """Découverte de chaînes (100 u par appel) : jamais pour suivre le panel."""
        page = self._get(
            "search",
            part="snippet",
            type="channel",
            q=query,
            regionCode=region,
            relevanceLanguage=language,
            maxResults=min(max_results, BATCH_SIZE),
        )
        items: list[Json] = page.get("items", [])
        return items


# --- Normalisation ----------------------------------------------------------------------

_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


def parse_duration(value: str | None) -> int | None:
    """ISO 8601 → secondes ; `P0D` (direct à venir) ou format inconnu → None."""
    match = _DURATION.fullmatch(value or "")
    if match is None:
        return None
    days, hours, minutes, seconds = (int(group or 0) for group in match.groups())
    return (((days * 24 + hours) * 60 + minutes) * 60 + seconds) or None


def is_vertical(player: Json) -> bool | None:
    # Le lecteur adopte le format de la vidéo (608 x 1080 contre 1920 x 1080 sur les fixtures).
    # Pas de déduction par la durée : des vidéos courtes sont horizontales.
    width, height = player.get("embedWidth"), player.get("embedHeight")
    if width is None or height is None:
        return None
    return int(height) > int(width)


def _count(statistics: Json, key: str) -> int | None:
    value = statistics.get(key)  # absent quand la chaîne masque le compteur
    return None if value is None else int(value)


def expiries(now: datetime, retention_days: int) -> tuple[datetime, datetime]:
    """(expires_at, text_expires_at) : les textes n'excèdent jamais 30 jours."""
    retention = timedelta(days=retention_days)
    return now + retention, now + min(retention, MAX_TEXT_RETENTION)


def channel_row(item: Json, now: datetime, retention_days: int) -> Json:
    # La ligne porte un titre : elle suit la rétention des textes, renouvelée à chaque passage.
    _, text_expires_at = expiries(now, retention_days)
    return {
        "platform": PLATFORM,
        "external_id": item["id"],
        "title": item["snippet"].get("title"),
        "first_seen_at": now,
        "active": True,
        "expires_at": text_expires_at,
    }


def video_row(item: Json, channel_id: UUID, now: datetime, retention_days: int) -> Json:
    snippet = item["snippet"]
    _, text_expires_at = expiries(now, retention_days)
    return {
        "platform": PLATFORM,
        "external_id": item["id"],
        "channel_id": channel_id,
        "published_at": _parse_time(snippet["publishedAt"]),
        "duration_s": parse_duration(item["contentDetails"].get("duration")),
        "is_vertical": is_vertical(item.get("player", {})),
        "language": snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage"),
        "first_seen_at": now,
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "text_expires_at": text_expires_at,
    }


def observation_row(item: Json, video_id: UUID, now: datetime, retention_days: int) -> Json:
    statistics = item.get("statistics", {})
    expires_at, _ = expiries(now, retention_days)
    return {
        "video_id": video_id,
        "observed_at": now,
        "views": _count(statistics, "viewCount"),
        "likes": _count(statistics, "likeCount"),
        "comments": _count(statistics, "commentCount"),
        "expires_at": expires_at,
    }


# --- Écriture ---------------------------------------------------------------------------


def upsert_channels(session: Session, rows: list[Json]) -> dict[str, UUID]:
    """Renvoie external_id → id pour toutes les lignes, nouvelles ou existantes."""
    if not rows:
        return {}
    statement = insert(TrackedChannel).values(rows)
    upsert = statement.on_conflict_do_update(
        index_elements=["platform", "external_id"],
        set_={"title": statement.excluded.title, "expires_at": statement.excluded.expires_at},
    ).returning(TrackedChannel.external_id, TrackedChannel.id)
    return {external_id: id_ for external_id, id_ in session.execute(upsert)}


def upsert_videos(session: Session, rows: list[Json]) -> dict[str, UUID]:
    """Seuls les textes et leur expiration sont rafraîchis sur une vidéo déjà connue."""
    if not rows:
        return {}
    statement = insert(Video).values(rows)
    upsert = statement.on_conflict_do_update(
        index_elements=["platform", "external_id"],
        set_={
            "title": statement.excluded.title,
            "description": statement.excluded.description,
            "text_expires_at": statement.excluded.text_expires_at,
        },
    ).returning(Video.external_id, Video.id)
    return {external_id: id_ for external_id, id_ in session.execute(upsert)}


def insert_observations(session: Session, rows: list[Json]) -> None:
    """Append-only : une observation déjà enregistrée (même vidéo, même instant) est ignorée."""
    if rows:
        session.execute(insert(VideoObservation).values(rows).on_conflict_do_nothing())


# --- Passage complet --------------------------------------------------------------------


@dataclass
class RunReport:
    """Ce qu'il faut écrire dans collection_run."""

    quota_used: int
    status: str


def collect(
    client: YouTubeClient,
    session: Session,
    channel_ids: list[str],
    since: datetime,
    now: datetime,
    retention_days: int,
) -> RunReport:
    """Uploads récents du panel puis statistiques de ces vidéos ; le commit revient à l'appelant.

    Sur QuotaExceeded, plus aucun appel n'est envoyé et ce qui est déjà écrit est conservé.
    """
    start = client.ledger.used
    try:
        channels = client.get_upload_playlists(channel_ids)
        channel_uuids = upsert_channels(
            session, [channel_row(item, now, retention_days) for item in channels]
        )
        video_ids: list[str] = []
        for channel in channels:
            playlist_id = channel["contentDetails"]["relatedPlaylists"]["uploads"]
            try:
                video_ids += client.list_recent_uploads(playlist_id, since)
            except httpx.HTTPError as error:
                logger.warning("chaîne ignorée", extra={"channel": channel["id"], "error": error})
        for batch in batched(video_ids, BATCH_SIZE):
            items = client.get_videos(list(batch))
            video_uuids = upsert_videos(
                session,
                [
                    video_row(
                        item, channel_uuids[item["snippet"]["channelId"]], now, retention_days
                    )
                    for item in items
                ],
            )
            insert_observations(
                session,
                [
                    observation_row(item, video_uuids[item["id"]], now, retention_days)
                    for item in items
                ],
            )
    except QuotaExceeded as error:
        logger.warning("source YouTube arrêtée", extra={"error": str(error)})
        return RunReport(client.ledger.used - start, QUOTA_EXCEEDED_STATUS)
    return RunReport(client.ledger.used - start, OK_STATUS)
