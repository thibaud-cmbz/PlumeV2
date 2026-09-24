"""EXPÉRIMENTAL — intérêt Google Trends par extraction non officielle (région France).

Aucune garantie : Google peut changer ces points d'accès ou refuser les requêtes (429).
Isolée derrière DemandSource pour être remplacée par l'API officielle sans toucher au reste ;
désactivable par PLUME_TRENDS_ENABLED. Les erreurs remontent à collect_demand qui les isole.

Valeur = indice relatif 0-100, normalisé par requête (donc par terme et par fenêtre) :
ce n'est pas un volume, et deux fenêtres ne sont pas directement comparables.
"""

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
from plume_core.models import DemandSeries

from plume_sources.demand import DemandPoint, charge, date_windows

BASE_URL = "https://trends.google.com/trends/"


class GoogleTrendsUnofficial:
    name = "google_trends"
    region = "FR"
    granularity = "daily"
    max_days = 90  # au-delà d'environ 9 mois, Google passe en données hebdomadaires

    def __init__(self, http: httpx.Client, budget: int) -> None:
        self._http = http
        self.budget = budget
        self.requests_made = 0
        self._has_cookie = False

    def fetch(self, series: DemandSeries, start: date, end: date) -> list[DemandPoint]:
        points = []
        for first, last in date_windows(start, end, self.max_days):
            explore = self._get(
                "api/explore",
                req=json.dumps(
                    {
                        "comparisonItem": [
                            {"keyword": series.term, "geo": self.region, "time": f"{first} {last}"}
                        ],
                        "category": 0,
                        "property": "",
                    }
                ),
            )
            widget = next(w for w in explore["widgets"] if w["id"] == "TIMESERIES")
            data = self._get(
                "api/widgetdata/multiline",
                req=json.dumps(widget["request"]),
                token=widget["token"],
            )
            for item in data["default"]["timelineData"]:
                if item["hasData"][0]:
                    period = datetime.fromtimestamp(int(item["time"]), UTC)
                    points.append(DemandPoint(period, float(item["value"][0])))
        return points

    def _get(self, path: str, **params: str) -> Any:
        if not self._has_cookie:
            # Sans le cookie posé par la page d'accueil, l'API répond 429.
            charge(self)
            self._http.get(BASE_URL, params={"geo": self.region}).raise_for_status()
            self._has_cookie = True
        charge(self)
        response = self._http.get(BASE_URL + path, params={"hl": "fr", "tz": "0", **params})
        response.raise_for_status()
        # Préfixe anti-JSON-hijacking « )]}' » sur la première ligne.
        return json.loads(response.text.split("\n", 1)[1])
