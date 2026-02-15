"""Change skills column from Text to JSON type.

Revision ID: 006
Revises: 005
Create Date: 2026-02-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Change skills column from Text to JSON
    # Since PostgreSQL can handle this conversion automatically for JSON strings
    op.alter_column(
        "agents",
        "skills", 
        existing_type=sa.Text(),
        type_=sa.JSON(),
        existing_nullable=True,
        postgresql_using="skills::json"
    )


def downgrade() -> None:
    # Change skills column back from JSON to Text
    op.alter_column(
        "agents",
        "skills",
        existing_type=sa.JSON(),
        type_=sa.Text(),
        existing_nullable=True,
        postgresql_using="skills::text"
    )