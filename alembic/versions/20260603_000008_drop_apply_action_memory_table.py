"""Drop the obsolete SQLite table for adaptive apply memory."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260603_000008"
down_revision = "20260603_000007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ux_apply_action_memories_task_signature", table_name="apply_action_memories")
    op.drop_index(op.f("ix_apply_action_memories_expires_at"), table_name="apply_action_memories")
    op.drop_index(op.f("ix_apply_action_memories_task_type"), table_name="apply_action_memories")
    op.drop_table("apply_action_memories")


def downgrade() -> None:
    msg = "Downgrade is not supported for the removed apply_action_memories table."
    raise NotImplementedError(msg)
