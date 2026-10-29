"""Durable feedback submissions mirrored to a project board.

Revision ID: feedback_mirror_20261029
Revises: public_cutover_20260912
"""

from alembic import op
import sqlalchemy as sa

revision = "feedback_mirror_20261029"
down_revision = "public_cutover_20260912"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "feedback_submissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("event_id", sa.String(36), unique=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("category", sa.String(16), nullable=False, server_default="bug"),
        sa.Column("page", sa.String(500)),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("trace_id", sa.String(32)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("ticket_id", sa.String(64)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending','sent','failed')", name="ck_feedback_status"
        ),
    )
    op.create_index(
        "ix_feedback_submissions_pending",
        "feedback_submissions",
        ["status", "next_attempt_at"],
    )
    # Rescue feedback already sitting in telemetry before retention deletes it.
    op.execute(
        """
        INSERT INTO feedback_submissions
            (id, created_at, event_id, user_id, category, page, message, trace_id, status, attempts)
        SELECT
            gen_random_uuid()::text,
            created_at,
            id,
            user_id,
            COALESCE(attributes->>'category', 'bug'),
            LEFT(attributes->>'page', 500),
            message,
            trace_id,
            'pending',
            0
        FROM telemetry_events
        WHERE kind = 'feedback' AND message IS NOT NULL
        ON CONFLICT (event_id) DO NOTHING
        """
    )


def downgrade():
    op.drop_index("ix_feedback_submissions_pending", table_name="feedback_submissions")
    op.drop_table("feedback_submissions")
