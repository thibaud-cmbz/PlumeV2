"""Pages vues quotidiennes des articles fr.wikipedia (API REST Wikimedia, accès libre).

Règles Wikimedia : User-Agent identifiant un contact, requêtes en série. Les réponses sont
gardées en cache local pour la journée ; le rejeu sur 429 vient de plume_core.http.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from plume_core.models import DemandSeries, ProfileSeedTerm
from sqlalchemy import select
from sqlalchemy.orm import Session

from plume_sources.demand import DemandPoint, charge, date_windows

logger = logging.getLogger(__name__)

API_URL = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"


def utc_now() -> datetime:
    return datetime.now(UTC)


def user_agent(contact: str) -> str:
    return f"Plume/{version('plume-sources')} ({contact})"


def profile_articles(session: Session, profile_id: UUID) -> list[str]:
    """Titres d'articles associés aux termes du profil ; un terme sans titre est ignoré."""
    titles = session.scalars(
        select(ProfileSeedTerm.wikipedia_title).where(ProfileSeedTerm.profile_id == profile_id)
    )
    return [title for title in titles if title]


class WikipediaPageviews:
    """series.term est le titre de l'article."""

    name = "wikipedia"
    region = "fr"  # édition linguistique, pas un pays
    granularity = "daily"
    max_days = 366

    def __init__(
        self,
        http: httpx.Client,
        contact: str | None,
        budget: int,
        cache_dir: Path,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not contact:
            raise ValueError("PLUME_WIKIMEDIA_CONTACT est obligatoire (règles Wikimedia)")
        self._http = http
        self._user_agent = user_agent(contact)
        self.budget = budget
        self.requests_made = 0
        self._cache_dir = cache_dir / self.name
        self._clock = clock

    def fetch(self, series: DemandSeries, start: date, end: date) -> list[DemandPoint]:
        article = quote(series.term.replace(" ", "_"), safe="")
        points = []
        for first, last in date_windows(start, end, self.max_days):
            url = (
                f"{API_URL}fr.wikipedia/all-access/user/{article}/daily/"
                f"{first:%Y%m%d}00/{last:%Y%m%d}00"
            )
            for item in self._get(url).get("items", []):
                period = datetime.strptime(item["timestamp"], "%Y%m%d%H").replace(tzinfo=UTC)
                points.append(DemandPoint(period, float(item["views"])))
        return points

    def _get(self, url: str) -> Any:
        # ponytail: pas de purge des jours passés, ajouter si le dossier grossit.
        day = self._clock().date().isoformat()
        cached = self._cache_dir / day / f"{hashlib.sha256(url.encode()).hexdigest()}.json"
        if cached.exists():
            return json.loads(cached.read_text())
        charge(self)
        response = self._http.get(url, headers={"User-Agent": self._user_agent})
        if response.status_code == 404:  # article inconnu ou sans vues sur la période
            logger.warning("article sans données", extra={"url": url})
            return {}
        response.raise_for_status()
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(response.text)
        return response.json()
