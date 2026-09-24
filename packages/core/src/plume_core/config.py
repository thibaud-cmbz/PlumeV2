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

    @property
    def sqlalchemy_url(self) -> str:
        # Sans le suffixe de driver, SQLAlchemy chercherait psycopg2.
        return str(self.database_url).replace("postgresql://", "postgresql+psycopg://", 1)
