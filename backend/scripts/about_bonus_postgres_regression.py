"""PostgreSQL-only concurrency regression for the one-time About reward."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from persistence.about_bonus import claim_about_bonus, about_bonus_claimed
from persistence.database import engine
from persistence.models import User, WalletTransaction

name = "about-regression-" + uuid4().hex[:12]
with Session(engine()) as db:
    player = User(username="BonusTest", username_key=name)
    db.add(player)
    db.commit()
    player_id = player.id


def run():
    with Session(engine()) as db:
        return claim_about_bonus(db, player_id)


with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(lambda _: run(), range(4)))

assert sum(newly_claimed for newly_claimed, _ in results) == 1, results
expected = {"bronze": 50, "silver": 40, "gold": 30, "platinum": 20}
assert all(balances == expected for _, balances in results), results
with Session(engine()) as db:
    assert about_bonus_claimed(db, player_id)
    rows = db.scalars(select(WalletTransaction).where(
        WalletTransaction.user_id == player_id,
    )).all()
    assert len(rows) == 4, rows
    assert sum(tx.amount for tx in rows) == 140, rows
print("About reward: four concurrent requests, one atomic award")
