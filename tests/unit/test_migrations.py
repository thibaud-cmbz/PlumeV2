from pathlib import Path

import plume_core

VERSIONS = Path(plume_core.__file__).parent / "migrations" / "versions"


def test_migrations_declare_no_postgres_enum() -> None:
    migrations = sorted(VERSIONS.glob("*.py"))
    assert migrations
    for migration in migrations:
        assert "enum" not in migration.read_text(encoding="utf-8").lower(), migration.name
