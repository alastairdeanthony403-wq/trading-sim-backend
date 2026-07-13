import os
from flask import Blueprint, jsonify, request
from app import db

bp = Blueprint("setup", __name__)


def _authorized():
    provided_key = request.headers.get("X-Setup-Key")
    expected_key = os.environ.get("SETUP_KEY")
    return bool(expected_key) and provided_key == expected_key


@bp.route("/setup/create-tables", methods=["POST"])
def create_tables():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    db.create_all()
    return jsonify({"status": "ok", "message": "tables created"})


@bp.route("/setup/db-stamp-baseline", methods=["POST"])
def db_stamp_baseline():
    """One-time transition to Alembic for a database whose tables already
    exist (built via create-tables + manual ALTERs). Marks the schema as
    already at the baseline revision WITHOUT recreating anything. Run this
    exactly once, right after the Alembic release deploys, before applying
    any further migrations."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    from flask_migrate import stamp
    revision = (request.get_json(silent=True) or {}).get("revision", "head")
    try:
        stamp(revision=revision)
        return jsonify({"status": "ok", "message": f"stamped {revision}"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@bp.route("/setup/db-upgrade", methods=["POST"])
def db_upgrade():
    """Apply all pending Alembic migrations up to head. Safe to call after
    every deploy that ships new migrations (idempotent — a no-op when already
    current)."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    from flask_migrate import upgrade
    try:
        upgrade()
        return jsonify({"status": "ok", "message": "migrated to head"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@bp.route("/setup/migrate-completed-lessons", methods=["POST"])
def migrate_completed_lessons():
    provided_key = request.headers.get("X-Setup-Key")
    expected_key = os.environ.get("SETUP_KEY")
    if not expected_key or provided_key != expected_key:
        return jsonify({"error": "unauthorized"}), 401

    from sqlalchemy import text
    try:
        db.session.execute(text(
            "ALTER TABLE user_progress ADD COLUMN IF NOT EXISTS completed_lessons VARCHAR[] DEFAULT '{}'"
        ))
        db.session.commit()
        return jsonify({"status": "ok", "message": "completed_lessons column added"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500
