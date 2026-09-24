# Plume

Détecte les sujets de vidéos verticales qui vont marcher, pour des niches définies par l'utilisateur.

## Installation

Prérequis : [uv](https://docs.astral.sh/uv/) et Docker (tests d'intégration uniquement).

```sh
uv sync
docker compose up -d   # PostgreSQL 17 + pgvector sur localhost:5432
uv run alembic upgrade head
```

## Commandes

```sh
uv run ruff format .                      # formatage (--check en CI)
uv run ruff check .                       # lint
uv run mypy .                             # typage strict
uv run lint-imports                       # dépendances entre paquets
uv run pytest -m unit                     # tests unitaires, sans Docker
uv run pytest -m integration              # tests d'intégration, avec docker compose
uv run alembic revision --autogenerate -m "…"  # nouvelle migration (relire avant commit)
uv run python scripts/check_niche_terms.py  # aucun terme de niche hors de fixtures/
```

Configuration par variables d'environnement préfixées `PLUME_` (ex. `PLUME_DATABASE_URL`).

## Sources de demande

- **Wikipedia** (`plume_sources.wikipedia`) : pages vues quotidiennes des articles fr.wikipedia
  (`all-access`, agent `user`). `PLUME_WIKIMEDIA_CONTACT` est obligatoire (User-Agent
  « Plume/<version> (<contact>) »). Requêtes en série, réponses en cache pour la journée dans
  `PLUME_DEMAND_CACHE_DIR`. Les titres d'articles viennent de `profile_seed_term.wikipedia_title`.
- **Google Trends** (`plume_sources.trends`) : **expérimental**, extraction non officielle,
  région France, désactivable par `PLUME_TRENDS_ENABLED=false`. Trends donne un **indice relatif
  de 0 à 100**, normalisé par requête (par terme et par fenêtre de 90 jours), **pas un volume** de
  recherches : deux termes ou deux fenêtres ne se comparent pas directement.

Chaque source a un budget de requêtes par passage (`PLUME_*_REQUEST_BUDGET`). Une panne d'une
source passe sa `collection_run` en échec sans toucher aux autres. Les observations sont
append-only : une valeur révisée ajoute une ligne, l'ancienne reste.

## Structure

```
packages/
  core/       modèles, accès base, configuration, client HTTP   (plume_core)
              models.py, schemas.py (contrats JSON), migrations/ (Alembic)
  sources/    clients des sources externes          → core      (plume_sources)
  collector/  planificateur et tâches de collecte   → core, sources
  discovery/  sujets, scores, recommandations       → core
  api/        FastAPI                                → core
tests/unit/         tests unitaires (marqueur posé automatiquement)
tests/integration/  tests PostgreSQL
fixtures/     seul endroit où un terme de niche est autorisé
scripts/      check_niche_terms.py + niche_terms.txt
```

Les dépendances entre paquets sont imposées par import-linter (`[tool.importlinter]` dans `pyproject.toml`).
