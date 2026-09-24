from alembic import context
from plume_core.config import Settings
from plume_core.models import Base
from sqlalchemy import create_engine


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    # Les partitions mensuelles sont créées en SQL : l'autogenerate ne doit pas les supprimer.
    return not (type_ == "table" and name is not None and name.startswith("video_observation_p"))


engine = create_engine(Settings().sqlalchemy_url)
with engine.connect() as connection:
    context.configure(
        connection=connection, target_metadata=Base.metadata, include_name=include_name
    )
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
