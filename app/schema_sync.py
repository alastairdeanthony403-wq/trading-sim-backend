"""Idempotent schema reconciliation (run at startup).

The app has always used a hybrid schema strategy: create_all() for new tables and
Alembic for column changes. In practice that means a deploy can ship models that
are ahead of the database if the migration step is missed — every user-keyed
endpoint then 500s with "column ... does not exist".

reconcile_schema() closes that gap safely: it creates any missing tables and adds
any missing columns with `ADD COLUMN IF NOT EXISTS`. New columns are added
NULLABLE with no server default — that DDL never fails on a populated table and
is concurrency-safe across gunicorn workers. The application code already sets
these columns on write and tolerates NULLs on read, so nullable backfill is
functionally correct. Alembic migrations remain the canonical history for a
clean database; this is the safety net for existing ones.
"""
from sqlalchemy import inspect, text
from app import db


def reconcile_schema(app):
    with app.app_context():
        try:
            db.create_all()   # create any missing tables (idempotent)
            insp = inspect(db.engine)
            existing = set(insp.get_table_names())
            added = []
            for table in db.metadata.sorted_tables:
                if table.name not in existing:
                    continue   # create_all just built it — fully current
                have = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name in have:
                        continue
                    col_type = col.type.compile(dialect=db.engine.dialect)
                    db.session.execute(text(
                        f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN IF NOT EXISTS "{col.name}" {col_type}'
                    ))
                    added.append(f"{table.name}.{col.name}")
            db.session.commit()
            if added:
                app.logger.info("schema_sync added columns: %s", ", ".join(added))
        except Exception as e:   # never let a reconcile failure block boot
            db.session.rollback()
            app.logger.warning("schema_sync skipped: %s", e)
