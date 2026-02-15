"""Add database indexes for performance.

Revision ID: 005
Revises: 004
Create Date: 2026-02-15
"""

from __future__ import annotations

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_agents_owner_id_created_at", "agents", ["owner_id", "created_at"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", "sessions")
    op.drop_index("ix_agents_owner_id_created_at", "agents")