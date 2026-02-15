"""Add database indexes and agent name uniqueness constraint.

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
    # Add indexes for performance (only ones that don't already exist)
    # Note: Most single-column indexes already exist from model definitions
    op.create_index("ix_agents_owner_id_created_at", "agents", ["owner_id", "created_at"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    
    # Add unique constraint for agent names per owner
    op.create_unique_constraint(
        "uq_agents_owner_id_name", 
        "agents", 
        ["owner_id", "name"]
    )


def downgrade() -> None:
    # Drop unique constraint
    op.drop_constraint("uq_agents_owner_id_name", "agents", type_="unique")
    
    # Drop indexes (only ones we created)
    op.drop_index("ix_sessions_expires_at", "sessions")
    op.drop_index("ix_agents_owner_id_created_at", "agents")