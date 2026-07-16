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
DEFAULT_FOREX_SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY"]
DEFAULT_CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]

# maps our internal timeframe label -> Alpha Vantage request config (equities)
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


def _split_pair(symbol, default_quote="USD"):
    """"EUR/USD" -> ("EUR", "USD"); "BTC" -> ("BTC", default_quote)."""
    parts = symbol.replace("-", "/").upper().split("/")
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], default_quote


def _bar_ohlcv(bar):
    """Pull OHLCV out of an Alpha Vantage bar dict, tolerant of the differing
    field-naming schemes across the equities / forex / crypto endpoints
    (crypto historically prefixed keys like '1a. open (USD)')."""
    def pick(*keys):
        for k in keys:
            if k in bar:
                return float(bar[k])
        return None
    o = pick("1. open", "1a. open (USD)")
    h = pick("2. high", "2a. high (USD)")
    l = pick("3. low", "3a. low (USD)")
    c = pick("4. close", "4a. close (USD)")
    v = pick("5. volume", "6. volume", "5. volume (USD)")
    return o, h, l, (c if c is not None else o), (v if v is not None else 0.0)


def _market_request(market, symbol, timeframe):
    """Build (params, series_key, asset_class, label) for a symbol in a given
    market. Forex/crypto are daily only; equities honour the timeframe.
    Returns None when the market/timeframe combination is unsupported."""
    if market in ("forex", "fx"):
        base, quote = _split_pair(symbol)
        return (
            {"function": "FX_DAILY", "from_symbol": base, "to_symbol": quote,
             "outputsize": "full"},
            "Time Series FX (Daily)", "forex", f"{base}{quote}_1D",
        )
    if market in ("crypto", "digital"):
        base, quote = _split_pair(symbol, default_quote="USD")
        return (
            {"function": "DIGITAL_CURRENCY_DAILY", "symbol": base, "market": quote},
            "Time Series (Digital Currency Daily)", "crypto", f"{base}{quote}_1D",
        )
    # equities / stocks (default)
    config = TIMEFRAME_CONFIGS.get(timeframe)
    if not config:
        return None
    return (
        {"function": config["function"], "symbol": symbol, **config["extra_params"]},
        config["series_key"], "equity", f"{symbol}_{timeframe}",
    )


@bp.route("/setup/ingest-scenarios", methods=["POST"])
def ingest_scenarios():
    provided_key = request.headers.get("X-Setup-Key")
    expected_key = os.environ.get("SETUP_KEY")
    if not expected_key or provided_key != expected_key:
        return jsonify({"error": "unauthorized"}), 401

    if not ALPHA_VANTAGE_KEY:
        return jsonify({"error": "ALPHA_VANTAGE_KEY not set"}), 400

    body = request.get_json(silent=True) or {}
    # market: "equity" (default) | "forex" | "crypto". Symbols default per market.
    market = (body.get("market") or "equity").lower()
    if market in ("forex", "fx"):
        symbols = body.get("symbols", DEFAULT_FOREX_SYMBOLS)
        timeframes = ["1D"]
    elif market in ("crypto", "digital"):
        symbols = body.get("symbols", DEFAULT_CRYPTO_SYMBOLS)
        timeframes = ["1D"]
    else:
        symbols = body.get("symbols", DEFAULT_SYMBOLS)
        timeframes = body.get("timeframes", DEFAULT_TIMEFRAMES)

    created = []

    for timeframe in timeframes:
        for symbol in symbols:
            time.sleep(13)  # free tier: ~5 calls/minute limit

            req = _market_request(market, symbol, timeframe)
            if req is None:
                created.append({"symbol": symbol, "timeframe": timeframe,
                                "status": "failed", "detail": "unknown market/timeframe"})
                continue
            extra_params, series_key, asset_class, label = req

            params = {"apikey": ALPHA_VANTAGE_KEY, **extra_params}
            resp = requests.get("https://www.alphavantage.co/query", params=params, timeout=30)
            data = resp.json()
            series = data.get(series_key)

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
                name_internal=f"{label}_{window_dates[0]}_{window_dates[-1]}",
                asset_class=asset_class,
                timeframe=timeframe,
                difficulty_tier=random.choice([1, 2]),
                tags=["historical", asset_class],
                is_active=True,
            )
            db.session.add(scenario)
            db.session.flush()

            for i, d in enumerate(window_dates):
                o, h, l, c, v = _bar_ohlcv(series[d])
                if o is None or h is None or l is None:
                    continue
                db.session.add(ScenarioBar(
                    scenario_id=scenario.id,
                    bar_sequence=i,
                    open=o, high=h, low=l, close=c, volume=v,
                ))

            db.session.commit()
            created.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "asset_class": asset_class,
                "status": "created",
                "scenario_id": scenario.id,
                "bars": len(window_dates),
            })

    return jsonify({"status": "ok", "results": created})
