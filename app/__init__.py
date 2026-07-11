import os
from dotenv import load_dotenv
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

db = SQLAlchemy()

def create_app():
    app = Flask(__name__)
    db_url = os.environ.get("DATABASE_URL", "postgresql://localhost/trading_sim_dev")
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    from app.models import scenario, session, progress

    from app.routes import health, setup, ingest
    app.register_blueprint(health.bp)
    app.register_blueprint(setup.bp)
    app.register_blueprint(ingest.bp)

    return app
