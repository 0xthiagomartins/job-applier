"""Add adaptive Easy Apply memory table."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260603_000007"
down_revision = "20260527_000006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apply_action_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(length=128), nullable=False),
        sa.Column("signature_hash", sa.String(length=64), nullable=False),
        sa.Column("signature_json", sa.JSON(), nullable=False),
        sa.Column("strategy_payload_json", sa.JSON(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_apply_action_memories")),
    )
    op.create_index(
        op.f("ix_apply_action_memories_task_type"),
        "apply_action_memories",
        ["task_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_apply_action_memories_expires_at"),
        "apply_action_memories",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ux_apply_action_memories_task_signature",
        "apply_action_memories",
        ["task_type", "signature_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_apply_action_memories_task_signature", table_name="apply_action_memories")
    op.drop_index(op.f("ix_apply_action_memories_expires_at"), table_name="apply_action_memories")
    op.drop_index(op.f("ix_apply_action_memories_task_type"), table_name="apply_action_memories")
    op.drop_table("apply_action_memories")
