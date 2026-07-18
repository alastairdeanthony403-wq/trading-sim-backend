import os
from dotenv import load_dotenv
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_cors import CORS

load_dotenv()

db = SQLAlchemy()
migrate = Migrate()

def create_app():
    app = Flask(__name__)
    CORS(app)
    db_url = os.environ.get("DATABASE_URL", "postgresql://localhost/trading_sim_dev")
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    # Import models before Migrate so Alembic autogenerate sees every table.
    from app.models import scenario, session, progress, mission, event, competition  # noqa: F401
    migrate.init_app(app, db)

    from app.routes import health, setup, ingest, game, progress, missions, contests
    app.register_blueprint(health.bp)
    app.register_blueprint(setup.bp)
    app.register_blueprint(ingest.bp)
    app.register_blueprint(game.bp)
    app.register_blueprint(progress.bp)
    app.register_blueprint(missions.bp)
    app.register_blueprint(contests.bp)

    # Self-heal: make the DB schema match the models on boot (creates missing
    # tables, adds missing columns) so a deploy can't leave endpoints 500ing on
    # a stale schema. Idempotent and safe to run on every worker.
    if os.environ.get("SCHEMA_SYNC", "on") != "off":
        from app.schema_sync import reconcile_schema
        reconcile_schema(app)

    return app
