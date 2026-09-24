"""Sources de demande : interface commune, écriture append-only et collecte isolée par source.

Une panne d'une source (exception, budget épuisé, erreur SQL) n'affecte jamais les autres :
elle est journalisée et sa collection_run passe en échec.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from plume_core.models import CollectionRun, DemandObservation, DemandSeries
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

OK_STATUS = "ok"
FAILED_STATUS = "failed"


@dataclass(frozen=True)
class DemandPoint:
    period_start: datetime
    value: float


class DemandSource(Protocol):
    name: str
    region: str
    granularity: str
    budget: int  # requêtes HTTP autorisées par passage
    requests_made: int

    def fetch(self, series: DemandSeries, start: date, end: date) -> list[DemandPoint]: ...


class BudgetExhausted(Exception):
    """Budget de requêtes du passage atteint : la source s'arrête."""


def charge(source: DemandSource) -> None:
    """À appeler avant chaque requête HTTP."""
    if source.requests_made >= source.budget:
        raise BudgetExhausted(f"{source.name} : budget de {source.budget} requêtes atteint")
    source.requests_made += 1


def date_windows(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """Découpe [start, end] (bornes incluses) en fenêtres contiguës d'au plus max_days jours."""
    windows = []
    while start <= end:
        last = min(end, start + timedelta(days=max_days - 1))
        windows.append((start, last))
        start = last + timedelta(days=1)
    return windows


def get_or_create_series(session: Session, source: DemandSource, term: str) -> DemandSeries:
    key = {
        "source": source.name,
        "term": term,
        "region": source.region,
        "granularity": source.granularity,
    }
    session.execute(insert(DemandSeries).values(**key).on_conflict_do_nothing())
    return session.scalars(select(DemandSeries).filter_by(**key)).one()


def append_observations(
    session: Session, series: DemandSeries, points: list[DemandPoint], now: datetime
) -> int:
    """Nouvelle ligne pour chaque point nouveau ou révisé ; une valeur inchangée n'ajoute rien."""
    latest = dict(
        session.execute(
            select(DemandObservation.period_start, DemandObservation.value)
            .where(DemandObservation.series_id == series.id)
            .distinct(DemandObservation.period_start)
            .order_by(DemandObservation.period_start, DemandObservation.observed_at.desc())
        )
        .tuples()
        .all()
    )
    rows = [
        {
            "series_id": series.id,
            "period_start": point.period_start,
            "observed_at": now,
            "value": point.value,
        }
        for point in points
        if latest.get(point.period_start) != point.value
    ]
    if rows:
        session.execute(insert(DemandObservation).values(rows).on_conflict_do_nothing())
    return len(rows)


def collect_demand(
    session: Session,
    jobs: list[tuple[DemandSource, list[str]]],
    start: date,
    end: date,
    now: datetime,
) -> list[CollectionRun]:
    """Une collection_run par source ; le commit revient à l'appelant.

    Un savepoint par terme : sur erreur, la source s'arrête et garde les termes déjà écrits.
    """
    runs = []
    for source, terms in jobs:
        run = CollectionRun(source=source.name, started_at=now, status=OK_STATUS)
        before = source.requests_made
        try:
            for term in terms:
                with session.begin_nested():
                    series = get_or_create_series(session, source, term)
                    points = source.fetch(series, start, end)
                    append_observations(session, series, points, now)
        except Exception as error:
            logger.exception("source de demande en échec", extra={"source": source.name})
            run.status, run.error = FAILED_STATUS, f"{type(error).__name__}: {error}"
        run.quota_used = source.requests_made - before
        run.finished_at = datetime.now(UTC)
        session.add(run)
        session.flush()
        runs.append(run)
    return runs
