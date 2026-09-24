import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from plume_core.config import Settings
from plume_core.http import make_client
from plume_core.models import CollectionRun, TrackedChannel, Video, VideoObservation
from plume_sources.youtube import (
    QUOTA_EXCEEDED_STATUS,
    QuotaExceeded,
    QuotaLedger,
    RunReport,
    YouTubeClient,
    collect,
    ledger_for_today,
)
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from tenacity import wait_none

FIXTURES = Path(__file__).parents[2] / "fixtures" / "youtube"
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
SINCE = NOW - timedelta(days=30)
RETENTION = 30
PANEL = [f"UCpanel{i:017d}" for i in range(200)]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(scope="module", autouse=True)
def _migrated() -> None:
    command.upgrade(Config(toml_file=Path(__file__).parents[2] / "pyproject.toml"), "head")


@pytest.fixture
def session() -> Iterator[Session]:
    # Tout est annulé en fin de test : la base reste vide.
    engine = create_engine(Settings().sqlalchemy_url)
    with engine.connect() as connection, Session(bind=connection) as session:
        session.execute(text("SELECT create_video_observation_partition('2026-09-01')"))
        yield session
        session.rollback()
    engine.dispose()


class FakeYouTube:
    """Sert les fixtures à tout panel : chaque chaîne a les 10 uploads des pages réelles
    (tous de moins de 30 jours), suivis d'un upload ancien qui arrête la pagination."""

    def __init__(self, title: str = "Titre anonymisé", failing: frozenset[str] = frozenset()):
        self.title = title
        self.failing = failing
        self.calls: list[httpx.Request] = []
        self.channel = fixture("channels.json")["items"][0]
        self.uploads = [
            *fixture("playlist_items_p1.json")["items"],
            *fixture("playlist_items_p2.json")["items"],
        ]
        self.videos = fixture("videos.json")["items"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        method = request.url.path.rsplit("/", 1)[1]
        params = request.url.params
        if method == "channels":
            return self._items(self._channel(cid) for cid in params["id"].split(","))
        if method == "playlistItems":
            playlist = params["playlistId"]
            if playlist in self.failing:
                return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})
            old = {"contentDetails": {"videoId": "old", "videoPublishedAt": "2026-01-01T00:00:00Z"}}
            items = [*self.uploads, old]
            return self._items(
                {"contentDetails": {**i["contentDetails"], "videoId": f"{playlist}.{n}"}}
                for n, i in enumerate(items)
            )
        assert method == "videos"
        return self._items(self._video(vid) for vid in params["id"].split(","))

    @staticmethod
    def _items(items: Any) -> httpx.Response:
        return httpx.Response(200, json={"items": list(items)})

    def _channel(self, channel_id: str) -> Any:
        item = copy.deepcopy(self.channel)
        item["id"] = channel_id
        item["contentDetails"]["relatedPlaylists"]["uploads"] = "UU" + channel_id[2:]
        return item

    def _video(self, video_id: str) -> Any:
        playlist, n = video_id.split(".")
        item = copy.deepcopy(self.videos[int(n)])
        item["id"] = video_id
        item["snippet"]["channelId"] = "UC" + playlist[2:]
        item["snippet"]["title"] = self.title
        return item


def run(session: Session, fake: FakeYouTube, ledger: QuotaLedger, now: datetime = NOW) -> RunReport:
    http = make_client(transport=httpx.MockTransport(fake), wait=wait_none())
    client = YouTubeClient(http, "secret-key", ledger)
    return collect(client, session, PANEL, SINCE, now, RETENTION)


def counts(session: Session) -> tuple[int, ...]:
    return tuple(
        session.scalar(select(func.count()).select_from(model)) or 0
        for model in (TrackedChannel, Video, VideoObservation)
    )


def test_full_pass_for_200_channels_under_500_units(session: Session) -> None:
    report = run(session, FakeYouTube(), QuotaLedger(7000))

    print(f"\nBudget consommé pour {len(PANEL)} chaînes : {report.quota_used} unités")
    assert report == RunReport(quota_used=4 + 200 + 40, status="ok")
    assert report.quota_used < 500
    assert counts(session) == (200, 2000, 2000)


def test_every_row_has_its_expiry(session: Session) -> None:
    run(session, FakeYouTube(), QuotaLedger(7000))
    text_expiry, expiry = NOW + timedelta(days=30), NOW + timedelta(days=RETENTION)
    for column, expected in (
        (TrackedChannel.expires_at, text_expiry),
        (Video.text_expires_at, text_expiry),
        (VideoObservation.expires_at, expiry),
    ):
        assert session.scalars(select(column).distinct()).all() == [expected]


def test_reinsertion_is_idempotent(session: Session) -> None:
    run(session, FakeYouTube(), QuotaLedger(7000))
    run(session, FakeYouTube(), QuotaLedger(7000))
    assert counts(session) == (200, 2000, 2000)


def test_later_pass_refreshes_only_video_texts(session: Session) -> None:
    columns = (Video.external_id, Video.id, Video.first_seen_at, Video.duration_s, Video.language)
    run(session, FakeYouTube(), QuotaLedger(7000))
    before = sorted(session.execute(select(*columns)).all())

    later = NOW + timedelta(hours=1)
    run(session, FakeYouTube(title="Titre modifié"), QuotaLedger(7000), now=later)

    assert sorted(session.execute(select(*columns)).all()) == before
    texts = session.execute(select(Video.title, Video.text_expires_at).distinct()).tuples()
    assert list(texts) == [("Titre modifié", later + timedelta(days=30))]
    # Nouvel instant d'observation : une nouvelle ligne par vidéo.
    assert counts(session) == (200, 2000, 4000)


def test_failing_channel_does_not_stop_the_others(session: Session) -> None:
    fake = FakeYouTube(failing=frozenset({"UU" + PANEL[3][2:]}))
    assert run(session, fake, QuotaLedger(7000)).status == "ok"
    assert counts(session) == (200, 1990, 1990)


def test_quota_exceeded_stops_the_source_and_keeps_written_rows(session: Session) -> None:
    fake = FakeYouTube()
    original = fake.__call__

    def quota_after_playlists(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/videos") and len(fake.calls) > 204:
            fake.calls.append(request)
            return httpx.Response(403, json=fixture("quota_exceeded.json"))
        return original(request)

    http = make_client(transport=httpx.MockTransport(quota_after_playlists), wait=wait_none())
    client = YouTubeClient(http, "secret-key", QuotaLedger(7000))
    report = collect(client, session, PANEL, SINCE, NOW, RETENTION)

    assert report.status == QUOTA_EXCEEDED_STATUS
    # 4 + 200 + 1 lot de vidéos réussi + l'appel refusé par YouTube, puis plus rien.
    assert len(fake.calls) == 206
    assert counts(session) == (200, 50, 50)
    with pytest.raises(QuotaExceeded):
        client.get_videos(["x"])
    assert len(fake.calls) == 206


def test_ledger_resumes_from_todays_runs(session: Session) -> None:
    # 12 h UTC = 5 h à Los Angeles : le jour de quota a commencé à 7 h UTC.
    for started_at, used, status in (
        (datetime(2026, 9, 24, 6, 59, tzinfo=UTC), 1000, QUOTA_EXCEEDED_STATUS),  # la veille
        (datetime(2026, 9, 24, 7, tzinfo=UTC), 300, "ok"),
        (datetime(2026, 9, 24, 10, tzinfo=UTC), 200, "ok"),
    ):
        session.add(
            CollectionRun(source="youtube", started_at=started_at, quota_used=used, status=status)
        )
    session.flush()
    assert ledger_for_today(session, 7000, NOW) == QuotaLedger(7000, 500, None)

    session.add(
        CollectionRun(source="youtube", started_at=NOW, quota_used=5, status=QUOTA_EXCEEDED_STATUS)
    )
    session.flush()
    ledger = ledger_for_today(session, 7000, NOW)
    assert ledger == QuotaLedger(7000, 505, datetime(2026, 9, 25, 7, tzinfo=UTC))
