import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from plume_core.http import make_client
from plume_sources.youtube import (
    PACIFIC,
    QuotaExceeded,
    QuotaLedger,
    YouTubeClient,
    expiries,
    observation_row,
    parse_duration,
    video_row,
)
from tenacity import wait_none

FIXTURES = Path(__file__).parents[2] / "fixtures" / "youtube"
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


VIDEOS = {item["id"]: item for item in fixture("videos.json")["items"]}


def client_for(
    handler: Callable[[httpx.Request], httpx.Response], budget: int = 7000
) -> tuple[YouTubeClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    http = make_client(transport=httpx.MockTransport(recording), wait=wait_none())
    return YouTubeClient(http, "secret-key", QuotaLedger(budget)), calls


def test_pagination_follows_token_and_stops_at_since() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = "p2" if "pageToken" in request.url.params else "p1"
        return httpx.Response(200, json=fixture(f"playlist_items_{page}.json"))

    client, calls = client_for(handler)
    recent = client.list_recent_uploads("UU0", since=datetime(2026, 9, 16, tzinfo=UTC))
    assert (recent, len(calls)) == (["vid00000000", "vid00000001"], 1)

    client, calls = client_for(handler)
    recent = client.list_recent_uploads("UU0", since=datetime(2026, 9, 2, tzinfo=UTC))
    assert recent == [f"vid0000000{i}" for i in range(6)]
    assert len(calls) == 2
    assert calls[1].url.params["pageToken"] == fixture("playlist_items_p1.json")["nextPageToken"]
    assert client.ledger.used == 2


def test_key_is_sent_as_header_not_in_url() -> None:
    client, calls = client_for(lambda _: httpx.Response(200, json={"items": []}))
    client.get_videos(["a"])
    assert calls[0].headers["X-Goog-Api-Key"] == "secret-key"
    assert "secret-key" not in str(calls[0].url)


@pytest.mark.parametrize(
    ("method", "call"),
    [
        ("channels", lambda c, ids: c.get_upload_playlists(ids)),
        ("videos", lambda c, ids: c.get_videos(ids)),
    ],
)
def test_ids_are_batched_by_50(
    method: str, call: Callable[[YouTubeClient, list[str]], Any]
) -> None:
    client, calls = client_for(lambda _: httpx.Response(200, json={"items": []}))
    call(client, [f"id{i}" for i in range(120)])
    assert [len(c.url.params["id"].split(",")) for c in calls] == [50, 50, 20]
    assert {c.url.path.rsplit("/", 1)[1] for c in calls} == {method}
    assert client.ledger.used == 3


def test_videos_request_player_dimensions() -> None:
    client, calls = client_for(lambda _: httpx.Response(200, json={"items": []}))
    client.get_videos(["a"])
    params = calls[0].url.params
    assert params["part"] == "snippet,contentDetails,statistics,player"
    assert "maxHeight" in params


def test_search_costs_100_units() -> None:
    client, calls = client_for(lambda _: httpx.Response(200, json=fixture("search_channels.json")))
    channels = client.search_channels("terme", "FR", "fr", max_results=5)
    assert len(channels) == 5
    assert calls[0].url.params["type"] == "channel"
    assert client.ledger.used == 100


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("PT2M27S", 147),
        ("PT1H2M3S", 3723),
        ("P1DT1S", 86401),
        ("PT17S", 17),
        ("P0D", None),
        ("", None),
    ],
)
def test_parse_duration(value: str, seconds: int | None) -> None:
    assert parse_duration(value) == seconds


def test_is_vertical_from_player_on_real_fixtures() -> None:
    row = {vid: video_row(item, uuid4(), NOW, 30)["is_vertical"] for vid, item in VIDEOS.items()}
    # Vérités terrain relevées sur les vidéos d'origine.
    assert [vid for vid, vertical in row.items() if vertical] == [
        "vid00000002", "vid00000004", "vid00000006", "vid00000009",
        *(f"vid000000{i}" for i in range(10, 15)),
    ]  # fmt: skip
    # vid00000005 dure 56 s et elle est horizontale : la durée ne suffit pas.
    assert VIDEOS["vid00000005"]["contentDetails"]["duration"] == "PT56S"
    assert row["vid00000005"] is False
    assert (
        video_row({**VIDEOS["vid00000005"], "player": {}}, uuid4(), NOW, 30)["is_vertical"] is None
    )


def test_video_normalisation() -> None:
    channel_id = uuid4()
    row = video_row(VIDEOS["vid00000000"], channel_id, NOW, 30)
    assert row["external_id"] == "vid00000000"
    assert row["channel_id"] == channel_id
    assert row["published_at"] == datetime(2026, 9, 22, 23, 0, 10, tzinfo=UTC)
    assert row["duration_s"] == 147
    assert row["language"] == "en"
    assert row["title"] == "Titre anonymisé"


@pytest.mark.parametrize(
    ("snippet", "language"),
    [
        ({"defaultAudioLanguage": "fr", "defaultLanguage": "en"}, "fr"),
        ({"defaultLanguage": "en"}, "en"),
        ({}, None),
    ],
)
def test_language_fallback(snippet: dict[str, str], language: str | None) -> None:
    item = VIDEOS["vid00000000"]
    base = {
        k: v
        for k, v in item["snippet"].items()
        if k not in ("defaultAudioLanguage", "defaultLanguage")
    }
    row = video_row({**item, "snippet": {**base, **snippet}}, uuid4(), NOW, 30)
    assert row["language"] == language


def test_observation_with_hidden_counters() -> None:
    item = {**VIDEOS["vid00000000"], "statistics": {"viewCount": "12"}}
    row = observation_row(item, uuid4(), NOW, 30)
    assert (row["views"], row["likes"], row["comments"]) == (12, None, None)
    assert row["observed_at"] == NOW


@pytest.mark.parametrize(("retention", "text_days"), [(30, 30), (7, 7), (1095, 30)])
def test_text_expiry_never_exceeds_30_days(retention: int, text_days: int) -> None:
    assert expiries(NOW, retention) == (
        NOW + timedelta(days=retention),
        NOW + timedelta(days=text_days),
    )
    assert video_row(VIDEOS["vid00000000"], uuid4(), NOW, retention)["text_expires_at"] == (
        NOW + timedelta(days=text_days)
    )
    assert observation_row(VIDEOS["vid00000000"], uuid4(), NOW, retention)["expires_at"] == (
        NOW + timedelta(days=retention)
    )


def test_ledger_refuses_before_exceeding_budget() -> None:
    client, calls = client_for(lambda _: httpx.Response(200, json={"items": []}), budget=102)
    client.ledger.used = 100
    with pytest.raises(QuotaExceeded):
        client.search_channels("terme", "FR", "fr")
    client.get_videos(["a"])
    client.get_videos(["a"])
    with pytest.raises(QuotaExceeded):
        client.get_videos(["a"])
    # Refus locaux : aucune requête envoyée, rien de débité.
    assert (client.ledger.used, len(calls)) == (102, 2)


def test_quota_exceeded_stops_every_call() -> None:
    client, calls = client_for(lambda _: httpx.Response(403, json=fixture("quota_exceeded.json")))
    with pytest.raises(QuotaExceeded):
        client.get_videos(["a"])
    for call in (
        lambda: client.get_videos(["a"]),
        lambda: client.get_upload_playlists(["UC"]),
        lambda: client.list_recent_uploads("UU", NOW),
    ):
        with pytest.raises(QuotaExceeded):
            call()
    assert len(calls) == 1
    assert client.ledger.blocked_until is not None
    local = client.ledger.blocked_until.astimezone(PACIFIC)
    assert (local.hour, local.minute) == (0, 0)


def test_blocked_until_is_next_pacific_midnight() -> None:
    ledger = QuotaLedger(7000)
    ledger.block(datetime(2026, 9, 24, 6, tzinfo=UTC))  # 23 h la veille à Los Angeles (UTC-7)
    assert ledger.blocked_until == datetime(2026, 9, 24, 7, tzinfo=UTC)
    with pytest.raises(QuotaExceeded):
        ledger.charge("videos", datetime(2026, 9, 24, 6, 59, tzinfo=UTC))
    ledger.charge("videos", datetime(2026, 9, 24, 7, tzinfo=UTC))
    assert ledger.used == 1


def test_other_403_is_not_a_quota_stop() -> None:
    client, _ = client_for(
        lambda _: httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}})
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.get_videos(["a"])
    assert client.ledger.blocked_until is None
