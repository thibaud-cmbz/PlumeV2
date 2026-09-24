from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLUME_")

    database_url: PostgresDsn = PostgresDsn("postgresql://plume:plume@localhost:5432/plume")

    @property
    def sqlalchemy_url(self) -> str:
        # Sans le suffixe de driver, SQLAlchemy chercherait psycopg2.
        return str(self.database_url).replace("postgresql://", "postgresql+psycopg://", 1)
