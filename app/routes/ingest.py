import os
import random
import requests
from flask import Blueprint, jsonify, request
from app import db
from app.models.scenario import Scenario, ScenarioBar

bp = Blueprint("ingest", __name__)

ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY")

# A small curated set of tickers across asset feel (stocks only for MVP, free tier friendly)
DEFAULT_SYMBOLS = ["IBM", "AAPL", "MSFT", "TSLA", "AMZN"]


@bp.route("/setup/ingest-scenarios", methods=["POST"])
def ingest_scenarios():
    provided_key = request.headers.get("X-Setup-Key")
    expected_key = os.environ.get("SETUP_KEY")
    if not expected_key or provided_key != expected_key:
        return jsonify({"error": "unauthorized"}), 401

    if not ALPHA_VANTAGE_KEY:
        return jsonify({"error": "ALPHA_VANTAGE_KEY not set"}), 400

    symbols = request.json.get("symbols", DEFAULT_SYMBOLS) if request.is_json else DEFAULT_SYMBOLS
    created = []

    for symbol in symbols:
        resp = requests.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol,
                "outputsize": "compact",
                "apikey": ALPHA_VANTAGE_KEY,
            },
            timeout=30,
        )
        data = resp.json()
        series = data.get("Time Series (Daily)")
        if not series:
            detail = data.get("Note") or data.get("Error Message") or data.get("Information") or str(data)[:300]
            created.append({"symbol": symbol, "status": "failed", "detail": detail})
            continue

        # sort dates ascending
        dates = sorted(series.keys())
        if len(dates) < 100:
            created.append({"symbol": symbol, "status": "skipped", "detail": "not enough bars"})
            continue

        # use the most recent 100 bars (compact tier only returns ~100)
        window_dates = dates[-100:]

        scenario = Scenario(
            name_internal=f"{symbol}_{window_dates[0]}_{window_dates[-1]}",
            asset_class="equity",
            timeframe="1D",
            difficulty_tier=random.choice([1, 2]),
            tags=["historical_daily"],
            is_active=True,
        )
        db.session.add(scenario)
        db.session.flush()  # get scenario.id before inserting bars

        for i, d in enumerate(window_dates):
            bar = series[d]
            db.session.add(ScenarioBar(
                scenario_id=scenario.id,
                bar_sequence=i,
                open=float(bar["1. open"]),
                high=float(bar["2. high"]),
                low=float(bar["3. low"]),
                close=float(bar["4. close"]),
                volume=float(bar["5. volume"]),
            ))

        db.session.commit()
        created.append({"symbol": symbol, "status": "created", "scenario_id": scenario.id, "bars": len(window_dates)})

    return jsonify({"status": "ok", "results": created})
