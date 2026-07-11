from flask import Blueprint, jsonify
from app import db
from sqlalchemy import text

bp = Blueprint("health", __name__)

@bp.route("/health")
def health():
    return jsonify({"status": "ok"})

@bp.route("/health/db")
def health_db():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "db": "connected"})
    except Exception as e:
        return jsonify({"status": "error", "db": "failed", "detail": str(e)}), 500
