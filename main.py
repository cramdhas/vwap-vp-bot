"""
XAUUSD Multi-Strategy Telegram Bot
Updated with SMA 6 M5 crossover strategy.

SMA6 rule:
- BUY: previous closed M5 candle close <= previous SMA6,
        latest closed M5 candle close > latest SMA6,
        and SMA6 is rising.
- SELL: opposite conditions.
- The live/current M5 candle is excluded from calculations.

Existing strategies are retained:
VWAP+VP, Liquidity Sweep, EMA+RSI, Order Block, FVG, SMA6.
"""

import json
import os
import statistics
import sys
import time
import traceback
import requests
from datetime import datetime, timezone, date, timedelta

# ================= CONFIG =================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")

SYMBOL = "XAUUSD"
LOOKBACK_BARS = 500
POINT_SIZE = 1.0

COOLDOWN_SECONDS = 240
ZONE_DEDUP_SECONDS = 3600

ATR_PERIOD = 14
MIN_SL_POINTS = 6.0
MAX_SL_POINTS = 10.0
MIN_TP_POINTS = 8.0
MAX_TP_POINTS = 12.0
BE_BUFFER = 0.50

STRATEGY_ATR_CONFIG = {
    "VWAP+VP":         {"sl_mult": 1.2, "tp1_ratio": 0.75, "tp2_ratio": 1.25},
    "EMA+RSI":         {"sl_mult": 1.2, "tp1_ratio": 0.75, "tp2_ratio": 1.25},
    "Liquidity Sweep": {"sl_mult": 1.4, "tp1_ratio": 0.80, "tp2_ratio": 1.35},
    "Order Block":     {"sl_mult": 1.4, "tp1_ratio": 0.80, "tp2_ratio": 1.35},
    "FVG":             {"sl_mult": 1.3, "tp1_ratio": 0.80, "tp2_ratio": 1.30},
    "SMA6":            {"sl_mult": 1.2, "tp1_ratio": 0.75, "tp2_ratio": 1.25},
    # Fixed points, not ATR-scaled — overridden directly in main() below.
    "MA4/45 Pullback": {"sl_mult": 1.0, "tp1_ratio": 1.0, "tp2_ratio": 1.375},
}

# ---- MA4/45 Pullback strategy settings ----
MA_FAST_PERIOD = 4
MA_SLOW_PERIOD = 45
MA_SLOPE_LOOKBACK = 10
MA_SLOPE_THRESHOLD = 3.0   # dollars the slow MA must have moved to count as trending
MA_CONFIRM_WINDOW = 3      # bars after the touch allowed for the confirming candle
MA_FIXED_SL = 8.0
MA_FIXED_TP1 = 8.0
MA_FIXED_TP2 = 11.0

# MA3/25 touch-cross strategy (fast MA reacts to the live tick, so it
# doesn't wait for the candle to close before firing).
MA2_PERIOD = 3
MA148_PERIOD = 25
MA2148_FIXED_SL = 7.0
MA2148_FIXED_TP1 = 7.0
MA2148_FIXED_TP2 = 11.0
MA_CROSS_SLOPE_LOOKBACK = 3  # bars back to measure the slow MA's slope
MA_CROSS_MIN_SLOPE = 0.02    # min pts/bar slope required — filters out flat chop

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
SWING_LOOKBACK = 12
SWEEP_TOLERANCE = 1.5
OB_LOOKBACK = 20
OB_IMPULSE_MULT = 1.8
FVG_MIN_GAP = 1.5
TOUCH_TOLERANCE = 3.0
VOLUME_MULT = 1.3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_FILE = os.path.join(SCRIPT_DIR, "trades.json")
STATE_FILE = os.path.join(SCRIPT_DIR, "state.json")


# ================= STATE =================
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_trades():
    trades = load_json(TRADE_LOG_FILE, [])

    # Backward compatibility for trades created before TP1/BE tracking
    # was added. Older open trades may not contain "tp1_hit".
    if isinstance(trades, list):
        changed = False
        for t in trades:
            if not isinstance(t, dict):
                continue

            # Backward compatibility for older trade records.
            if "tp1_hit" not in t:
                t["tp1_hit"] = False
                changed = True

            # Some older/open records can contain null pnl_points.
            # Always normalize it to a number before += operations.
            if t.get("pnl_points") is None:
                t["pnl_points"] = 0.0
                changed = True

            # Older trade records may also be missing one or more price
            # fields. Do not guess SL/TP values for an already-open trade.
            # Close such legacy records as invalid so they cannot crash the
            # scanner or create a fake result.
            if t.get("status") == "open":
                required = ("direction", "entry", "sl_price", "tp1_price", "tp2_price")
                if any(t.get(k) is None for k in required):
                    t["status"] = "closed"
                    t["result"] = "invalid_legacy"
                    t["closed_at"] = datetime.now(timezone.utc).isoformat()
                    changed = True

        if changed:
            save_json(TRADE_LOG_FILE, trades)

    return trades


def save_trades(trades):
    save_json(TRADE_LOG_FILE, trades)


def load_state():
    default = {
        "last_signal_time": {},
        "last_signal_direction": {},
        "last_summary_date": None,
        "zone_last_fired": {},
    }
    state = load_json(STATE_FILE, default)

    if not isinstance(state, dict):
        return default

    old_time = state.get("last_signal_time")
    if not isinstance(old_time, dict):
        state["last_signal_time"] = {}
        if isinstance(old_time, (int, float)):
            for strategy in STRATEGY_ATR_CONFIG:
                state["last_signal_time"][strategy] = float(old_time)

    old_dir = state.get("last_signal_direction")
    if not isinstance(old_dir, dict):
        state["last_signal_direction"] = {}
        if isinstance(old_dir, str):
            for strategy in STRATEGY_ATR_CONFIG:
                state["last_signal_direction"][strategy] = old_dir

    if not isinstance(state.get("zone_last_fired"), dict):
        state["zone_last_fired"] = {}

    return state


def save_state(state):
    save_json(STATE_FILE, state)


def zone_already_fired(state, zone_key):
    last = state.setdefault("zone_last_fired", {}).get(zone_key)
    return last is not None and (time.time() - last) < ZONE_DEDUP_SECONDS


def mark_zone_fired(state, zone_key):
    state.setdefault("zone_last_fired", {})[zone_key] = time.time()


# ================= TRADE MANAGEMENT =================
def price_to_points(price_distance):
    return int(round(price_distance / POINT_SIZE))


def open_trade(direction, entry, sl, tp1, tp2, strategy, info=""):
    trades = load_trades()
    trades.append({
        "direction": direction,
        "entry": entry,
        "sl_price": sl,
        "original_sl": sl,
        "tp1_price": tp1,
        "tp2_price": tp2,
        "tp1_hit": False,
        "strategy": strategy,
        "level": info,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "closed_at": None,
        "result": None,
        "pnl_points": 0.0,
    })
    save_trades(trades)


def check_open_trades(price):
    trades = load_trades()
    changed = False

    for t in trades:
        if t.get("status") != "open":
            continue

        if t.get("pnl_points") is None:
            t["pnl_points"] = 0.0

        direction = t["direction"]
        entry = t["entry"]

        if direction == "long":
            if not t.get("tp1_hit", False) and price >= t["tp1_price"]:
                t["tp1_hit"] = True
                t["sl_price"] = round(entry + BE_BUFFER, 2)
                t["pnl_points"] += price_to_points(
                    (t["tp1_price"] - entry) * 0.5
                )
                changed = True
                save_trades(trades)
                send_telegram(
                    f"🎯 <b>TP1 HIT (+{round(t['tp1_price'] - entry, 2)} pts) — "
                    f"{t['strategy']}</b>\n"
                    f"50% closed. SL moved to Breakeven: "
                    f"<b>{t['sl_price']:.2f}</b>"
                )

            elif price >= t["tp2_price"]:
                runner_mult = 0.5 if t.get("tp1_hit", False) else 1.0
                t["pnl_points"] += price_to_points(
                    (t["tp2_price"] - entry) * runner_mult
                )
                t.update(
                    status="closed",
                    result="win",
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
                changed = True
                save_trades(trades)
                notify_result(t)

            elif price <= t["sl_price"]:
                if t.get("tp1_hit", False):
                    t.update(
                        status="closed",
                        result="win",
                        closed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    changed = True
                    save_trades(trades)
                    send_telegram(
                        f"🛡️ <b>BREAKEVEN HIT — {t['strategy']}</b>\n"
                        f"Remaining 50% closed at {t['sl_price']:.2f}."
                    )
                else:
                    t["pnl_points"] = -price_to_points(
                        entry - t["sl_price"]
                    )
                    t.update(
                        status="closed",
                        result="loss",
                        closed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    changed = True
                    save_trades(trades)
                    notify_result(t)

        else:
            if not t.get("tp1_hit", False) and price <= t["tp1_price"]:
                t["tp1_hit"] = True
                t["sl_price"] = round(entry - BE_BUFFER, 2)
                t["pnl_points"] += price_to_points(
                    (entry - t["tp1_price"]) * 0.5
                )
                changed = True
                save_trades(trades)
                send_telegram(
                    f"🎯 <b>TP1 HIT (+{round(entry - t['tp1_price'], 2)} pts) — "
                    f"{t['strategy']}</b>\n"
                    f"50% closed. SL moved to Breakeven: "
                    f"<b>{t['sl_price']:.2f}</b>"
                )

            elif price <= t["tp2_price"]:
                runner_mult = 0.5 if t.get("tp1_hit", False) else 1.0
                t["pnl_points"] += price_to_points(
                    (entry - t["tp2_price"]) * runner_mult
                )
                t.update(
                    status="closed",
                    result="win",
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
                changed = True
                save_trades(trades)
                notify_result(t)

            elif price >= t["sl_price"]:
                if t.get("tp1_hit", False):
                    t.update(
                        status="closed",
                        result="win",
                        closed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    changed = True
                    save_trades(trades)
                    send_telegram(
                        f"🛡️ <b>BREAKEVEN HIT — {t['strategy']}</b>\n"
                        f"Remaining 50% closed at {t['sl_price']:.2f}."
                    )
                else:
                    t["pnl_points"] = -price_to_points(
                        t["sl_price"] - entry
                    )
                    t.update(
                        status="closed",
                        result="loss",
                        closed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    changed = True
                    save_trades(trades)
                    notify_result(t)

    if changed:
        save_trades(trades)


def trade_stats(trades):
    closed = [t for t in trades if t.get("status") == "closed"]
    wins = [t for t in closed if t.get("result") == "win"]
    losses = [t for t in closed if t.get("result") == "loss"]
    points = sum(float(t.get("pnl_points") or 0) for t in closed)
    wr = (len(wins) / len(closed) * 100) if closed else 0.0
    return len(wins), len(losses), points, wr


def strategy_stats(trades, strategy):
    closed = [
        t for t in trades
        if t.get("status") == "closed" and t.get("strategy") == strategy
    ]
    wins = [t for t in closed if t.get("result") == "win"]
    losses = [t for t in closed if t.get("result") == "loss"]
    points = sum(float(t.get("pnl_points") or 0) for t in closed)
    wr = (len(wins) / len(closed) * 100) if closed else 0.0
    return len(wins), len(losses), points, wr


def notify_result(t):
    emoji = "✅ WIN" if t["result"] == "win" else "❌ LOSS"
    all_trades = load_trades()
    wins, losses, net_points, wr = trade_stats(all_trades)
    s_wins, s_losses, s_points, s_wr = strategy_stats(all_trades, t.get("strategy"))
    msg = (
        f"<b>{emoji}</b> — XAUUSD {t['direction'].upper()}\n"
        f"Strategy: <b>{t.get('strategy')}</b>\n"
        f"Entry: {t['entry']:.2f} | {t.get('level','-')}\n"
        f"Result: {t['result'].upper()} | "
        f"{int(t['pnl_points']):+d} points\n"
        f"This strategy: {s_wins}W / {s_losses}L | WR {s_wr:.1f}% | "
        f"Net {s_points:+.0f} points\n"
        f"All strategies: {wins}W / {losses}L | WR {wr:.1f}% | "
        f"Net {net_points:+.0f} points"
    )
    send_telegram(msg)


def send_daily_summary(for_date):
    trades = load_trades()
    day_str = for_date.isoformat()
    day_trades = [
        t for t in trades
        if t.get("status") == "closed"
        and t.get("closed_at", "")[:10] == day_str
    ]

    if not day_trades:
        send_telegram(f"<b>📊 Daily Summary — {day_str}</b>\nNo closed trades.")
        return

    wins = [t for t in day_trades if t["result"] == "win"]
    losses = [t for t in day_trades if t["result"] == "loss"]
    total = sum(t["pnl_points"] for t in day_trades)
    wr = len(wins) / len(day_trades) * 100

    by = {}
    for t in day_trades:
        s = t.get("strategy", "?")
        by.setdefault(s, {"w": 0, "l": 0, "pts": 0})
        if t["result"] == "win":
            by[s]["w"] += 1
        else:
            by[s]["l"] += 1
        by[s]["pts"] += int(t.get("pnl_points") or 0)

    lines = "\n".join(
        f"• {s}: {v['w']}W/{v['l']}L | {v['pts']:+d} pts"
        for s, v in by.items()
    )

    send_telegram(
        f"<b>📊 Daily Summary — {day_str}</b>\n"
        f"Total: {len(day_trades)} | {len(wins)}W/{len(losses)}L | "
        f"WR {wr:.1f}% | Net {int(total):+d} points\n\n{lines}"
    )


# ================= DATA =================
def fetch_xaus_json(path, params=None, timeout=15, retries=2, backoff=3):
    url = f"https://xaus.com{path}"
    headers = {"User-Agent": "XAUUSD-Multi-Strategy-Bot/1.0"}

    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params or {}, headers=headers, timeout=timeout)
            r.raise_for_status()
            data = r.json()

            if isinstance(data, dict):
                state = data.get("data_state") or {}
                if state.get("status") == "unavailable":
                    raise RuntimeError("XAUS data is unavailable")

            return data
        except Exception as e:
            last_exc = e
            if attempt < retries:
                print(f"[XAUS retry] {path} attempt {attempt + 1} failed: {e}, retrying in {backoff}s")
                time.sleep(backoff)

    raise last_exc


def fetch_spot():
    try:
        data = fetch_xaus_json(
            "/api/v1/spot",
            {"currency": "USD", "unit": "oz", "compact": "1"},
            timeout=10,
        )
        price = data.get("spot_usd_oz")
        if price is not None:
            return float(price)
    except Exception as e:
        print(f"[XAUS spot fail] {e}")

    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(
            "https://data-asg.goldprice.org/dbXRates/USD",
            headers=headers,
            timeout=8,
        )
        return float(r.json()["items"][0]["xauPrice"])
    except Exception:
        pass

    try:
        r = requests.get(
            "https://api.gold-api.com/price/XAU",
            headers=headers,
            timeout=8,
        )
        return float(r.json()["price"])
    except Exception:
        pass

    return None


def _parse_xaus_chart_points(data):
    points = data.get("points") if isinstance(data, dict) else None
    if not isinstance(points, list):
        return []

    candles = []

    for p in points:
        try:
            ts = int(p["t"])
            o = float(p["o"])
            h = float(p["h"])
            lo = float(p["l"])
            c = float(p["c"])
            v = float(p.get("v") or 0.0)

            if min(o, h, lo, c) <= 0:
                continue

            candles.append({
                "open_time": ts * 1000,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": max(v, 0.0),
            })
        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["open_time"])
    return candles


def _get_xauusd_m5_chart(limit=LOOKBACK_BARS):
    data = fetch_xaus_json(
        "/api/v1/chart",
        {"symbol": "xau", "range": "5d", "interval": "5m"},
        timeout=20,
    )

    candles = _parse_xaus_chart_points(data)

    if len(candles) < min(limit, 30):
        raise RuntimeError(
            f"XAUS returned only {len(candles)} XAUUSD M5 candles"
        )

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    current_bucket_ms = (now_ms // (5 * 60 * 1000)) * (5 * 60 * 1000)

    candles = [
        c for c in candles
        if c["open_time"] < current_bucket_ms
    ]

    if len(candles) < min(limit, 30):
        raise RuntimeError(
            "Not enough completed XAUUSD M5 candles after removing the live candle"
        )

    return candles[-limit:]


def _get_xauusd_m5_intraday_fallback(limit=LOOKBACK_BARS):
    data = fetch_xaus_json(
        "/api/v1/intraday",
        {"symbol": "xau", "hours": 48},
        timeout=20,
    )

    points = data.get("points") if isinstance(data, dict) else None
    if not isinstance(points, list):
        raise RuntimeError("XAUS intraday response has no points")

    buckets = {}

    for p in points:
        try:
            ts = int(p["t"])
            price = float(p["p"])

            if price <= 0:
                continue

            bucket = (ts // 300) * 300
            b = buckets.get(bucket)

            if b is None:
                buckets[bucket] = {
                    "open_time": bucket * 1000,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 0.0,
                }
            else:
                b["high"] = max(b["high"], price)
                b["low"] = min(b["low"], price)
                b["close"] = price

        except (KeyError, TypeError, ValueError):
            continue

    candles = [buckets[k] for k in sorted(buckets)]

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    current_bucket_ms = (now_ms // (5 * 60 * 1000)) * (5 * 60 * 1000)

    candles = [
        c for c in candles
        if c["open_time"] < current_bucket_ms
    ]

    if len(candles) < min(limit, 30):
        raise RuntimeError(
            f"XAUS intraday fallback returned only {len(candles)} M5 candles"
        )

    return candles[-limit:]


def _get_xauusd_m5_twelvedata(limit=LOOKBACK_BARS):
    if not TWELVEDATA_API_KEY:
        raise RuntimeError("TWELVEDATA_API_KEY not set, skipping TwelveData fallback")

    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": "XAU/USD",
            "interval": "5min",
            "outputsize": min(limit, 5000),
            "apikey": TWELVEDATA_API_KEY,
            "timezone": "UTC",
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()

    if data.get("status") == "error":
        raise RuntimeError(f"TwelveData error: {data.get('message')}")

    values = data.get("values")
    if not isinstance(values, list) or not values:
        raise RuntimeError("TwelveData returned no candle values")

    candles = []
    for v in values:
        try:
            dt = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            candles.append({
                "open_time": int(dt.timestamp() * 1000),
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
                "volume": float(v.get("volume") or 0.0),
            })
        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["open_time"])

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    current_bucket_ms = (now_ms // (5 * 60 * 1000)) * (5 * 60 * 1000)
    candles = [c for c in candles if c["open_time"] < current_bucket_ms]

    if len(candles) < min(limit, 30):
        raise RuntimeError(
            f"TwelveData returned only {len(candles)} usable M5 candles"
        )

    return candles[-limit:]


def sanitize_candles(candles, max_pct_jump=0.03):
    """
    Drop candles whose close jumps more than max_pct_jump (3%) from the
    previous accepted candle's close. Gold rarely moves >1% in a single
    M5 bar under normal conditions, so a bigger jump is almost always a
    bad data point from a flaky free-tier API (like a stray outlier
    candle skewing every moving-average calculation downstream) rather
    than a real price move.
    """
    if not candles:
        return candles

    cleaned = [candles[0]]
    dropped = 0
    for c in candles[1:]:
        prev_close = cleaned[-1]["close"]
        if prev_close and abs(c["close"] - prev_close) / prev_close > max_pct_jump:
            dropped += 1
            continue
        cleaned.append(c)

    if dropped:
        print(f"[DATA WARNING] Dropped {dropped} outlier candle(s) from feed")

    return cleaned


def get_klines(limit=LOOKBACK_BARS):
    """Returns (candles, source) where source is one of
    "xaus_chart", "xaus_intraday", "twelvedata", or None on total failure."""
    try:
        candles = sanitize_candles(_get_xauusd_m5_chart(limit))
        print(
            f"[DATA] XAUUSD M5 via XAUS chart: "
            f"{len(candles)} completed candles"
        )
        return candles, "xaus_chart"
    except Exception as e:
        print(f"[XAUS chart fail] {e}")

    try:
        candles = sanitize_candles(_get_xauusd_m5_intraday_fallback(limit))
        print(
            f"[DATA] XAUUSD M5 via XAUS intraday fallback: "
            f"{len(candles)} completed candles"
        )
        return candles, "xaus_intraday"
    except Exception as e2:
        print(f"[XAUS intraday fallback fail] {e2}")

    try:
        candles = sanitize_candles(_get_xauusd_m5_twelvedata(limit))
        print(
            f"[DATA] XAUUSD M5 via TwelveData fallback: "
            f"{len(candles)} completed candles"
        )
        return candles, "twelvedata"
    except Exception as e3:
        print(f"[TwelveData fallback fail] {e3}")
        return None, None


# ================= INDICATORS =================
def ema(values, period):
    if len(values) < period:
        return [None] * len(values)

    out = [None] * (period - 1)
    s = sum(values[:period]) / period
    out.append(s)

    k = 2 / (period + 1)

    for i in range(period, len(values)):
        out.append((values[i] - out[-1]) * k + out[-1])

    return out


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)

    out = [None] * period
    gains, losses = [], []

    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))

    ag = sum(gains) / period
    al = sum(losses) / period

    out.append(
        100 if al == 0
        else 100 - (100 / (1 + ag / al))
    )

    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(ch, 0)) / period
        al = (al * (period - 1) + max(-ch, 0)) / period

        out.append(
            100 if al == 0
            else 100 - (100 / (1 + ag / al))
        )

    return out


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        true_range = max(
            h - l,
            abs(h - prev_close),
            abs(l - prev_close),
        )

        trs.append(true_range)

    if len(trs) < period:
        return None

    return statistics.mean(trs[-period:])


def session_vwap(candles):
    vals = []
    cum_pv = 0.0
    cum_vol = 0.0
    day = None

    for c in candles:
        d = datetime.fromtimestamp(
            c["open_time"] / 1000,
            tz=timezone.utc,
        ).date()

        if d != day:
            day = d
            cum_pv = 0.0
            cum_vol = 0.0

        typ = (c["high"] + c["low"] + c["close"]) / 3
        vol = c["volume"] if c["volume"] > 0 else 1.0

        cum_pv += typ * vol
        cum_vol += vol

        vals.append(cum_pv / cum_vol if cum_vol else typ)

    return vals


def vwap_slope(vals, n=5):
    if len(vals) < n + 1:
        return "flat"

    d = vals[-1] - vals[-n]

    if d > 0.05:
        return "rising"
    if d < -0.05:
        return "falling"
    return "flat"


def volume_profile(candles, bins=10):
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    mx, mn = max(highs), min(lows)

    if mx == mn:
        return None

    size = (mx - mn) / bins
    vols = [0.0] * bins

    for c in candles:
        idx = max(
            0,
            min(bins - 1, int((c["close"] - mn) / size)),
        )
        vols[idx] += c["volume"] if c["volume"] > 0 else 1.0

    poc_i = vols.index(max(vols))
    poc = mn + (poc_i + 0.5) * size

    total = sum(vols)
    target = total * 0.70
    captured = vols[poc_i]

    lo = hi = poc_i

    while captured < target and (lo > 0 or hi < bins - 1):
        bel = vols[lo - 1] if lo > 0 else -1
        abv = vols[hi + 1] if hi < bins - 1 else -1

        if abv >= bel:
            hi += 1
            captured += vols[hi]
        else:
            lo -= 1
            captured += vols[lo]

    return {
        "poc": round(poc, 2),
        "vah": round(mn + (hi + 1) * size, 2),
        "val": round(mn + lo * size, 2),
    }


# ================= SESSION & 1H BIAS =================
def get_session(utc_hour):
    if 0 <= utc_hour < 7:
        return "Asian"
    if 7 <= utc_hour < 12:
        return "London"
    if 12 <= utc_hour < 21:
        return "NewYork"
    return "Late"


def get_1h_bias(candles_5m, ema_period=20):
    if len(candles_5m) < (ema_period * 12):
        return "neutral"

    groups = {}

    for c in candles_5m:
        ts = datetime.fromtimestamp(
            c["open_time"] / 1000,
            tz=timezone.utc,
        )
        bucket = ts.replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        groups[bucket] = c["close"]

    hourly_closes = [groups[k] for k in sorted(groups)]

    if len(hourly_closes) < ema_period:
        return "neutral"

    e_1h = ema(hourly_closes, ema_period)

    if e_1h[-1] is None:
        return "neutral"

    current_price = hourly_closes[-1]

    if current_price > e_1h[-1]:
        return "bullish"
    if current_price < e_1h[-1]:
        return "bearish"

    return "neutral"


# ================= STRATEGIES =================
def is_rejection(c, level, direction):
    total_range = c["high"] - c["low"]

    if total_range <= 0:
        return False

    body_high = max(c["open"], c["close"])
    body_low = min(c["open"], c["close"])

    if direction == "long":
        lower_wick = body_low - c["low"]
        return (
            lower_wick / total_range >= 0.25
            and c["close"] >= c["open"]
        )

    upper_wick = c["high"] - body_high
    return (
        upper_wick / total_range >= 0.25
        and c["close"] <= c["open"]
    )


def vol_ok(candles, idx):
    if idx < 10:
        return False

    vols = [c["volume"] for c in candles[idx - 10:idx]]

    if sum(vols) == 0:
        return True

    return candles[idx]["volume"] >= statistics.mean(vols) * VOLUME_MULT


def check_vwap_vp(candles, vwap, profile):
    if not profile:
        return None, None

    last = candles[-1]
    slope = vwap_slope(vwap)

    for name, price in [
        ("POC", profile["poc"]),
        ("VAH", profile["vah"]),
        ("VAL", profile["val"]),
    ]:
        if abs(last["close"] - price) > TOUCH_TOLERANCE:
            continue

        if (
            slope == "rising"
            and is_rejection(last, price, "long")
            and vol_ok(candles, len(candles) - 1)
        ):
            return "long", name

        if (
            slope == "falling"
            and is_rejection(last, price, "short")
            and vol_ok(candles, len(candles) - 1)
        ):
            return "short", name

    return None, None


def check_liquidity_sweep(candles):
    if len(candles) < SWING_LOOKBACK + 3:
        return None, None

    window = candles[-(SWING_LOOKBACK + 1):-1]
    sh = max(c["high"] for c in window)
    sl = min(c["low"] for c in window)
    last = candles[-1]

    if (
        last["low"] < sl - SWEEP_TOLERANCE
        and last["close"] > sl
        and last["close"] > last["open"]
    ):
        return "long", f"Sweep Low {sl:.2f}"

    if (
        last["high"] > sh + SWEEP_TOLERANCE
        and last["close"] < sh
        and last["close"] < last["open"]
    ):
        return "short", f"Sweep High {sh:.2f}"

    return None, None


def check_ema_rsi(candles):
    closes = [c["close"] for c in candles]

    if len(closes) < max(EMA_SLOW, RSI_PERIOD) + 5:
        return None, None

    ef = ema(closes, EMA_FAST)
    es = ema(closes, EMA_SLOW)
    r = rsi(closes, RSI_PERIOD)

    if None in (ef[-1], es[-1], r[-1], ef[-2], es[-2]):
        return None, None

    if ef[-2] <= es[-2] and ef[-1] > es[-1] and r[-1] > 50:
        return "long", f"EMA Cross RSI {r[-1]:.1f}"

    if ef[-2] >= es[-2] and ef[-1] < es[-1] and r[-1] < 50:
        return "short", f"EMA Cross RSI {r[-1]:.1f}"

    return None, None


def check_order_block(candles, state):
    if len(candles) < OB_LOOKBACK + 5:
        return None, None

    bodies = [
        abs(c["close"] - c["open"])
        for c in candles[-OB_LOOKBACK - 5:-1]
    ]
    avg_body = statistics.mean(bodies) if bodies else 1.0
    last = candles[-1]

    for i in range(
        len(candles) - 3,
        len(candles) - OB_LOOKBACK - 1,
        -1,
    ):
        c = candles[i]

        if c["close"] < c["open"]:
            impulse = any(
                candles[j]["close"] > candles[j]["open"]
                and abs(candles[j]["close"] - candles[j]["open"])
                > avg_body * OB_IMPULSE_MULT
                for j in range(
                    i + 1,
                    min(i + 3, len(candles) - 1),
                )
            )

            if (
                impulse
                and last["low"] <= c["high"]
                and last["high"] >= c["low"]
                and is_rejection(last, c["low"], "long")
            ):
                zone_key = f"OB-long-{round(c['low'], 1)}"

                if not zone_already_fired(state, zone_key):
                    mark_zone_fired(state, zone_key)
                    return "long", f"Bullish OB {c['low']:.2f}"

        if c["close"] > c["open"]:
            impulse = any(
                candles[j]["close"] < candles[j]["open"]
                and abs(candles[j]["close"] - candles[j]["open"])
                > avg_body * OB_IMPULSE_MULT
                for j in range(
                    i + 1,
                    min(i + 3, len(candles) - 1),
                )
            )

            if (
                impulse
                and last["low"] <= c["high"]
                and last["high"] >= c["low"]
                and is_rejection(last, c["high"], "short")
            ):
                zone_key = f"OB-short-{round(c['high'], 1)}"

                if not zone_already_fired(state, zone_key):
                    mark_zone_fired(state, zone_key)
                    return "short", f"Bearish OB {c['high']:.2f}"

    return None, None


def check_fvg(candles, state):
    if len(candles) < 8:
        return None, None

    last = candles[-1]

    for i in range(
        len(candles) - 2,
        max(len(candles) - 15, 2),
        -1,
    ):
        c1, c3 = candles[i - 2], candles[i]

        if c3["low"] > c1["high"] + FVG_MIN_GAP:
            if (
                last["low"] <= c3["low"]
                and last["high"] >= c1["high"]
                and last["close"] > last["open"]
            ):
                zone_key = f"FVG-long-{round(c1['high'], 1)}"

                if not zone_already_fired(state, zone_key):
                    mark_zone_fired(state, zone_key)
                    return "long", f"Bullish FVG {c1['high']:.2f}"

        if c3["high"] < c1["low"] - FVG_MIN_GAP:
            if (
                last["high"] >= c3["high"]
                and last["low"] <= c1["low"]
                and last["close"] < last["open"]
            ):
                zone_key = f"FVG-short-{round(c3['high'], 1)}"

                if not zone_already_fired(state, zone_key):
                    mark_zone_fired(state, zone_key)
                    return "short", f"Bearish FVG {c3['high']:.2f}"

    return None, None


def check_sma6(candles):
    """
    SMA 6 on completed M5 candles.

    BUY:
      previous close <= previous SMA6
      current close > current SMA6
      current SMA6 > previous SMA6

    SELL:
      previous close >= previous SMA6
      current close < current SMA6
      current SMA6 < previous SMA6
    """
    if len(candles) < 8:
        return None, None

    closes = [c["close"] for c in candles]

    sma = [None] * len(closes)

    for i in range(5, len(closes)):
        sma[i] = sum(closes[i - 5:i + 1]) / 6

    prev_close = closes[-2]
    current_close = closes[-1]
    prev_sma = sma[-2]
    current_sma = sma[-1]

    if prev_sma is None or current_sma is None:
        return None, None

    if (
        prev_close <= prev_sma
        and current_close > current_sma
        and current_sma > prev_sma
    ):
        return "long", f"SMA6 Cross Up {current_sma:.2f}"

    if (
        prev_close >= prev_sma
        and current_close < current_sma
        and current_sma < prev_sma
    ):
        return "short", f"SMA6 Cross Down {current_sma:.2f}"

    return None, None


def check_ma_pullback(candles):
    """
    SMA(4) / SMA(45) trend-pullback, validated against real M5 XAUUSD data.

    1. Trend filter: slow MA must have moved > MA_SLOPE_THRESHOLD dollars
       over the last MA_SLOPE_LOOKBACK bars (skips flat/choppy stretches).
    2. Touch: fast MA crosses to/through the slow MA.
    3. Confirmation is NOT required on the same bar as the touch — real
       data shows the reversal candle lands 1-3 bars later. We look back
       up to MA_CONFIRM_WINDOW bars from the current closed candle for a
       matching touch.
    """
    need = MA_SLOW_PERIOD + MA_SLOPE_LOOKBACK + MA_CONFIRM_WINDOW + 2
    if len(candles) < need:
        return None, None

    closes = [c["close"] for c in candles]
    opens = [c["open"] for c in candles]
    n = len(closes)

    ma_fast = [None] * n
    ma_slow = [None] * n
    for i in range(MA_FAST_PERIOD - 1, n):
        ma_fast[i] = sum(closes[i - MA_FAST_PERIOD + 1:i + 1]) / MA_FAST_PERIOD
    for i in range(MA_SLOW_PERIOD - 1, n):
        ma_slow[i] = sum(closes[i - MA_SLOW_PERIOD + 1:i + 1]) / MA_SLOW_PERIOD

    last = n - 1  # last closed candle — the confirmation candidate
    if ma_slow[last] is None:
        return None, None

    bull = closes[last] > opens[last] and closes[last] > ma_slow[last]
    bear = closes[last] < opens[last] and closes[last] < ma_slow[last]
    if not (bull or bear):
        return None, None

    for back in range(1, MA_CONFIRM_WINDOW + 1):
        i = last - back
        if i - MA_SLOPE_LOOKBACK < 0:
            continue
        if None in (ma_fast[i], ma_fast[i - 1], ma_slow[i], ma_slow[i - 1], ma_slow[i - MA_SLOPE_LOOKBACK]):
            continue

        slope = ma_slow[i] - ma_slow[i - MA_SLOPE_LOOKBACK]
        touched_from_above = ma_fast[i - 1] > ma_slow[i - 1] and ma_fast[i] <= ma_slow[i]
        touched_from_below = ma_fast[i - 1] < ma_slow[i - 1] and ma_fast[i] >= ma_slow[i]

        if bull and slope > MA_SLOPE_THRESHOLD and touched_from_above:
            return "long", f"MA4/45 Pullback (touch {back} bar{'s' if back > 1 else ''} ago)"
        if bear and slope < -MA_SLOPE_THRESHOLD and touched_from_below:
            return "short", f"MA4/45 Pullback (touch {back} bar{'s' if back > 1 else ''} ago)"

    return None, None


def check_ma2_148_cross(candles, live_price, atr_value=None):
    """
    MA(3) / MA(25) touch-cross, evaluated on CLOSED candles only.

    Both the fast and slow MA are computed from the SAME candle series,
    so any absolute pricing offset between this data source and your
    broker's own feed cancels out — the decision only depends on the
    fast line's position relative to the slow line within one series,
    never on comparing this series' history against a separately
    fetched live tick.

    A slope filter on the slow MA(25) is also applied: a touch/cross is
    only taken when the slow line itself is clearly sloping in that
    direction (a real short-term trend), not flat/chopping — this is
    what filters out the whipsaw touches that happen during small
    sideways pullbacks where the two lines briefly overlap and price
    immediately reverses back.

    BUY:  fast MA was at/below slow MA, now above it, AND slow MA rising.
    SELL: fast MA was at/above slow MA, now below it, AND slow MA falling.
    """
    if len(candles) < MA148_PERIOD + MA_CROSS_SLOPE_LOOKBACK + 2:
        return None, None

    closes = [c["close"] for c in candles]

    slow = [None] * len(closes)
    fast = [None] * len(closes)
    for i in range(MA148_PERIOD - 1, len(closes)):
        slow[i] = sum(closes[i - MA148_PERIOD + 1:i + 1]) / MA148_PERIOD
    for i in range(MA2_PERIOD - 1, len(closes)):
        fast[i] = sum(closes[i - MA2_PERIOD + 1:i + 1]) / MA2_PERIOD

    last = len(closes) - 1
    prev = last - 1

    if slow[prev] is None or slow[last] is None:
        return None, None

    slope_ref = last - MA_CROSS_SLOPE_LOOKBACK
    if slope_ref < 0 or slow[slope_ref] is None:
        return None, None

    slope = (slow[last] - slow[slope_ref]) / MA_CROSS_SLOPE_LOOKBACK

    fast_prev, slow_prev = fast[prev], slow[prev]
    fast_now, slow_now = fast[last], slow[last]

    entry_price = live_price if live_price is not None else closes[last]

    if fast_prev <= slow_prev and fast_now > slow_now:
        if slope < MA_CROSS_MIN_SLOPE:
            return None, None
        return "long", f"MA3/25 Touch Up @ {entry_price:.2f} (MA25 {slow_now:.2f}, slope {slope:+.2f})"

    if fast_prev >= slow_prev and fast_now < slow_now:
        if slope > -MA_CROSS_MIN_SLOPE:
            return None, None
        return "short", f"MA3/25 Touch Down @ {entry_price:.2f} (MA25 {slow_now:.2f}, slope {slope:+.2f})"

    return None, None
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
            },
            timeout=12,
        )
    except Exception as e:
        print(f"[TG ERROR] {e}")


def format_signal(
    direction,
    entry,
    strategy,
    info,
    sl,
    tp1,
    tp2,
    sl_pts,
    tp1_pts,
    tp2_pts,
    session,
    bias_1h,
):
    arrow = "🟢 BUY" if direction == "long" else "🔴 SELL"

    return (
        f"<b>{arrow} XAUUSD (5m)</b>\n"
        f"Strategy: <b>{strategy}</b>\n"
        f"Entry: <b>{entry:.2f}</b> | {info}\n"
        f"1H Trend: <b>{bias_1h.upper()}</b> | Session: {session}\n"
        f"─────────────────────\n"
        f"🛑 SL: <b>{sl:.2f}</b> (-{sl_pts} pts) [Max 10.0]\n"
        f"🎯 TP1: <b>{tp1:.2f}</b> (+{tp1_pts} pts) [50% & BE]\n"
        f"🏁 TP2: <b>{tp2:.2f}</b> (+{tp2_pts} pts) [Max 12.0]\n"
        f"─────────────────────\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )


def send_weekly_summary(now_utc):
    trades = load_trades()
    week_ago = now_utc - timedelta(days=7)

    week_trades = [
        t for t in trades
        if t.get("status") == "closed"
        and t.get("closed_at")
        and datetime.fromisoformat(t["closed_at"].replace("Z", "+00:00")) >= week_ago
    ]

    if not week_trades:
        send_telegram(
            f"<b>🗓️ Weekly Summary — {week_ago.date().isoformat()} to {now_utc.date().isoformat()}</b>\n"
            f"No closed trades this week."
        )
        return

    wins = [t for t in week_trades if t["result"] == "win"]
    losses = [t for t in week_trades if t["result"] == "loss"]
    total = sum(t["pnl_points"] for t in week_trades)
    wr = len(wins) / len(week_trades) * 100

    by = {}
    for t in week_trades:
        s = t.get("strategy", "?")
        by.setdefault(s, {"w": 0, "l": 0, "pts": 0})
        if t["result"] == "win":
            by[s]["w"] += 1
        else:
            by[s]["l"] += 1
        by[s]["pts"] += int(t.get("pnl_points") or 0)

    all_strategies = [
        "VWAP+VP", "Liquidity Sweep", "EMA+RSI",
        "Order Block", "FVG", "SMA6", "MA4/45 Pullback",
        "MA3/25 Cross",
    ]

    lines = []
    for s in all_strategies:
        v = by.get(s)
        if v:
            lines.append(f"• {s}: {v['w']}W/{v['l']}L | {v['pts']:+d} pts")
        else:
            lines.append(f"• {s}: no closed trades")

    send_telegram(
        f"<b>🗓️ Weekly Summary — {week_ago.date().isoformat()} to {now_utc.date().isoformat()}</b>\n"
        f"Total: {len(week_trades)} | {len(wins)}W/{len(losses)}L | "
        f"WR {wr:.1f}% | Net {int(total):+d} points\n\n" + "\n".join(lines)
    )


def is_octa_xauusd_open(now_utc):
    weekday = now_utc.weekday()
    hour_min = now_utc.hour * 60 + now_utc.minute

    if weekday == 5:
        return False

    if weekday == 6:
        return hour_min >= 22 * 60

    return hour_min < 21 * 60 or hour_min >= 22 * 60


def has_open_trade(strategy):
    trades = load_trades()
    return any(
        t.get("strategy") == strategy and t.get("status") == "open"
        for t in trades
    )


def can_send(state, strategy, direction):
    if has_open_trade(strategy):
        return False

    now = time.time()
    last_t = state.get("last_signal_time", {}).get(strategy, 0)
    last_d = state.get("last_signal_direction", {}).get(strategy)

    return not (
        now - last_t < COOLDOWN_SECONDS
        and last_d == direction
    )


def record(state, strategy, direction):
    state.setdefault("last_signal_time", {})[strategy] = time.time()
    state.setdefault("last_signal_direction", {})[strategy] = direction


# ================= MAIN =================
def run_strategy(strategy_name, func, *args):
    """
    Run a single strategy's check function in isolation so that:
    1. One strategy raising an exception never crashes the whole scan
       (which previously made ALL strategies go silent for that run).
    2. Every run prints one line per strategy, so the Actions log always
       shows whether a strategy evaluated cleanly, found nothing, fired
       a signal, or errored out.
    """
    try:
        direction, info = func(*args)
    except Exception as exc:
        print(
            f"[STRATEGY ERROR] {strategy_name}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc()
        return None, None

    if direction:
        print(f"[STRATEGY] {strategy_name}: signal={direction} info={info!r}")
    else:
        print(f"[STRATEGY] {strategy_name}: no signal")

    return direction, info


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Missing secrets", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    now_utc = datetime.now(timezone.utc)

    if not is_octa_xauusd_open(now_utc):
        print(
            f"[MARKET CLOSED] Octa XAUUSD is closed | "
            f"{now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

        # Saturday ~8:00 AM IST (2:30 UTC) weekly report.
        # Market is closed all Saturday anyway, so this never touches
        # strategy/signal logic below.
        hour_min = now_utc.hour * 60 + now_utc.minute
        today = now_utc.date().isoformat()
        if (
            now_utc.weekday() == 5
            and 150 <= hour_min < 160
            and state.get("last_weekly_summary_date") != today
        ):
            send_weekly_summary(now_utc)
            state["last_weekly_summary_date"] = today

        save_state(state)
        return

    candles, data_source = get_klines()

    if candles is None:
        print("[DATA] No candle data available this run (all sources down) — skipping scan.")
        save_state(state)
        return

    live_spot = fetch_spot()
    price = (
        live_spot
        if live_spot is not None
        else candles[-1]["close"]
    )

    session = get_session(now_utc.hour)
    bias_1h = get_1h_bias(candles, ema_period=20)
    current_atr = atr(candles, ATR_PERIOD)
    fallback_atr = 5.0

    check_open_trades(price)

    today = now_utc.date().isoformat()

    if state.get("last_summary_date") is None:
        state["last_summary_date"] = today
    elif today != state["last_summary_date"]:
        y, m, d = map(
            int,
            state["last_summary_date"].split("-"),
        )
        send_daily_summary(date(y, m, d))
        state["last_summary_date"] = today

    raw = []

    vwap = session_vwap(candles)
    profile = volume_profile(candles[-200:])

    d, info = run_strategy("VWAP+VP", check_vwap_vp, candles, vwap, profile)
    if d:
        raw.append(("VWAP+VP", d, info or ""))

    d, info = run_strategy("Liquidity Sweep", check_liquidity_sweep, candles)
    if d:
        raw.append(("Liquidity Sweep", d, info or ""))

    d, info = run_strategy("EMA+RSI", check_ema_rsi, candles)
    if d:
        raw.append(("EMA+RSI", d, info or ""))

    d, info = run_strategy("Order Block", check_order_block, candles, state)
    if d:
        raw.append(("Order Block", d, info or ""))

    d, info = run_strategy("FVG", check_fvg, candles, state)
    if d:
        raw.append(("FVG", d, info or ""))

    # NEW: SMA6 strategy
    d, info = run_strategy("SMA6", check_sma6, candles)
    if d:
        raw.append(("SMA6", d, info or ""))

    # NEW: MA4/45 trend-pullback strategy
    d, info = run_strategy("MA4/45 Pullback", check_ma_pullback, candles)
    if d:
        raw.append(("MA4/45 Pullback", d, info or ""))

    # NEW: MA3/25 live touch-cross strategy
    # Only runs on XAUS-sourced candles — this strategy compares an
    # absolute historical average against the live spot price, so it
    # only makes sense when both come from the same/consistent feed as
    # your broker. TwelveData's historical series has been observed to
    # disagree with the broker's own MA by 30+ points, which would
    # generate false signals, so skip it on that fallback.
    if data_source in ("xaus_chart", "xaus_intraday"):
        d, info = run_strategy(
            "MA3/25 Cross", check_ma2_148_cross, candles, price,
            current_atr if current_atr else fallback_atr,
        )
        if d:
            raw.append(("MA3/25 Cross", d, info or ""))
    else:
        print(
            f"[STRATEGY] MA3/25 Cross: skipped — data source is "
            f"'{data_source}', not the primary XAUS feed"
        )

    final = []

    for strategy, direction, info in raw:
        if strategy == "MA3/25 Cross":
            # No 1H bias filter, no cooldown — fires immediately both ways.
            final.append((strategy, direction, info))
            continue

        # Keep the existing 1H bias filter.
        if bias_1h == "bullish" and direction == "short":
            print(f"[FILTERED] {strategy} {direction}: blocked by 1H bullish bias")
            continue

        if bias_1h == "bearish" and direction == "long":
            print(f"[FILTERED] {strategy} {direction}: blocked by 1H bearish bias")
            continue

        if not can_send(state, strategy, direction):
            reason = "already has an open trade" if has_open_trade(strategy) else "cooldown/dedup"
            print(f"[FILTERED] {strategy} {direction}: blocked by {reason} (can_send)")
            continue

        final.append((strategy, direction, info))

    for strategy, direction, info in final:
        if strategy == "MA4/45 Pullback":
            # Fixed points, as specified — not ATR-scaled like the others.
            sl_pts, tp1_pts, tp2_pts = MA_FIXED_SL, MA_FIXED_TP1, MA_FIXED_TP2
        elif strategy == "MA3/25 Cross":
            sl_pts, tp1_pts, tp2_pts = MA2148_FIXED_SL, MA2148_FIXED_TP1, MA2148_FIXED_TP2
        else:
            cfg = STRATEGY_ATR_CONFIG.get(
                strategy,
                {
                    "sl_mult": 1.2,
                    "tp1_ratio": 0.75,
                    "tp2_ratio": 1.25,
                },
            )

            active_atr = (
                current_atr
                if current_atr is not None
                else fallback_atr
            )

            raw_sl = round(
                active_atr * cfg["sl_mult"],
                2,
            )

            sl_pts = min(
                max(raw_sl, MIN_SL_POINTS),
                MAX_SL_POINTS,
            )

            raw_tp2 = round(
                sl_pts * cfg["tp2_ratio"],
                2,
            )

            tp2_pts = min(
                max(raw_tp2, MIN_TP_POINTS),
                MAX_TP_POINTS,
            )

            tp1_pts = round(
                sl_pts * cfg["tp1_ratio"],
                2,
            )

        if direction == "long":
            sl = round(price - sl_pts, 2)
            tp1 = round(price + tp1_pts, 2)
            tp2 = round(price + tp2_pts, 2)
        else:
            sl = round(price + sl_pts, 2)
            tp1 = round(price - tp1_pts, 2)
            tp2 = round(price - tp2_pts, 2)

        msg = format_signal(
            direction,
            price,
            strategy,
            info,
            sl,
            tp1,
            tp2,
            sl_pts,
            tp1_pts,
            tp2_pts,
            session,
            bias_1h,
        )

        send_telegram(msg)
        open_trade(
            direction,
            price,
            sl,
            tp1,
            tp2,
            strategy,
            info,
        )
        record(state, strategy, direction)

        print(
            f"[SIGNAL] {strategy} {direction.upper()} @ {price:.2f} | "
            f"SL: -{sl_pts} | TP1: +{tp1_pts} | TP2: +{tp2_pts}"
        )

    now_ts = time.time()

    state["zone_last_fired"] = {
        k: v
        for k, v in state.get("zone_last_fired", {}).items()
        if now_ts - v < ZONE_DEDUP_SECONDS
    }

    save_state(state)


if __name__ == "__main__":
    main()
