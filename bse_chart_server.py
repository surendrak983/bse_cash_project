"""
BSE Cash EOD Chart Server
Serves daily, weekly, and monthly charts from stored SQLite EOD data
(Data/bse_cash_eod.db / table bse_cash_eod, produced by bse_cash_project).

Also exposes /api/config  (GET to read, POST to save config.json)
and /api/rerun            (POST to re-run technical_analysis.py for one symbol,
                            if you have one — otherwise that button will just
                            report "not found" and you can ignore it)

Run:  python bse_chart_server.py
Open: http://localhost:5001

Folder layout expected (drop this file into C:\\bajisar\\bse_cash_project):

    bse_cash_project/
        bse_chart_server.py     <- this file
        Data/
            bse_cash_eod.db      <- your existing db (produced by bse_cash.py)
        templates/
            chart_viewer.html    <- the matching HTML (provided alongside this file)
        config.json              <- optional, auto-created with defaults if missing
        technical_analysis.py    <- optional, only needed for the "Re-run" button
"""
import calendar
import json
import subprocess
import sys
import sqlite3
from collections import OrderedDict
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from pathlib import Path

app = Flask(__name__, template_folder='templates')

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
TA_SCRIPT   = BASE_DIR / "technical_analysis.py"
DB_PATH     = BASE_DIR / "Data" / "bse_cash_eod.db"
TABLE_NAME  = "bse_cash_eod"

DEFAULT_CONFIG = {
    "moving_averages": {"ema_periods": [5], "sma_periods": [20, 50, 100, 200]},
    "rsi": {"period": 14},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "bollinger_bands": {"period": 20, "std_dev": 2},
    "atr": {"period": 14},
    "adx": {"period": 14},
    "stochastic": {"k_period": 14, "d_period": 3},
    "supertrend": {"atr_period": 14, "multiplier": 3},
    "vwap": {"window": 20, "window_monthly": 3},
    "volume_profile": {"lookback_bars": 252, "buckets": 24},
}

CONFIG_SNAPSHOT = {
    "ema_periods": DEFAULT_CONFIG["moving_averages"]["ema_periods"],
    "sma_periods": DEFAULT_CONFIG["moving_averages"]["sma_periods"],
}


def ensure_config():
    """Create config.json with sane defaults on first run if it doesn't exist."""
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, 'w') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)


# ── Page ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    symbols = list_symbols_from_storage()
    return render_template('chart_viewer.html', symbols=symbols)


# ── Chart data ────────────────────────────────────────────────────────────────

@app.route('/api/chart/<symbol>')
def get_chart_data(symbol: str):
    db_payload = build_chart_payload(symbol.upper())
    if db_payload:
        return jsonify(db_payload)
    return jsonify({'error': f'{symbol} not found'}), 404


@app.route('/api/symbols')
def list_symbols():
    symbols = list_symbols_from_storage()
    return jsonify({'symbols': symbols, 'count': len(symbols)})


def list_symbols_from_storage():
    symbols = set()
    if DB_PATH.exists():
        with sqlite3.connect(DB_PATH) as conn:
            if table_exists(conn, TABLE_NAME):
                rows = conn.execute(
                    f"SELECT DISTINCT symbol FROM {TABLE_NAME} ORDER BY symbol"
                ).fetchall()
                symbols.update(row[0] for row in rows if row[0])
    return sorted(symbols)


def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def parse_db_date(value: str) -> datetime:
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError(f"Unsupported date format: {value}")


def unix_time(dt: datetime) -> int:
    return calendar.timegm(dt.date().timetuple())


def round_num(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def fetch_daily_bars(symbol: str):
    """
    Pull OHLCV for one symbol from bse_cash_eod.
    NOTE: a small number of symbols (corporate-action relistings, e.g. a couple
    out of ~9900) have more than one fininstrm_id. We group purely by symbol,
    which is fine for charting continuity but means a relisting could show a
    price gap on the exact switchover date — same behavior the NSE version had
    with plain Symbol grouping.
    """
    if not DB_PATH.exists():
        return []

    query = f"""
        SELECT trade_date, open_price, high_price, low_price, close_price, volume
        FROM {TABLE_NAME}
        WHERE UPPER(symbol) = ?
        ORDER BY substr(trade_date, 7, 4), substr(trade_date, 4, 2), substr(trade_date, 1, 2)
    """

    rows = []
    with sqlite3.connect(DB_PATH) as conn:
        if not table_exists(conn, TABLE_NAME):
            return []
        for row in conn.execute(query, (symbol.upper(),)):
            dt = parse_db_date(row[0])
            rows.append(
                {
                    "dt": dt,
                    "time": unix_time(dt),
                    "date": dt.strftime("%Y-%m-%d"),
                    "open": round_num(row[1]),
                    "high": round_num(row[2]),
                    "low": round_num(row[3]),
                    "close": round_num(row[4]),
                    "volume": int(row[5] or 0),
                    # BSE bhavcopy in this db has no delivery data; keep the keys
                    # present (as None) so the frontend's delivery panel just
                    # renders empty instead of erroring.
                    "delivery_qty": None,
                    "delivery_pct": None,
                }
            )
    return rows


def aggregate_bars(daily_bars, timeframe: str):
    grouped = OrderedDict()
    for bar in daily_bars:
        dt = bar["dt"]
        if timeframe == "weekly":
            year, week, _ = dt.isocalendar()
            bucket_key = (year, week)
            bucket_dt = datetime.strptime(f"{year} {week} 1", "%G %V %u")
        elif timeframe == "monthly":
            bucket_key = (dt.year, dt.month)
            bucket_dt = datetime(dt.year, dt.month, 1)
        else:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        bucket = grouped.setdefault(
            bucket_key,
            {
                "dt": bucket_dt,
                "time": unix_time(bucket_dt),
                "date": bucket_dt.strftime("%Y-%m-%d"),
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": 0,
                "delivery_qty": 0,
                "_has_delivery": False,
            },
        )
        bucket["high"] = max(bucket["high"], bar["high"])
        bucket["low"] = min(bucket["low"], bar["low"])
        bucket["close"] = bar["close"]
        bucket["volume"] += bar["volume"] or 0
        if bar["delivery_qty"] is not None:
            bucket["delivery_qty"] += bar["delivery_qty"]
            bucket["_has_delivery"] = True

    bars = []
    for bucket in grouped.values():
        delivery_qty = bucket["delivery_qty"] if bucket["_has_delivery"] else None
        delivery_pct = None
        if delivery_qty is not None and bucket["volume"]:
            delivery_pct = round_num(delivery_qty / bucket["volume"] * 100)
        bars.append(
            {
                "dt": bucket["dt"],
                "time": bucket["time"],
                "date": bucket["date"],
                "open": round_num(bucket["open"]),
                "high": round_num(bucket["high"]),
                "low": round_num(bucket["low"]),
                "close": round_num(bucket["close"]),
                "volume": bucket["volume"],
                "delivery_qty": delivery_qty,
                "delivery_pct": delivery_pct,
            }
        )
    return bars


def add_chart_fields(bars):
    closes = []
    ema_values = {}
    for index, bar in enumerate(bars):
        closes.append(bar["close"])

        for period in CONFIG_SNAPSHOT["sma_periods"]:
            key = f"sma{period}"
            if len(closes) >= period:
                bar[key] = round_num(sum(closes[-period:]) / period)
            else:
                bar[key] = None

        for period in CONFIG_SNAPSHOT["ema_periods"]:
            key = f"ema{period}"
            alpha = 2 / (period + 1)
            previous = ema_values.get(period)
            ema = bar["close"] if previous is None else (bar["close"] * alpha) + (previous * (1 - alpha))
            ema_values[period] = ema
            bar[key] = round_num(ema)

        bar["vwap"] = None
        if index >= 19:
            window = bars[index - 19 : index + 1]
            volume = sum(item["volume"] or 0 for item in window)
            if volume:
                bar["vwap"] = round_num(
                    sum(item["close"] * (item["volume"] or 0) for item in window) / volume
                )

        bar["bb_middle"] = bar.get("sma20")
        if index >= 19:
            window = closes[-20:]
            mean = sum(window) / 20
            variance = sum((price - mean) ** 2 for price in window) / 20
            std = variance ** 0.5
            bar["bb_upper"] = round_num(mean + 2 * std)
            bar["bb_lower"] = round_num(mean - 2 * std)
        else:
            bar["bb_upper"] = None
            bar["bb_lower"] = None

        volume_window = bars[max(0, index - 19) : index + 1]
        avg_volume = sum(item["volume"] or 0 for item in volume_window) / len(volume_window)
        bar["volume_signal"] = 1 if avg_volume and bar["volume"] > avg_volume * 1.5 else 0

        for key in (
            "rsi", "macd", "macd_signal", "macd_hist", "stoch_k", "stoch_d",
            "atr", "datr", "adx", "plus_di", "minus_di", "supertrend",
            "fvg_bull_top", "fvg_bull_bottom", "fvg_bear_top", "fvg_bear_bottom",
            "pivot_p", "pivot_r1", "pivot_r2", "pivot_r3", "pivot_s1",
            "pivot_s2", "pivot_s3",
        ):
            bar[key] = None
        bar["supertrend_dir"] = 1
        bar["obv"] = None

    return [{key: value for key, value in bar.items() if key != "dt"} for bar in bars]


def volume_profile(bars, buckets=24, lookback=252):
    sample = bars[-lookback:] if len(bars) > lookback else bars
    if not sample:
        return []

    low = min(bar["low"] for bar in sample)
    high = max(bar["high"] for bar in sample)
    if high <= low:
        return []

    step = (high - low) / buckets
    profile = [{"price": low + (i + 0.5) * step, "vol": 0} for i in range(buckets)]
    for bar in sample:
        start = max(0, int((bar["low"] - low) / step))
        end = min(buckets - 1, int((bar["high"] - low) / step))
        slots = max(1, end - start + 1)
        share = (bar["volume"] or 0) / slots
        for index in range(start, end + 1):
            profile[index]["vol"] += share

    max_vol = max(item["vol"] for item in profile) or 1
    return [
        {
            "price": round_num(item["price"]),
            "vol": int(item["vol"]),
            "pct": round_num(item["vol"] / max_vol * 100, 1),
        }
        for item in profile
    ]


def make_timeframe_payload(bars):
    prepared = add_chart_fields([bar.copy() for bar in bars])
    return {
        "bars": prepared,
        "count": len(prepared),
        "last_date": prepared[-1]["date"] if prepared else None,
        "vol_profile": volume_profile(bars),
    }


def build_chart_payload(symbol: str):
    daily = fetch_daily_bars(symbol)
    if not daily:
        return None

    weekly = aggregate_bars(daily, "weekly")
    monthly = aggregate_bars(daily, "monthly")
    return {
        "symbol": symbol,
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "source": str(DB_PATH),
        "config_snapshot": CONFIG_SNAPSHOT,
        "timeframes": {
            "daily": make_timeframe_payload(daily),
            "weekly": make_timeframe_payload(weekly),
            "monthly": make_timeframe_payload(monthly),
        },
    }


# ── Config ────────────────────────────────────────────────────────────────────

@app.route('/api/config', methods=['GET'])
def get_config():
    ensure_config()
    with open(CONFIG_PATH) as f:
        return jsonify(json.load(f))


@app.route('/api/config', methods=['POST'])
def save_config():
    """Save posted JSON to config.json after basic validation."""
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'Payload must be a JSON object'}), 400

        # Strip _comment / _help keys before saving (they're read-only annotations)
        def strip_help(obj):
            if isinstance(obj, dict):
                return {k: strip_help(v) for k, v in obj.items() if not k.startswith('_')}
            return obj

        clean = strip_help(data)

        # Basic type checks
        ma = clean.get('moving_averages', {})
        for key in ('ema_periods', 'sma_periods'):
            val = ma.get(key, [])
            if not isinstance(val, list) or not all(isinstance(x, int) and x > 0 for x in val):
                return jsonify({'error': f'moving_averages.{key} must be a list of positive integers'}), 400

        numeric_checks = [
            ('rsi.period',            lambda v: isinstance(v, int) and 2 <= v <= 100),
            ('macd.fast',             lambda v: isinstance(v, int) and v > 0),
            ('macd.slow',             lambda v: isinstance(v, int) and v > 0),
            ('macd.signal',           lambda v: isinstance(v, int) and v > 0),
            ('bollinger_bands.period',lambda v: isinstance(v, int) and v >= 2),
            ('bollinger_bands.std_dev',lambda v: isinstance(v, (int,float)) and v > 0),
            ('atr.period',            lambda v: isinstance(v, int) and v >= 1),
            ('adx.period',            lambda v: isinstance(v, int) and v >= 1),
            ('stochastic.k_period',   lambda v: isinstance(v, int) and v >= 1),
            ('stochastic.d_period',   lambda v: isinstance(v, int) and v >= 1),
            ('supertrend.atr_period', lambda v: isinstance(v, int) and v >= 1),
            ('supertrend.multiplier', lambda v: isinstance(v, (int,float)) and v > 0),
            ('vwap.window',           lambda v: isinstance(v, int) and v >= 1),
            ('vwap.window_monthly',   lambda v: isinstance(v, int) and v >= 1),
            ('volume_profile.lookback_bars', lambda v: isinstance(v, int) and v >= 10),
            ('volume_profile.buckets',       lambda v: isinstance(v, int) and 4 <= v <= 100),
        ]
        for dotpath, check in numeric_checks:
            section, key = dotpath.split('.')
            val = clean.get(section, {}).get(key)
            if val is None or not check(val):
                return jsonify({'error': f'Invalid value for {dotpath}: {val}'}), 400

        # MACD fast must be < slow
        if clean['macd']['fast'] >= clean['macd']['slow']:
            return jsonify({'error': 'macd.fast must be less than macd.slow'}), 400

        with open(CONFIG_PATH, 'w') as f:
            json.dump(clean, f, indent=2)

        # Reflect the saved MA periods immediately for new chart requests
        global CONFIG_SNAPSHOT
        CONFIG_SNAPSHOT = {
            "ema_periods": clean["moving_averages"]["ema_periods"],
            "sma_periods": clean["moving_averages"]["sma_periods"],
        }
        return jsonify({'ok': True, 'message': 'config.json saved'})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Re-run ────────────────────────────────────────────────────────────────────

@app.route('/api/rerun/<symbol>', methods=['POST'])
def rerun_symbol(symbol: str):
    """Trigger technical_analysis.py for one symbol, if you have such a script.
    This is optional — the chart works fine from Data\bse_cash_eod.db without it.
    """
    if not TA_SCRIPT.exists():
        return jsonify({'error': 'technical_analysis.py not found (optional — chart data still comes straight from Data\\bse_cash_eod.db)'}), 404
    try:
        result = subprocess.run(
            [sys.executable, str(TA_SCRIPT), '--symbol', symbol.upper()],
            capture_output=True, text=True, timeout=120
        )
        return jsonify({
            'ok':     result.returncode == 0,
            'stdout': result.stdout,
            'stderr': result.stderr,
        })
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Timed out after 120 s'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Boot ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    ensure_config()
    symbol_count = len(list_symbols_from_storage()) if DB_PATH.exists() else 0
    print(f"DB         : {DB_PATH}  ({'found, ' + str(symbol_count) + ' symbols' if DB_PATH.exists() else 'MISSING'})")
    print(f"Config     : {CONFIG_PATH}  ({'found' if CONFIG_PATH.exists() else 'MISSING'})")
    print("Open       : http://localhost:5001")
    app.run(debug=False, host='0.0.0.0', port=5001)
