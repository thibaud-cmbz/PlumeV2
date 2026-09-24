from pathlib import Path

from pydantic import PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLUME_")

    database_url: PostgresDsn = PostgresDsn("postgresql://plume:plume@localhost:5432/plume")
    youtube_api_key: SecretStr | None = None
    # 30 jours par défaut ; 1095 (36 mois) si l'audit YouTube est accepté. Les textes restent à 30.
    youtube_retention_days: int = 30
    # Part des 10 000 unités quotidiennes que Plume s'autorise.
    youtube_daily_quota_budget: int = 7000
    # Wikimedia exige un User-Agent identifiant un contact : « Plume/<version> (<contact>) ».
    wikimedia_contact: str | None = None
    wikipedia_request_budget: int = 500
    # EXPÉRIMENTAL : extraction non officielle de Google Trends, désactivable ici.
    trends_enabled: bool = True
    trends_request_budget: int = 50
    demand_cache_dir: Path = Path(".cache/plume")

    @property
    def sqlalchemy_url(self) -> str:
        # Sans le suffixe de driver, SQLAlchemy chercherait psycopg2.
        return str(self.database_url).replace("postgresql://", "postgresql+psycopg://", 1)
