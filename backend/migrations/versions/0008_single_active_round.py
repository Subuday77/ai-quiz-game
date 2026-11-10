"""Enforce at most one active game per account (PostgreSQL partial index).

Revision ID: 0008_single_active_round
Revises: 0007_bonus_wager

Historic versions could leave multiple 'main'/'bonus' games for one account.
Preserve the most recent game, and archive ONLY old duplicate runs without
purchased helpers or charged star perks. No rows, attempts or history are
deleted. Abort rather than silently forfeit a previously paid benefit.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_single_active_round"
down_revision = "0007_bonus_wager"
branch_labels = None
depends_on = None

INDEX = "ux_game_runs_one_active_per_user"


def upgrade():
    conn = op.get_bind()
    duplicated_users = conn.execute(sa.text("""
        SELECT user_id FROM game_runs
        WHERE status IN ('main', 'bonus')
        GROUP BY user_id HAVING count(*) > 1
    """)).scalars().all()
    for user_id in duplicated_users:
        games = conn.execute(sa.text("""
            SELECT id FROM game_runs
            WHERE user_id = :user_id AND status IN ('main', 'bonus')
            ORDER BY started_at DESC, id DESC FOR UPDATE
        """), {"user_id": user_id}).scalars().all()
        # Keep the same most-recent game that GET /active selected before the
        # migration. Evaluate ALL duplicates before touching any one of them.
        for old_id in games[1:]:
            purchase = conn.execute(sa.text("""
                SELECT 1 FROM platinum_helper_purchases
                WHERE run_id = :run_id LIMIT 1
            """), {"run_id": old_id}).first()
            charge = conn.execute(sa.text("""
                SELECT 1 FROM wallet_transactions
                WHERE run_id = :run_id AND amount < 0 AND reason LIKE 'star/%'
                LIMIT 1
            """), {"run_id": old_id}).first()
            if purchase or charge:
                raise RuntimeError(
                    f"Cannot auto-archive paid duplicate game {old_id}. "
                    "Its purchased perks must be reviewed before upgrading "
                    "to 0008; no changes from this migration were committed."
                )
        for old_id in games[1:]:
            conn.execute(sa.text("""
                UPDATE game_runs SET status = 'abandoned', finished_at = CURRENT_TIMESTAMP
                WHERE id = :run_id AND status IN ('main', 'bonus')
            """), {"run_id": old_id})

    op.create_index(
        INDEX, "game_runs", ["user_id"], unique=True,
        postgresql_where=sa.text("status IN ('main', 'bonus')"),
    )


def downgrade():
    op.drop_index(INDEX, table_name="game_runs")
