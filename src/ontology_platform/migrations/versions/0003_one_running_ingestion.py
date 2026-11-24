"""at most one running ingestion per connector

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Close duplicates left by the earlier check-then-insert race before adding the unique index.
    # Keeps the most recently started run per connector (window functions: PostgreSQL, SQLite >= 3.25).
    op.execute(
        "UPDATE ingestion_runs SET status = 'failed', error = 'superseded duplicate running run' "
        "WHERE status = 'running' AND id NOT IN ("
        "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY tenant_id, connector_id ORDER BY started_at DESC, id DESC) AS rn "
        "FROM ingestion_runs WHERE status = 'running') AS ranked WHERE rn = 1)"
    )
    op.create_index(
        "uq_run_one_running", "ingestion_runs", ["tenant_id", "connector_id"], unique=True,
        sqlite_where=sa.text("status = 'running'"), postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_run_one_running", table_name="ingestion_runs")
