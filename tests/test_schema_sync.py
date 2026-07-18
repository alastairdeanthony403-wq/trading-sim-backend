"""Startup schema self-heal test.

Simulates a database whose schema is behind the models (a deploy where the
migration step was missed) and verifies reconcile_schema() repairs it so
user-keyed endpoints stop 500ing.

    DATABASE_URL=postgresql://.../trading_sim_dev python tests/test_schema_sync.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text, inspect
from app import create_app, db
from app.schema_sync import reconcile_schema


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        raise AssertionError(name)


def test_reconcile_readds_missing_columns():
    app = create_app()
    with app.app_context():
        db.create_all()
        # simulate a stale prod schema: drop columns the models expect
        db.session.execute(text(
            "ALTER TABLE user_progress DROP COLUMN IF EXISTS total_trades_all"))
        db.session.execute(text(
            "ALTER TABLE sessions DROP COLUMN IF EXISTS is_contest"))
        db.session.commit()

    # a user-keyed endpoint 500s while the column is missing
    client = app.test_client()
    check("stale schema makes /career 500", client.get("/career/heal_u").status_code == 500)

    # self-heal
    reconcile_schema(app)

    check("/career recovers after reconcile", client.get("/career/heal_u").status_code == 200)
    check("/progress recovers after reconcile", client.get("/progress/heal_u").status_code == 200)

    with app.app_context():
        cols = {c["name"] for c in inspect(db.engine).get_columns("user_progress")}
        check("dropped column is restored", "total_trades_all" in cols)
        scols = {c["name"] for c in inspect(db.engine).get_columns("sessions")}
        check("session column is restored", "is_contest" in scols)


def test_reconcile_is_idempotent():
    app = create_app()
    reconcile_schema(app)   # nothing missing → no-op, no error
    reconcile_schema(app)
    check("running reconcile twice is a no-op", True)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        print(t.__name__)
        try:
            t()
        except AssertionError:
            failed += 1
        except Exception as e:
            print(f"  ERROR {e}"); failed += 1
    print(f"\n{'ALL PASSED' if failed == 0 else str(failed) + ' FAILED'} ({len(TESTS)} tests)")
    sys.exit(1 if failed else 0)
