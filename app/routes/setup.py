import os
from flask import Blueprint, jsonify, request
from app import db

bp = Blueprint("setup", __name__)

@bp.route("/setup/create-tables", methods=["POST"])
def create_tables():
    provided_key = request.headers.get("X-Setup-Key")
    expected_key = os.environ.get("SETUP_KEY")
    if not expected_key or provided_key != expected_key:
        return jsonify({"error": "unauthorized"}), 401

    db.create_all()
    return jsonify({"status": "ok", "message": "tables created"})
