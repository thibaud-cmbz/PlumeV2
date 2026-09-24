"""Titre Wikipedia optionnel sur les termes de profil.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("profile_seed_term", sa.Column("wikipedia_title", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("profile_seed_term", "wikipedia_title")
