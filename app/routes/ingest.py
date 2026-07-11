import os
import random
import time
import requests
from flask import Blueprint, jsonify, request
from app import db
from app.models.scenario import Scenario, ScenarioBar

bp = Blueprint("ingest", __name__)

ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY")

DEFAULT_SYMBOLS = ["IBM", "AAPL", "MSFT", "TSLA", "AMZN"]

# maps our internal timeframe label -> Alpha Vantage request config
TIMEFRAME_CONFIGS = {
    "1D": {
        "function": "TIME_SERIES_DAILY",
        "series_key": "Time Series (Daily)",
        "extra_params": {"outputsize": "compact"},
    },
    "1W": {
        "function": "TIME_SERIES_WEEKLY",
        "series_key": "Weekly Time Series",
        "extra_params": {},
    },
    "60min": {
        "function": "TIME_SERIES_INTRADAY",
        "series_key": "Time Series (60min)",
        "extra_params": {"interval": "60min", "outputsize": "compact"},
    },
    "15min": {
        "function": "TIME_SERIES_INTRADAY",
        "series_key": "Time Series (15min)",
        "extra_params": {"interval": "15min", "outputsize": "compact"},
    },
}

DEFAULT_TIMEFRAMES = ["1D"]


@bp.route("/setup/ingest-scenarios", methods=["POST"])
def ingest_scenarios():
    provided_key = request.headers.get("X-Setup-Key")
    expected_key = os.environ.get("SETUP_KEY")
    if not expected_key or provided_key != expected_key:
        return jsonify({"error": "unauthorized"}), 401

    if not ALPHA_VANTAGE_KEY:
        return jsonify({"error": "ALPHA_VANTAGE_KEY not set"}), 400

    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols", DEFAULT_SYMBOLS)
    timeframes = body.get("timeframes", DEFAULT_TIMEFRAMES)

    created = []

    for timeframe in timeframes:
        config = TIMEFRAME_CONFIGS.get(timeframe)
        if not config:
            created.append({"timeframe": timeframe, "status": "failed", "detail": "unknown timeframe"})
            continue

        for symbol in symbols:
            time.sleep(13)  # free tier: ~5 calls/minute limit

            params = {
                "function": config["function"],
                "symbol": symbol,
                "apikey": ALPHA_VANTAGE_KEY,
                **config["extra_params"],
            }
            resp = requests.get("https://www.alphavantage.co/query", params=params, timeout=30)
            data = resp.json()
            series = data.get(config["series_key"])

            if not series:
                detail = data.get("Note") or data.get("Error Message") or data.get("Information") or str(data)[:300]
                created.append({"symbol": symbol, "timeframe": timeframe, "status": "failed", "detail": detail})
                continue

            dates = sorted(series.keys())
            if len(dates) < 100:
                created.append({"symbol": symbol, "timeframe": timeframe, "status": "skipped", "detail": "not enough bars"})
                continue

            window_dates = dates[-100:]

            scenario = Scenario(
                name_internal=f"{symbol}_{timeframe}_{window_dates[0]}_{window_dates[-1]}",
                asset_class="equity",
                timeframe=timeframe,
                difficulty_tier=random.choice([1, 2]),
                tags=["historical"],
                is_active=True,
            )
            db.session.add(scenario)
            db.session.flush()

            for i, d in enumerate(window_dates):
                bar = series[d]
                db.session.add(ScenarioBar(
                    scenario_id=scenario.id,
                    bar_sequence=i,
                    open=float(bar["1. open"]),
                    high=float(bar["2. high"]),
                    low=float(bar["3. low"]),
                    close=float(bar["4. close"]),
                    volume=float(bar.get("5. volume", 0)),
                ))

            db.session.commit()
            created.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "status": "created",
                "scenario_id": scenario.id,
                "bars": len(window_dates),
            })

    return jsonify({"status": "ok", "results": created})
