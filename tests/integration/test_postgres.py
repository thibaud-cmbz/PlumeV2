import psycopg
from plume_core.config import Settings


def test_vector_extension_is_available() -> None:
    with psycopg.connect(str(Settings().database_url)) as connection:
        available = connection.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
        ).fetchone()
        assert available is not None

        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        installed = connection.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        assert installed is not None
