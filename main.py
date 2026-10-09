"""
XAUUSD Multi-Strategy Telegram Bot
Updated:
- MSB-OB: Configured to ZigZag 9, Fib 0.5. Triggers on the close of the first
  candle that closes across the swing high / low line (e.g. marked green breakout candle).
- SMA6 & SMA8: TradingView settings (Length 6/8, Source Close, Offset 1)
- SL/TP: SMA6 & SMA8 (SL 10 / TP 8), MSB-OB (SL 10 / TP 10), HalfTrend (SL 10 / TP 8)
- Daily & Weekly reports track all active strategies.
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
LOOKBACK_BARS = 800
POINT_SIZE = 1.0

COOLDOWN_SECONDS = 240
ZONE_DEDUP_SECONDS = 3600
GLOBAL_SIGNAL_COOLDOWN_SECONDS = 300

ATR_PERIOD = 14
MIN_SL_POINTS = 6.0
MAX_SL_POINTS = 10.0
MIN_TP_POINTS = 8.0
MAX_TP_POINTS = 12.0
BE_BUFFER = 0.50

# ---- Global fixed SL/TP for standard strategies ----
FIXED_SL_POINTS = 7.0
FIXED_TP_POINTS = 6.0

# ---- Feed reconciliation ----
FEED_ALIGN_TO_SPOT = True
FEED_BASIS_WARN_POINTS = 2.0
FEED_BASIS_MAX_POINTS = 50.0

# ---- Volume-Trend Order Block Engine [BigBeluga] (5m only) ----
OBE_ENTRY_MODE = "video"
OBE_STRATEGIES = ("OB Engine 5m",)
OBE_TIMEFRAMES = {"OB Engine 5m": "5m"}
OBE_ST_LENGTH = 50
OBE_ST_MULT = 2.5
OBE_PIVOT = 5
OBE_DELETE_ON_BREAK = True
OBE_MIN_BUY_PCT = 50.0
OBE_MIN_SELL_PCT = 50.0
OBE_SL_POINTS = 7.0
OBE_TP_POINTS = 6.0
OBE_MACD_FAST = 13
OBE_MACD_SLOW = 34
OBE_MACD_NEAR_MIN = 1.0
OBE_MACD_NEAR_MAX = 3.0
OBE_MACD_NEAR_FRACTION = 0.50

# ---- Dynamic Grid [BigBeluga] ----
DG_ENABLED = True
DG_HMA_LENGTH = 150
DG_ATR_LENGTH = 100
DG_ATR_MULT = 1.6
DG_NUM_LEVELS = 3
DG_MIN_GAP_BARS = 10
DG_CONFIRM_WINDOW_BARS = 3
DG_REQUIRE_TREND = True
DG_MODE = "both"
DG_MIN_REVERSION_LEVEL = 2

OBE_BAR_MS = {"5m": 300_000}
OBE_FEED_BARS = {"5m": 800}
OBE_MAX_AGE_BARS = {"5m": 5}
OBE_MAX_FEED_LAG_MIN = {"5m": 12.0}
OBE_MAX_SIGNAL_AGE_MIN = {"5m": 30.0}
OBE_FEED_SOURCE = {}
OBE_CLOCK = None
OBE_USE_1H_BIAS = False
OBE_USE_GLOBAL_LIMIT = False
OBE_PLAN = {}

# ---- SMA14 + EMA28 (TradingView: SMA length 14, Close, Offset 0, smoothing EMA 28) ----
SMAX_PERIOD = 14
SMAX_SMOOTH_PERIOD = 28
SMAX_SLOPE_LOOKBACK = 3      # bars used to measure SMA14 slope
SMAX_MIN_SMA_SLOPE = 0.30    # min SMA14 slope (pts/bar) in signal direction
SMAX_EMA_SLOPE_LOOKBACK = 5  # bars used to measure EMA28 slope
SMAX_MIN_EMA_SLOPE = -0.15   # EMA28 must not still be running hard against the cross (it lags, so slightly negative is normal)
SMAX_CHOP_LOOKBACK = 20      # no other SMA/EMA cross in this many bars (rejects red-circle chop)
SMAX_MIN_CLOSE_SEP = 1.0     # close must be this far beyond EMA28 in signal direction
SMAX_COOLDOWN_BARS = 3

# ---- HalfTrend Long/Short Engine ----
HT_ENABLED = True
HT_AMPLITUDE = 12
HT_CHANNEL_DEVIATION = 2.0
HT_ATR_LENGTH = 100
HT_SL_POINTS = 10.0
HT_TP_POINTS = 8.0

# ---- Per-strategy fixed settings / filters ----
NO_BIAS_STRATEGIES = {"SMA6", "SMA8", "HalfTrend", "MSB-OB"}
STRATEGY_SL_TP_OVERRIDE = {
    "SMA6": (10.0, 8.0),
    "SMA8": (10.0, 8.0),
    "HalfTrend": (10.0, 8.0),
    "MSB-OB": (10.0, 10.0),
}

# ---- Market Structure Break & Order Block (MSB-OB) ----
MSBOB_ENABLED = True
MSBOB_ZIGZAG_LEN = 9
MSBOB_FIB_FACTOR = 0.35
MSBOB_SL_POINTS = 10.0
MSBOB_TP_POINTS = 10.0

STRATEGY_ATR_CONFIG = {
    "VWAP+VP":         {"sl_mult": 1.2, "tp1_ratio": 0.75, "tp2_ratio": 1.25},
    "EMA+RSI":         {"sl_mult": 1.2, "tp1_ratio": 0.75, "tp2_ratio": 1.25},
    "Liquidity Sweep": {"sl_mult": 1.4, "tp1_ratio": 0.80, "tp2_ratio": 1.35},
    "SMA6":            {"sl_mult": 1.2, "tp1_ratio": 0.75, "tp2_ratio": 1.25},
    "SMA8":            {"sl_mult": 1.2, "tp1_ratio": 0.75, "tp2_ratio": 1.25},
    "MA4/45 Pullback": {"sl_mult": 1.0, "tp1_ratio": 1.0, "tp2_ratio": 1.375},
}

# ---- MA4/45 Pullback ----
MA_FAST_PERIOD = 4
MA_SLOW_PERIOD = 45
MA_SLOPE_LOOKBACK = 10
MA_SLOPE_THRESHOLD = 3.0
MA_CONFIRM_WINDOW = 3
MA_FIXED_SL = 8.0
MA_FIXED_TP1 = 8.0
MA_FIXED_TP2 = 11.0

# ---- MA3/25 Touch-Cross ----
MA2_PERIOD = 3
MA148_PERIOD = 25
MA2148_FIXED_SL = 7.0
MA2148_FIXED_TP1 = 7.0
MA2148_FIXED_TP2 = 11.0

# ---- SMA18 Touch ----
SMA18_PERIOD = 18
SMA18_FIXED_SL = 7.0
SMA18_FIXED_TP1 = 7.0
SMA18_FIXED_TP2 = 11.0
SMA18_SLOPE_LOOKBACK = 3
SMA18_MIN_SLOPE = 0.10        # avg SMA18 slope over 3 bars, pts/bar
SMA18_LAST_SLOPE_MIN = 0.05   # SMA18 must still be moving in signal direction on the LAST bar (rejects a line that is rolling over)
SMA18_MIN_CLOSE_MARGIN = 0.5  # close must be this far beyond SMA18

# ---- Volume Profile ----
VP_STRATEGIES = ("VP POC Bounce", "VP Reversal", "VP Breakout")
VP_NUM_ROWS = 60
VP_VALUE_AREA_PCT = 0.70
VP_MIN_RANGE = 4.0
VP_DAY_START_UTC_HOUR = 22
VP_MIN_SESSION_BARS = 150
VP_OUTSIDE_BUFFER = 0.50
VP_TOUCH_TOL = 0.60
VP_BREAKOUT_LOOKBACK = 60
VP_BREAKOUT_MIN_DIST = 2.0
VP_RETEST_TOL = 2.0
VP_HOLD_TOL = 0.5
VP_SL_BUFFER = 0.5
VP_RR = 2.0
VP_MAX_SL = MAX_SL_POINTS
VP_REFIRE_SECONDS = 3600
VP_PLAN = {}

CLOSED_CANDLE_ENTRY_STRATEGIES = {
    "MA3/25 Cross", "SMA14/EMA28", "Dynamic Grid", "HalfTrend", "MSB-OB",
    *VP_STRATEGIES,
}

MA_CROSS_SLOPE_LOOKBACK = 3
MA_CROSS_MIN_SLOPE = 0.02
MA_CROSS_TOUCH_TOLERANCE = 0.05

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
SWING_LOOKBACK = 12
SWEEP_TOLERANCE = 1.5
OB_LOOKBACK = 20
OB_IMPULSE_MULT = 1.8
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
    if isinstance(trades, list):
        changed = False
        for t in trades:
            if not isinstance(t, dict):
                continue
            if "tp1_hit" not in t:
                t["tp1_hit"] = False
                changed = True
            if t.get("pnl_points") is None:
                t["pnl_points"] = 0.0
                changed = True
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
    if not isinstance(state.get("last_signal_time"), dict):
        state["last_signal_time"] = {}
    if not isinstance(state.get("last_signal_direction"), dict):
        state["last_signal_direction"] = {}
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


def open_trade(direction, entry, sl, tp1, tp2, strategy, info="", breakeven_enabled=True,
               single_target=False, extra=None):
    trades = load_trades()
    new_trade = {
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
        "breakeven_enabled": bool(breakeven_enabled),
        "single_target": bool(single_target),
    }
    if extra:
        new_trade.update(extra)
    trades.append(new_trade)
    save_trades(trades)


def resolve_obe_trades(res_candles, bar_ms):
    if not res_candles:
        return
    trades = load_trades()
    for t in trades:
        if t.get("status") != "open" or t.get("strategy") not in OBE_STRATEGIES:
            continue
        from_ms = t.get("scan_from_ms")
        if from_ms is None:
            continue
        long_ = t["direction"] == "long"
        entry, sl_p, tp_p = t["entry"], t["sl_price"], t["tp1_price"]
        for c in res_candles:
            if c["open_time"] < from_ms:
                continue
            hit_sl = c["low"] <= sl_p if long_ else c["high"] >= sl_p
            hit_tp = c["high"] >= tp_p if long_ else c["low"] <= tp_p
            if not (hit_sl or hit_tp):
                continue
            if hit_sl:
                t["pnl_points"] = -price_to_points(abs(entry - sl_p))
                result = "loss"
            else:
                t["pnl_points"] = price_to_points(abs(tp_p - entry))
                result = "win"
            closed_at = datetime.fromtimestamp(
                (c["open_time"] + bar_ms) / 1000, tz=timezone.utc
            ).isoformat()
            t.update(status="closed", result=result, closed_at=closed_at)
            save_trades(trades)
            notify_result(t)
            break


def resolve_closed_candle_trades(res_candles, bar_ms):
    if not res_candles:
        return
    trades = load_trades()
    changed = False
    for t in trades:
        if t.get("status") != "open":
            continue
        if t.get("strategy") not in CLOSED_CANDLE_ENTRY_STRATEGIES:
            continue
        from_ms = t.get("scan_from_ms")
        if from_ms is None:
            from_ms = res_candles[-1].get("open_time", 0) + bar_ms
        long_ = t.get("direction") == "long"
        entry = float(t["entry"])
        sl_p = float(t["sl_price"])
        tp_p = float(t["tp1_price"])

        for c in res_candles:
            if c.get("open_time", 0) < from_ms:
                continue
            hit_sl = float(c["low"]) <= sl_p if long_ else float(c["high"]) >= sl_p
            hit_tp = float(c["high"]) >= tp_p if long_ else float(c["low"]) <= tp_p
            if not (hit_sl or hit_tp):
                continue
            if hit_sl:
                t["pnl_points"] = -price_to_points(abs(entry - sl_p))
                result = "loss"
            else:
                t["pnl_points"] = price_to_points(abs(tp_p - entry))
                result = "win"
            closed_at = datetime.fromtimestamp(
                (c["open_time"] + bar_ms) / 1000, tz=timezone.utc
            ).isoformat()
            t.update(status="closed", result=result, closed_at=closed_at)
            changed = True
            notify_result(t)
            break
    if changed:
        save_trades(trades)


def check_open_trades(price):
    trades = load_trades()
    changed = False

    for t in trades:
        if t.get("status") != "open":
            continue
        if t.get("strategy") in CLOSED_CANDLE_ENTRY_STRATEGIES:
            continue

        if t.get("pnl_points") is None:
            t["pnl_points"] = 0.0

        direction = t["direction"]
        entry = t["entry"]

        if direction == "long":
            if t.get("single_target", False) and price >= t["tp1_price"]:
                t["pnl_points"] = price_to_points(t["tp1_price"] - entry)
                t.update(status="closed", result="win", closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                save_trades(trades)
                notify_result(t)
            elif not t.get("tp1_hit", False) and price >= t["tp1_price"]:
                t["tp1_hit"] = True
                if t.get("breakeven_enabled", True):
                    t["sl_price"] = round(entry + BE_BUFFER, 2)
                t["pnl_points"] += price_to_points((t["tp1_price"] - entry) * 0.5)
                changed = True
                save_trades(trades)
            elif price >= t["tp2_price"]:
                runner_mult = 0.5 if t.get("tp1_hit", False) else 1.0
                t["pnl_points"] += price_to_points((t["tp2_price"] - entry) * runner_mult)
                t.update(status="closed", result="win", closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                save_trades(trades)
                notify_result(t)
            elif price <= t["sl_price"]:
                t["pnl_points"] = -price_to_points(entry - t["sl_price"])
                t.update(status="closed", result="loss", closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                save_trades(trades)
                notify_result(t)
        else:
            if t.get("single_target", False) and price <= t["tp1_price"]:
                t["pnl_points"] = price_to_points(entry - t["tp1_price"])
                t.update(status="closed", result="win", closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                save_trades(trades)
                notify_result(t)
            elif not t.get("tp1_hit", False) and price <= t["tp1_price"]:
                t["tp1_hit"] = True
                if t.get("breakeven_enabled", True):
                    t["sl_price"] = round(entry - BE_BUFFER, 2)
                t["pnl_points"] += price_to_points((entry - t["tp1_price"]) * 0.5)
                changed = True
                save_trades(trades)
            elif price <= t["tp2_price"]:
                runner_mult = 0.5 if t.get("tp1_hit", False) else 1.0
                t["pnl_points"] += price_to_points((entry - t["tp2_price"]) * runner_mult)
                t.update(status="closed", result="win", closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                save_trades(trades)
                notify_result(t)
            elif price >= t["sl_price"]:
                t["pnl_points"] = -price_to_points(t["sl_price"] - entry)
                t.update(status="closed", result="loss", closed_at=datetime.now(timezone.utc).isoformat())
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
    closed = [t for t in trades if t.get("status") == "closed" and t.get("strategy") == strategy]
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
        f"Result: {t['result'].upper()} | {int(t['pnl_points']):+d} points\n"
        f"This strategy: {s_wins}W / {s_losses}L | WR {s_wr:.1f}% | Net {s_points:+.0f} points\n"
        f"All strategies: {wins}W / {losses}L | WR {wr:.1f}% | Net {net_points:+.0f} points"
    )
    send_telegram(msg)


def send_daily_summary(for_date):
    trades = load_trades()
    day_str = for_date.isoformat()
    day_trades = [t for t in trades if t.get("status") == "closed" and t.get("closed_at", "")[:10] == day_str]

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
        for s, v in sorted(by.items())
    )

    send_telegram(
        f"<b>📊 Daily Summary — {day_str}</b>\n"
        f"Total: {len(day_trades)} | {len(wins)}W/{len(losses)}L | WR {wr:.1f}% | Net {int(total):+d} points\n\n{lines}"
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
        "SMA6", "SMA8", "MA4/45 Pullback",
        "MA3/25 Cross", "SMA18 Touch", "SMA14/EMA28", "HalfTrend", "MSB-OB",
        "VP POC Bounce", "VP Reversal", "VP Breakout",
        "OB Engine 5m",
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
        f"Total: {len(week_trades)} | {len(wins)}W/{len(losses)}L | WR {wr:.1f}% | Net {int(total):+d} points\n\n" + "\n".join(lines)
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
    except Exception:
        pass

    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get("https://data-asg.goldprice.org/dbXRates/USD", headers=headers, timeout=8)
        return float(r.json()["items"][0]["xauPrice"])
    except Exception:
        pass

    try:
        r = requests.get("https://api.gold-api.com/price/XAU", headers=headers, timeout=8)
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
            ts, o, h, lo, c = int(p["t"]), float(p["o"]), float(p["h"]), float(p["l"]), float(p["c"])
            v = float(p.get("v") or 0.0)
            if min(o, h, lo, c) <= 0:
                continue
            candles.append({
                "open_time": ts * 1000,
                "open": o, "high": h, "low": lo, "close": c,
                "volume": max(v, 0.0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda x: x["open_time"])
    return candles


def _get_xauusd_m5_chart(limit=LOOKBACK_BARS):
    data = fetch_xaus_json("/api/v1/chart", {"symbol": "xau", "range": "5d", "interval": "5m"}, timeout=20)
    candles = _parse_xaus_chart_points(data)
    if len(candles) < min(limit, 30):
        raise RuntimeError(f"XAUS returned only {len(candles)} XAUUSD M5 candles")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    current_bucket_ms = (now_ms // (5 * 60 * 1000)) * (5 * 60 * 1000)
    candles = [c for c in candles if c["open_time"] < current_bucket_ms]
    if len(candles) < min(limit, 30):
        raise RuntimeError("Not enough completed XAUUSD M5 candles after removing the live candle")
    return candles[-limit:]


def _get_xauusd_m5_intraday_fallback(limit=LOOKBACK_BARS):
    data = fetch_xaus_json("/api/v1/intraday", {"symbol": "xau", "hours": 48}, timeout=20)
    points = data.get("points") if isinstance(data, dict) else None
    if not isinstance(points, list):
        raise RuntimeError("XAUS intraday response has no points")
    buckets = {}
    for p in points:
        try:
            ts, price = int(p["t"]), float(p["p"])
            if price <= 0:
                continue
            bucket = (ts // 300) * 300
            b = buckets.get(bucket)
            if b is None:
                buckets[bucket] = {"open_time": bucket * 1000, "open": price, "high": price, "low": price, "close": price, "volume": 0.0}
            else:
                b["high"], b["low"], b["close"] = max(b["high"], price), min(b["low"], price), price
        except (KeyError, TypeError, ValueError):
            continue
    candles = [buckets[k] for k in sorted(buckets)]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    current_bucket_ms = (now_ms // (5 * 60 * 1000)) * (5 * 60 * 1000)
    candles = [c for c in candles if c["open_time"] < current_bucket_ms]
    if len(candles) < min(limit, 30):
        raise RuntimeError(f"XAUS intraday fallback returned only {len(candles)} M5 candles")
    return candles[-limit:]


def _get_xauusd_m5_twelvedata(limit=LOOKBACK_BARS):
    if not TWELVEDATA_API_KEY:
        raise RuntimeError("TWELVEDATA_API_KEY not set, skipping TwelveData fallback")
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": "XAU/USD", "interval": "5min", "outputsize": min(limit, 5000), "apikey": TWELVEDATA_API_KEY, "timezone": "UTC"},
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
            dt = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            candles.append({
                "open_time": int(dt.timestamp() * 1000),
                "open": float(v["open"]), "high": float(v["high"]), "low": float(v["low"]), "close": float(v["close"]),
                "volume": float(v.get("volume") or 0.0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda x: x["open_time"])
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    current_bucket_ms = (now_ms // (5 * 60 * 1000)) * (5 * 60 * 1000)
    candles = [c for c in candles if c["open_time"] < current_bucket_ms]
    if len(candles) < min(limit, 30):
        raise RuntimeError(f"TwelveData returned only {len(candles)} usable M5 candles")
    return candles[-limit:]


def sanitize_candles(candles, max_pct_jump=0.03):
    if not candles:
        return candles
    cleaned = [candles[0]]
    for c in candles[1:]:
        prev_close = cleaned[-1]["close"]
        if prev_close and abs(c["close"] - prev_close) / prev_close > max_pct_jump:
            continue
        cleaned.append(c)
    return cleaned


def reconcile_feed_to_spot(candles, spot):
    if not candles or spot is None:
        return candles, 0.0
    chart_level = candles[-1]["close"]
    for c in reversed(candles):
        if c.get("volume", 0) > 0 and c.get("high") != c.get("low"):
            chart_level = c["close"]
            break
    basis = chart_level - spot
    if abs(basis) < 1e-6:
        return candles, basis
    aligned = []
    for c in candles:
        aligned.append({
            "open_time": c["open_time"],
            "open": round(c["open"] - basis, 4),
            "high": round(c["high"] - basis, 4),
            "low": round(c["low"] - basis, 4),
            "close": round(c["close"] - basis, 4),
            "volume": c["volume"],
        })
    return aligned, basis


def get_klines(limit=LOOKBACK_BARS):
    try:
        candles = sanitize_candles(_get_xauusd_m5_chart(limit))
        return candles, "xaus_chart"
    except Exception:
        pass
    try:
        candles = sanitize_candles(_get_xauusd_m5_intraday_fallback(limit))
        return candles, "xaus_intraday"
    except Exception:
        pass
    try:
        candles = sanitize_candles(_get_xauusd_m5_twelvedata(limit))
        return candles, "twelvedata"
    except Exception:
        return None, None


# ================= INDICATORS =================
def ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    out = [None] * (period - 1)
    out.append(sum(values[:period]) / period)
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
    ag, al = sum(gains) / period, sum(losses) / period
    out.append(100 if al == 0 else 100 - (100 / (1 + ag / al)))
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(ch, 0)) / period
        al = (al * (period - 1) + max(-ch, 0)) / period
        out.append(100 if al == 0 else 100 - (100 / (1 + ag / al)))
    return out


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    return statistics.mean(trs[-period:])


def session_vwap(candles):
    vals, cum_pv, cum_vol, day = [], 0.0, 0.0, None
    for c in candles:
        d = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc).date()
        if d != day:
            day, cum_pv, cum_vol = d, 0.0, 0.0
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
    return "rising" if d > 0.05 else ("falling" if d < -0.05 else "flat")


def volume_profile(candles, bins=10):
    highs, lows = [c["high"] for c in candles], [c["low"] for c in candles]
    mx, mn = max(highs), min(lows)
    if mx == mn:
        return None
    size = (mx - mn) / bins
    vols = [0.0] * bins
    for c in candles:
        idx = max(0, min(bins - 1, int((c["close"] - mn) / size)))
        vols[idx] += c["volume"] if c["volume"] > 0 else 1.0
    poc_i = vols.index(max(vols))
    poc = mn + (poc_i + 0.5) * size
    target = sum(vols) * 0.70
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
    return {"poc": round(poc, 2), "vah": round(mn + (hi + 1) * size, 2), "val": round(mn + lo * size, 2)}


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
        bucket = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
        groups[bucket] = c["close"]
    hourly_closes = [groups[k] for k in sorted(groups)]
    if len(hourly_closes) < ema_period:
        return "neutral"
    e_1h = ema(hourly_closes, ema_period)
    if e_1h[-1] is None:
        return "neutral"
    current_price = hourly_closes[-1]
    return "bullish" if current_price > e_1h[-1] else ("bearish" if current_price < e_1h[-1] else "neutral")


# ================= STRATEGIES =================
def is_rejection(c, level, direction):
    total_range = c["high"] - c["low"]
    if total_range <= 0:
        return False
    body_high = max(c["open"], c["close"])
    body_low = min(c["open"], c["close"])
    if direction == "long":
        return ((body_low - c["low"]) / total_range >= 0.25) and (c["close"] >= c["open"])
    return ((c["high"] - body_high) / total_range >= 0.25) and (c["close"] <= c["open"])


def vol_ok(candles, idx):
    if idx < 10:
        return False
    vols = [c["volume"] for c in candles[idx - 10:idx]]
    return True if sum(vols) == 0 else candles[idx]["volume"] >= statistics.mean(vols) * VOLUME_MULT


def check_vwap_vp(candles, vwap, profile):
    if not profile:
        return None, None
    last = candles[-1]
    slope = vwap_slope(vwap)
    for name, price in [("POC", profile["poc"]), ("VAH", profile["vah"]), ("VAL", profile["val"])]:
        if abs(last["close"] - price) > TOUCH_TOLERANCE:
            continue
        if slope == "rising" and is_rejection(last, price, "long") and vol_ok(candles, len(candles) - 1):
            return "long", name
        if slope == "falling" and is_rejection(last, price, "short") and vol_ok(candles, len(candles) - 1):
            return "short", name
    return None, None


def check_liquidity_sweep(candles):
    if len(candles) < SWING_LOOKBACK + 3:
        return None, None
    window = candles[-(SWING_LOOKBACK + 1):-1]
    sh, sl = max(c["high"] for c in window), min(c["low"] for c in window)
    last = candles[-1]
    if last["low"] < sl - SWEEP_TOLERANCE and last["close"] > sl and last["close"] > last["open"]:
        return "long", f"Sweep Low {sl:.2f}"
    if last["high"] > sh + SWEEP_TOLERANCE and last["close"] < sh and last["close"] < last["open"]:
        return "short", f"Sweep High {sh:.2f}"
    return None, None


def check_ema_rsi(candles):
    closes = [c["close"] for c in candles]
    if len(closes) < max(EMA_SLOW, RSI_PERIOD) + 5:
        return None, None
    ef, es, r = ema(closes, EMA_FAST), ema(closes, EMA_SLOW), rsi(closes, RSI_PERIOD)
    if None in (ef[-1], es[-1], r[-1], ef[-2], es[-2]):
        return None, None
    if ef[-2] <= es[-2] and ef[-1] > es[-1] and r[-1] > 50:
        return "long", f"EMA Cross RSI {r[-1]:.1f}"
    if ef[-2] >= es[-2] and ef[-1] < es[-1] and r[-1] < 50:
        return "short", f"EMA Cross RSI {r[-1]:.1f}"
    return None, None


# ---- Generic SMA Cross Engine (TradingView Offset: 1) ----
def _check_sma_cross(candles, length, offset=1, strategy_name="SMA"):
    need = length + offset + 3
    if len(candles) < need:
        return None, None

    closes = [c["close"] for c in candles]
    n = len(closes)

    raw_sma = [None] * n
    for i in range(length - 1, n):
        raw_sma[i] = sum(closes[i - length + 1:i + 1]) / length

    last_idx = n - 1
    prev_idx = n - 2

    cur_line_idx = last_idx - offset
    prev_line_idx = prev_idx - offset

    if cur_line_idx < 0 or prev_line_idx < 0:
        return None, None

    current_sma = raw_sma[cur_line_idx]
    prev_sma = raw_sma[prev_line_idx]

    if current_sma is None or prev_sma is None:
        return None, None

    current_close = closes[last_idx]
    prev_close = closes[prev_idx]

    if (
        prev_close <= prev_sma
        and current_close > current_sma
        and current_sma > prev_sma
    ):
        return "long", f"{strategy_name} Cross Up @ {current_close:.2f} (SMA{length} line {current_sma:.2f}, offset {offset})"

    if (
        prev_close >= prev_sma
        and current_close < current_sma
        and current_sma < prev_sma
    ):
        return "short", f"{strategy_name} Cross Down @ {current_close:.2f} (SMA{length} line {current_sma:.2f}, offset {offset})"

    return None, None


def check_sma6(candles):
    """SMA 6 (Source: Close, Offset: 1) on completed M5 candles."""
    return _check_sma_cross(candles, length=6, offset=1, strategy_name="SMA6")


def check_sma8(candles):
    """SMA 8 (Source: Close, Offset: 1) on completed M5 candles."""
    return _check_sma_cross(candles, length=8, offset=1, strategy_name="SMA8")


def _wma(values, period):
    out = [None] * len(values)
    if period <= 0:
        return out
    denom = period * (period + 1) / 2.0
    for i in range(period - 1, len(values)):
        total = sum(values[i - period + 1 + j] * (j + 1) for j in range(period))
        out[i] = total / denom
    return out


def _hma(values, period):
    if period < 2:
        return list(values)
    half = max(1, period // 2)
    root = max(1, int(round(period ** 0.5)))
    w_half = _wma(values, half)
    w_full = _wma(values, period)
    raw = [None] * len(values)
    for i in range(len(values)):
        if w_half[i] is not None and w_full[i] is not None:
            raw[i] = 2.0 * w_half[i] - w_full[i]
    valid = [x for x in raw if x is not None]
    h_valid = _wma(valid, root)
    out = [None] * len(values)
    first = next((i for i, x in enumerate(raw) if x is not None), None)
    if first is None:
        return out
    for j, x in enumerate(h_valid):
        if x is not None:
            out[first + j] = x
    return out


def _wilder_atr(candles, period):
    n = len(candles)
    tr = [None] * n
    for i, c in enumerate(candles):
        if i == 0:
            tr[i] = c["high"] - c["low"]
        else:
            tr[i] = max(
                c["high"] - c["low"],
                abs(c["high"] - candles[i - 1]["close"]),
                abs(c["low"] - candles[i - 1]["close"]),
            )
    out = [None] * n
    if n < period:
        return out
    first = sum(tr[:period]) / period
    out[period - 1] = first
    for i in range(period, n):
        out[i] = ((out[i - 1] * (period - 1)) + tr[i]) / period
    return out


def halftrend_compute(candles, amplitude=None, dev_mult=None):
    amplitude = amplitude or HT_AMPLITUDE
    dev_mult = HT_CHANNEL_DEVIATION if dev_mult is None else dev_mult
    n = len(candles)
    highs, lows, closes = [c["high"] for c in candles], [c["low"] for c in candles], [c["close"] for c in candles]
    atr_vals = _wilder_atr(candles, HT_ATR_LENGTH)
    trend_list = [0] * n
    line_list = [None] * n
    signal = [None] * n
    if n == 0:
        return {"trend": trend_list, "line": line_list, "signal": signal}
    trend, next_trend = 0, 0
    max_low, min_high = lows[0], highs[0]
    up, down, prev_trend = None, None, None
    for i in range(n):
        start = max(0, i - amplitude + 1)
        high_price, low_price = max(highs[start:i + 1]), min(lows[start:i + 1])
        highma = sum(highs[start:i + 1]) / (i + 1 - start)
        lowma = sum(lows[start:i + 1]) / (i + 1 - start)
        prev_low = lows[i - 1] if i > 0 else lows[i]
        prev_high = highs[i - 1] if i > 0 else highs[i]
        atr2 = atr_vals[i] / 2 if atr_vals[i] is not None else None

        if next_trend == 1:
            max_low = max(low_price, max_low)
            if highma < max_low and closes[i] < prev_low:
                trend, next_trend, min_high = 1, 0, high_price
        else:
            min_high = min(high_price, min_high)
            if lowma > min_high and closes[i] > prev_high:
                trend, next_trend, max_low = 0, 1, low_price

        arrow = False
        if trend == 0:
            up = down if (prev_trend is not None and prev_trend != 0 and down is not None) else (max_low if up is None else max(max_low, up))
            line_list[i] = up
            if atr2 is not None and prev_trend == 1:
                signal[i] = "long"
        else:
            down = up if (prev_trend is not None and prev_trend != 1 and up is not None) else (min_high if down is None else min(min_high, down))
            line_list[i] = down
            if atr2 is not None and prev_trend == 0:
                signal[i] = "short"
        trend_list[i] = trend
        prev_trend = trend
    return {"trend": trend_list, "line": line_list, "signal": signal}


def check_halftrend(candles, state):
    if not HT_ENABLED or len(candles) < HT_ATR_LENGTH + HT_AMPLITUDE + 5:
        return None, None
    ht = halftrend_compute(candles)
    last = len(candles) - 1
    direction = ht["signal"][last]
    if not direction:
        return None, None
    key = f"HT-{direction}-{candles[last].get('open_time', last)}"
    if zone_already_fired(state, key):
        return None, None
    mark_zone_fired(state, key)
    close, line = candles[last]["close"], ht["line"][last]
    side = "Up" if direction == "long" else "Down"
    return direction, f"HalfTrend Flip {side} @ {close:.2f} (line {line:.2f})"


# ---- MSB-OB (Market Structure Break) Engine Matching Circled Candle ----
def msbob_compute(candles):
    """
    Market Structure Break (MSB) engine matching TradingView:
    - ZigZag: MSBOB_ZIGZAG_LEN (9), Fib confirmation factor: MSBOB_FIB_FACTOR (0.5)
    - Bullish MSB (BUY): Prior closed candle <= swing high line, and current
      closed candle breaks and closes strictly ABOVE swing high line (the exact green marked candle).
    - Bearish MSB (SELL): Prior closed candle >= swing low line, and current
      closed candle breaks and closes strictly BELOW swing low line.
    """
    n = len(candles)
    z = MSBOB_ZIGZAG_LEN
    if n < z * 3 + 10:
        return None

    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]

    high_pivots = []
    low_pivots = []
    trend = 1
    market = 1
    last_to_up = None
    last_to_down = None
    signal = None

    for i in range(n):
        start = max(0, i - z + 1)
        to_up = highs[i] >= max(highs[start:i + 1])
        to_down = lows[i] <= min(lows[start:i + 1])

        prev_trend = trend
        if trend == 1 and to_down:
            trend = -1
        elif trend == -1 and to_up:
            trend = 1

        up_since = 1 if last_to_up is None else max(1, i - last_to_up)
        down_since = 1 if last_to_down is None else max(1, i - last_to_down)

        low_val = min(lows[max(0, i - up_since + 1):i + 1])
        low_index = next((j for j in range(i, max(0, i - up_since + 1) - 1, -1) if abs(lows[j] - low_val) <= 1e-12), i)
        high_val = max(highs[max(0, i - down_since + 1):i + 1])
        high_index = next((j for j in range(i, max(0, i - down_since + 1) - 1, -1) if abs(highs[j] - high_val) <= 1e-12), i)

        if trend != prev_trend:
            if trend == 1:
                low_pivots.append((low_val, low_index))
                if len(low_pivots) > 5:
                    low_pivots.pop(0)
            else:
                high_pivots.append((high_val, high_index))
                if len(high_pivots) > 5:
                    high_pivots.pop(0)

        # MSB line = latest confirmed swing pivot. Signal fires on the CLOSE of the first
        # candle that closes beyond the line, confirmed by Fib factor (candle extreme must
        # pass the line by FIB x swing range).
        if high_pivots and low_pivots and i > 0:
            h_line = high_pivots[-1][0]
            l_line = low_pivots[-1][0]
            rng = abs(h_line - l_line)
            fib_dist = rng * MSBOB_FIB_FACTOR
            if market != 1 and closes[i] > h_line and closes[i - 1] <= h_line and highs[i] >= h_line + fib_dist:
                market = 1
                signal = {"direction": "long", "index": i, "level": h_line, "pivot_low": l_line}
            elif market != -1 and closes[i] < l_line and closes[i - 1] >= l_line and lows[i] <= l_line - fib_dist:
                market = -1
                signal = {"direction": "short", "index": i, "level": l_line, "pivot_high": h_line}

        last_to_up = i if to_up else last_to_up
        last_to_down = i if to_down else last_to_down

    return signal


def check_msb_ob(candles, state):
    if not MSBOB_ENABLED:
        return None, None

    sig = msbob_compute(candles)
    if not sig:
        return None, None

    idx = sig["index"]
    last_idx = len(candles) - 1

    # Catch breakout on the exact closed bar or within a 1-candle grace window
    if (last_idx - idx) > 1:
        return None, None

    candle_key = candles[idx].get("open_time", idx)
    direction = sig["direction"]
    key = f"MSBOB-{direction}-{candle_key}"
    if zone_already_fired(state, key):
        return None, None
    mark_zone_fired(state, key)

    side = "Bullish MSB / BUY" if direction == "long" else "Bearish MSB / SELL"
    return direction, f"{side} | ZigZag {MSBOB_ZIGZAG_LEN} | Fib {MSBOB_FIB_FACTOR:g} | Break Level {sig['level']:.2f}"


def check_ma_pullback(candles):
    need = MA_SLOW_PERIOD + MA_SLOPE_LOOKBACK + MA_CONFIRM_WINDOW + 2
    if len(candles) < need:
        return None, None
    closes, opens = [c["close"] for c in candles], [c["open"] for c in candles]
    n = len(closes)
    ma_fast = [None] * n
    ma_slow = [None] * n
    for i in range(MA_FAST_PERIOD - 1, n):
        ma_fast[i] = sum(closes[i - MA_FAST_PERIOD + 1:i + 1]) / MA_FAST_PERIOD
    for i in range(MA_SLOW_PERIOD - 1, n):
        ma_slow[i] = sum(closes[i - MA_SLOW_PERIOD + 1:i + 1]) / MA_SLOW_PERIOD

    last = n - 1
    if ma_slow[last] is None:
        return None, None
    bull = closes[last] > opens[last] and closes[last] > ma_slow[last]
    bear = closes[last] < opens[last] and closes[last] < ma_slow[last]
    if not (bull or bear):
        return None, None

    for back in range(1, MA_CONFIRM_WINDOW + 1):
        i = last - back
        if i - MA_SLOPE_LOOKBACK < 0 or None in (ma_fast[i], ma_fast[i - 1], ma_slow[i], ma_slow[i - 1], ma_slow[i - MA_SLOPE_LOOKBACK]):
            continue
        slope = ma_slow[i] - ma_slow[i - MA_SLOPE_LOOKBACK]
        if bull and slope > MA_SLOPE_THRESHOLD and ma_fast[i - 1] > ma_slow[i - 1] and ma_fast[i] <= ma_slow[i]:
            return "long", f"MA4/45 Pullback (touch {back} bars ago)"
        if bear and slope < -MA_SLOPE_THRESHOLD and ma_fast[i - 1] < ma_slow[i - 1] and ma_fast[i] >= ma_slow[i]:
            return "short", f"MA4/45 Pullback (touch {back} bars ago)"
    return None, None


def check_ma2_148_cross(candles, live_price, atr_value=None):
    if len(candles) < MA148_PERIOD + MA_CROSS_SLOPE_LOOKBACK + 2:
        return None, None
    closes, highs, lows, opens = [c["close"] for c in candles], [c["high"] for c in candles], [c["low"] for c in candles], [c["open"] for c in candles]
    slow = [None] * len(closes)
    for i in range(MA148_PERIOD - 1, len(closes)):
        slow[i] = sum(closes[i - MA148_PERIOD + 1:i + 1]) / MA148_PERIOD
    last = len(closes) - 1
    slope_ref = last - MA_CROSS_SLOPE_LOOKBACK
    if slow[last] is None or slope_ref < 0 or slow[slope_ref] is None:
        return None, None
    slope = (slow[last] - slow[slope_ref]) / MA_CROSS_SLOPE_LOOKBACK
    ma25 = slow[last]
    touched = lows[last] <= ma25 + MA_CROSS_TOUCH_TOLERANCE and highs[last] >= ma25 - MA_CROSS_TOUCH_TOLERANCE
    if not touched:
        return None, None
    if closes[last] < ma25 and closes[last] <= opens[last] and slope <= -MA_CROSS_MIN_SLOPE:
        return "short", f"MA3/25 Touch Down @ {closes[last]:.2f} (MA25 {ma25:.2f}, slope {slope:+.2f})"
    if closes[last] > ma25 and closes[last] >= opens[last] and slope >= MA_CROSS_MIN_SLOPE:
        return "long", f"MA3/25 Touch Up @ {closes[last]:.2f} (MA25 {ma25:.2f}, slope {slope:+.2f})"
    return None, None


def check_sma18_touch(candles, live_price):
    if len(candles) < SMA18_PERIOD + SMA18_SLOPE_LOOKBACK + 2:
        return None, None
    closes = [c["close"] for c in candles]
    sma_vals = [None] * len(closes)
    for i in range(SMA18_PERIOD - 1, len(closes)):
        sma_vals[i] = sum(closes[i - SMA18_PERIOD + 1:i + 1]) / SMA18_PERIOD
    last, prev = len(closes) - 1, len(closes) - 2
    slope_ref = last - SMA18_SLOPE_LOOKBACK
    if sma_vals[prev] is None or sma_vals[last] is None or slope_ref < 0 or sma_vals[slope_ref] is None:
        return None, None
    slope = (sma_vals[last] - sma_vals[slope_ref]) / SMA18_SLOPE_LOOKBACK
    cp, cn, sp, sn = closes[prev], closes[last], sma_vals[prev], sma_vals[last]
    ep = live_price if live_price is not None else cn
    last_slope = sn - sp                      # 1-bar SMA move
    bullish_candle = candles[last]["close"] > candles[last]["open"]
    bearish_candle = candles[last]["close"] < candles[last]["open"]
    if (cp <= sp and cn > sn and slope >= SMA18_MIN_SLOPE and last_slope >= SMA18_LAST_SLOPE_MIN
            and cn - sn >= SMA18_MIN_CLOSE_MARGIN and bullish_candle):
        return "long", f"SMA18 Touch Up @ {ep:.2f} (slope {slope:+.2f})"
    if (cp >= sp and cn < sn and slope <= -SMA18_MIN_SLOPE and last_slope <= -SMA18_LAST_SLOPE_MIN
            and sn - cn >= SMA18_MIN_CLOSE_MARGIN and bearish_candle):
        return "short", f"SMA18 Touch Down @ {ep:.2f} (slope {slope:+.2f})"
    return None, None


def _session_date(c):
    t = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc)
    return (t - timedelta(hours=VP_DAY_START_UTC_HOUR)).date()


def vp_session_profile(session_candles, num_rows=VP_NUM_ROWS, va_pct=VP_VALUE_AREA_PCT):
    if not session_candles:
        return None
    lo, hi = min(c["low"] for c in session_candles), max(c["high"] for c in session_candles)
    if hi - lo < VP_MIN_RANGE:
        return None
    row = (hi - lo) / num_rows
    vols = [0.0] * num_rows
    for c in session_candles:
        v = c["volume"] if c["volume"] > 0 else 1.0
        i0 = max(0, min(num_rows - 1, int((c["low"] - lo) / row)))
        i1 = max(i0, min(num_rows - 1, int((c["high"] - lo) / row)))
        share = v / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            vols[i] += share
    poc_i = max(range(num_rows), key=lambda i: vols[i])
    target = sum(vols) * va_pct
    acc, lo_i, hi_i = vols[poc_i], poc_i, poc_i
    while acc < target and (lo_i > 0 or hi_i < num_rows - 1):
        up = sum(vols[hi_i + 1:hi_i + 3]) if hi_i < num_rows - 1 else -1
        dn = sum(vols[max(lo_i - 2, 0):lo_i]) if lo_i > 0 else -1
        if up >= dn:
            take = vols[hi_i + 1:hi_i + 3]
            hi_i += len(take)
            acc += sum(take)
        else:
            start = max(lo_i - 2, 0)
            acc += sum(vols[start:lo_i])
            lo_i = start
    return {"poc": round(lo + (poc_i + 0.5) * row, 2), "vah": round(lo + (hi_i + 1) * row, 2), "val": round(lo + lo_i * row, 2)}


def vp_context(candles):
    if not candles or len(candles) < 20:
        return None
    by_day = {}
    for c in candles:
        by_day.setdefault(_session_date(c), []).append(c)
    today = _session_date(candles[-1])
    today_c = by_day.get(today, [])
    prev_days = sorted(d for d in by_day if d < today and len(by_day[d]) >= VP_MIN_SESSION_BARS)
    if not prev_days or len(today_c) < 8:
        return None
    prev_c = by_day[prev_days[-1]]
    profile = vp_session_profile(prev_c)
    if not profile:
        return None
    prev_close = prev_c[-1]["close"]
    if prev_close > profile["vah"] + VP_OUTSIDE_BUFFER:
        pos = "above"
    elif prev_close < profile["val"] - VP_OUTSIDE_BUFFER:
        pos = "below"
    elif profile["val"] <= prev_close <= profile["vah"]:
        pos = "inside"
    else:
        pos = "edge"
    return {"profile": profile, "prev_close": prev_close, "position": pos, "today": today_c}


def vp_already_fired(state, strategy, direction):
    last = state.get("vp_fired", {}).get(f"{strategy}|{direction}")
    return last is not None and (time.time() - last) < VP_REFIRE_SECONDS


def mark_vp_fired(state, strategy, direction):
    fired = state.setdefault("vp_fired", {})
    fired[f"{strategy}|{direction}"] = time.time()


def vp_set_plan(strategy, direction, entry, sl_ref):
    raw_sl = (entry - sl_ref) if direction == "long" else (sl_ref - entry)
    if raw_sl > VP_MAX_SL:
        return False
    sl_pts = round(max(raw_sl, MIN_SL_POINTS), 2)
    tp_pts = round(sl_pts * VP_RR, 2)
    VP_PLAN[strategy] = (sl_pts, tp_pts, tp_pts)
    return True


def check_vp_poc_bounce(candles, ctx):
    if not ctx or ctx["position"] not in ("above", "below"):
        return None, None
    today = ctx["today"]
    if len(today) < 8:
        return None, None
    poc = ctx["profile"]["poc"]
    c, p = today[-1], today[-2]
    if not (c["low"] <= poc + VP_TOUCH_TOL and c["high"] >= poc - VP_TOUCH_TOL):
        return None, None
    entry = c["close"]
    if ctx["position"] == "above":
        if not (entry > poc and is_rejection(c, poc, "long")):
            return None, None
        if not vp_set_plan("VP POC Bounce", "long", entry, poc - VP_SL_BUFFER):
            return None, None
        return "long", f"POC Bounce long @ POC {poc:.2f}"
    else:
        if not (entry < poc and is_rejection(c, poc, "short")):
            return None, None
        if not vp_set_plan("VP POC Bounce", "short", entry, poc + VP_SL_BUFFER):
            return None, None
        return "short", f"POC Bounce short @ POC {poc:.2f}"


def check_vp_reversal(candles, ctx):
    if not ctx or ctx["position"] != "inside":
        return None, None
    today = ctx["today"]
    if len(today) < 8:
        return None, None
    prof = ctx["profile"]
    c, p = today[-1], today[-2]
    entry = c["close"]
    if p["close"] < prof["val"] and prof["val"] <= entry <= prof["vah"]:
        if not vp_set_plan("VP Reversal", "long", entry, c["low"] - VP_SL_BUFFER):
            return None, None
        return "long", f"VA Reversal back above VAL {prof['val']:.2f}"
    if p["close"] > prof["vah"] and prof["val"] <= entry <= prof["vah"]:
        if not vp_set_plan("VP Reversal", "short", entry, c["high"] + VP_SL_BUFFER):
            return None, None
        return "short", f"VA Reversal back below VAH {prof['vah']:.2f}"
    return None, None


def _vp_breakout_setup(window, level, direction):
    n = len(window)
    long_ = direction == "long"
    k = -1
    for i in range(n - 1, -1, -1):
        cl = window[i]["close"]
        if (cl < level - VP_HOLD_TOL) if long_ else (cl > level + VP_HOLD_TOL):
            k = i
            break
    b = None
    for i in range(k + 1, n):
        cl = window[i]["close"]
        if (cl > level + VP_BREAKOUT_MIN_DIST) if long_ else (cl < level - VP_BREAKOUT_MIN_DIST):
            b = i
            break
    if b is None or n - b < 5:
        return None
    seg = window[b:n - 1]
    if long_:
        h_idx = b + max(range(len(seg)), key=lambda j: seg[j]["high"])
        swing = window[h_idx]["high"]
    else:
        h_idx = b + min(range(len(seg)), key=lambda j: seg[j]["low"])
        swing = window[h_idx]["low"]
    pullback = window[h_idx + 1:n - 1]
    if len(pullback) < 2:
        return None
    c, p = window[-1], window[-2]
    if long_:
        pull_ext = min(x["low"] for x in pullback)
        if pull_ext > level + VP_RETEST_TOL:
            return None
        if not (c["close"] > swing >= p["close"] and c["close"] > c["open"]):
            return None
    else:
        pull_ext = max(x["high"] for x in pullback)
        if pull_ext < level - VP_RETEST_TOL:
            return None
        if not (c["close"] < swing <= p["close"] and c["close"] < c["open"]):
            return None
    return swing


def check_vp_breakout(candles, ctx):
    if not ctx:
        return None, None
    today = ctx["today"]
    if len(today) < 10:
        return None, None
    prof = ctx["profile"]
    window = today[-VP_BREAKOUT_LOOKBACK:]
    entry = window[-1]["close"]
    swing = _vp_breakout_setup(window, prof["vah"], "long")
    if swing is not None:
        if vp_set_plan("VP Breakout", "long", entry, swing - VP_SL_BUFFER):
            return "long", f"VA Breakout: held VAH {prof['vah']:.2f}, BOS above {swing:.2f}"
    swing = _vp_breakout_setup(window, prof["val"], "short")
    if swing is not None:
        if vp_set_plan("VP Breakout", "short", entry, swing + VP_SL_BUFFER):
            return "short", f"VA Breakout: held VAL {prof['val']:.2f}, BOS below {swing:.2f}"
    return None, None


def _sma14_ema28_series(closes):
    n = len(closes)
    sma_vals = [None] * n
    for i in range(SMAX_PERIOD - 1, n):
        sma_vals[i] = sum(closes[i - SMAX_PERIOD + 1:i + 1]) / SMAX_PERIOD
    valid = [x for x in sma_vals if x is not None]
    smooth_valid = ema(valid, SMAX_SMOOTH_PERIOD)   # TradingView SMA "Smoothing: EMA 28"
    smooth = [None] * n
    first = SMAX_PERIOD - 1
    for i in range(first, n):
        smooth[i] = smooth_valid[i - first]
    return sma_vals, smooth


def check_sma14_ema28(candles, live_price=None):
    """Signal only when the SMA14 line crosses the EMA28 line on a CLOSED candle,
    and the cross is not in a flat / tangled (chop) zone."""
    need = SMAX_PERIOD + SMAX_SMOOTH_PERIOD + SMAX_CHOP_LOOKBACK + SMAX_EMA_SLOPE_LOOKBACK + 5
    if len(candles) < need:
        return None, None
    closes = [c["close"] for c in candles]
    sma, sm = _sma14_ema28_series(closes)
    last, prev = len(closes) - 1, len(closes) - 2
    if any(x is None for x in (sma[last], sma[prev], sm[last], sm[prev])):
        return None, None

    up = sma[prev] <= sm[prev] and sma[last] > sm[last]
    dn = sma[prev] >= sm[prev] and sma[last] < sm[last]
    if not (up or dn):
        return None, None

    # chop filter: any other cross of the two lines in the lookback window -> skip
    for i in range(last - SMAX_CHOP_LOOKBACK, last):
        if sma[i] is None or sm[i] is None or sma[i - 1] is None or sm[i - 1] is None:
            return None, None
        if (sma[i - 1] - sm[i - 1]) * (sma[i] - sm[i]) < 0:
            return None, None

    sma_slope = (sma[last] - sma[last - SMAX_SLOPE_LOOKBACK]) / SMAX_SLOPE_LOOKBACK
    ema_slope = (sm[last] - sm[last - SMAX_EMA_SLOPE_LOOKBACK]) / SMAX_EMA_SLOPE_LOOKBACK
    sep = closes[last] - sm[last]
    tag = f"SMA14 {sma[last]:.2f} / EMA28 {sm[last]:.2f}"

    if up and sma_slope >= SMAX_MIN_SMA_SLOPE and ema_slope >= SMAX_MIN_EMA_SLOPE and sep >= SMAX_MIN_CLOSE_SEP:
        return "long", f"SMA14/EMA28 Cross Up ({tag})"
    if dn and sma_slope <= -SMAX_MIN_SMA_SLOPE and ema_slope <= -SMAX_MIN_EMA_SLOPE and sep <= -SMAX_MIN_CLOSE_SEP:
        return "short", f"SMA14/EMA28 Cross Down ({tag})"
    return None, None


def dynamic_grid_compute(candles):
    closes = [c["close"] for c in candles]
    hma_vals = _hma(closes, DG_HMA_LENGTH)
    atr_raw = _wilder_atr(candles, DG_ATR_LENGTH)
    step = [None if a is None else a * DG_ATR_MULT for a in atr_raw]
    n = len(candles)
    raw, level, kind = [None] * n, [None] * n, [None] * n
    last_signal = -10**9
    for i in range(1, n):
        if hma_vals[i] is None or hma_vals[i - 1] is None or step[i] is None or step[i - 1] is None or step[i - 1] <= 0:
            continue
        rising, falling = hma_vals[i] > hma_vals[i - 1], hma_vals[i] < hma_vals[i - 1]
        dist = (closes[i - 1] - hma_vals[i - 1]) / step[i - 1]
        k_low = min(DG_NUM_LEVELS, int(-dist)) if dist <= -1 else 0
        k_up = min(DG_NUM_LEVELS, int(dist)) if dist >= 1 else 0
        green, red = candles[i]["close"] > candles[i]["open"], candles[i]["close"] < candles[i]["open"]
        direction, lvl, sig_kind = None, None, None
        if k_low >= 1 and green:
            aligned = rising
            direction, lvl = "long", k_low
        elif k_up >= 1 and red:
            aligned = falling
            direction, lvl = "short", k_up
        if direction is not None:
            if aligned and DG_MODE in ("trend", "both"):
                sig_kind = "trend"
            elif (not aligned) and DG_MODE in ("reversion", "both") and lvl >= DG_MIN_REVERSION_LEVEL:
                sig_kind = "reversion"
            else:
                direction = None
        if direction is not None and i - last_signal >= DG_MIN_GAP_BARS:
            raw[i], level[i], kind[i], last_signal = direction, lvl, sig_kind, i
    return {"hma": hma_vals, "atr": atr_raw, "step": step, "signal": raw, "level": level, "kind": kind}


def check_dynamic_grid(candles):
    if not DG_ENABLED or len(candles) < max(DG_HMA_LENGTH + 20, DG_ATR_LENGTH + 20):
        return None, None
    grid = dynamic_grid_compute(candles)
    i = len(candles) - 1
    direction, level = grid["signal"][i], grid["level"][i]
    if direction is None or grid["hma"][i] is None:
        return None, None
    side = "BUY" if direction == "long" else "SELL"
    return direction, f"Dynamic Grid {side} | Degree {level} | HMA {grid['hma'][i]:.2f}"


def obe_compute(candles, st_length=None, mult=None, pivot=None, delete_on_break=None, min_buy=None, min_sell=None):
    L = OBE_ST_LENGTH if st_length is None else st_length
    M = OBE_ST_MULT if mult is None else mult
    P = OBE_PIVOT if pivot is None else pivot
    dob = OBE_DELETE_ON_BREAK if delete_on_break is None else delete_on_break
    mb = (OBE_MIN_BUY_PCT if min_buy is None else min_buy) / 100.0
    ms = (OBE_MIN_SELL_PCT if min_sell is None else min_sell) / 100.0

    n = len(candles)
    o, h, lo, cl = [c["open"] for c in candles], [c["high"] for c in candles], [c["low"] for c in candles], [c["close"] for c in candles]
    vols = [max(float(c.get("volume") or 0.0), 0.0) for c in candles]
    if sum(vols) <= 0:
        vols = [1.0] * n

    rng = [h[i] - lo[i] for i in range(n)]
    atr_vals = [None] * n
    run = 0.0
    for i in range(n):
        run += rng[i]
        if i >= L: run -= rng[i - L]
        if i >= L - 1: atr_vals[i] = run / L

    lower, upper, trend = [None] * n, [None] * n, [1] * n
    for i in range(n):
        if atr_vals[i] is None: continue
        src = (h[i] + lo[i]) / 2.0
        up_b, dn_b = src + M * atr_vals[i], src - M * atr_vals[i]
        if i == 0 or lower[i - 1] is None:
            lower[i], upper[i], trend[i] = dn_b, up_b, 1
            continue
        lower[i] = dn_b if (dn_b > lower[i - 1] or cl[i - 1] < lower[i - 1]) else lower[i - 1]
        upper[i] = up_b if (up_b < upper[i - 1] or cl[i - 1] > upper[i - 1]) else upper[i - 1]
        trend[i] = (1 if cl[i] > upper[i] else -1) if trend[i - 1] == -1 else (-1 if cl[i] < lower[i] else 1)

    fast, slow = ema(cl, OBE_MACD_FAST), ema(cl, OBE_MACD_SLOW)
    hist = [None] * n
    for i in range(n):
        if fast[i] is not None and slow[i] is not None:
            hist[i] = fast[i] - slow[i]

    act = None
    top_hist, bot_hist = [None] * n, [None] * n
    signals, macd_signals = [], []
    fallback_armed = False

    for t in range(n):
        if atr_vals[t] is None or t < 2 * P: continue
        piv_l = lo[t - P] == min(lo[t - 2 * P:t + 1])
        piv_h = h[t - P] == max(h[t - 2 * P:t + 1])
        wv = vols[t - P:t + 1]
        wtot = sum(wv)
        win_buy_pct = (sum(vols[j] for j in range(t - P, t + 1) if cl[j] >= o[j]) / wtot) if wtot > 0 else 0.5
        bull_ref, bear_ref, a = min(o[t - P], cl[t - P]), max(o[t - P], cl[t - P]), atr_vals[t]

        def free_of_overlap(new_bot, new_top):
            return act is None or (new_bot > act["top"] or new_top < act["bot"])

        if trend[t] == 1 and piv_l and free_of_overlap(bull_ref - a, bull_ref):
            top_, bot_ = bull_ref, bull_ref - a
            act = None if (dob and h[t] < bot_) else {"dir": 1, "top": top_, "bot": bot_, "created_i": t, "buy_pct": win_buy_pct}
            fallback_armed = False
        elif trend[t] == -1 and piv_h and free_of_overlap(bear_ref, bear_ref + a):
            top_, bot_ = bear_ref + a, bear_ref
            act = None if (dob and lo[t] > top_) else {"dir": -1, "top": top_, "bot": bot_, "created_i": t, "buy_pct": win_buy_pct}
            fallback_armed = False
        elif dob and act is not None:
            if (act["dir"] == 1 and h[t] < act["bot"]) or (act["dir"] == -1 and lo[t] > act["top"]):
                act = None
                fallback_armed = False

        top_hist[t] = act["top"] if act else None
        bot_hist[t] = act["bot"] if act else None
        if act is None or t <= act["created_i"]: continue
        if t > 0 and trend[t] != trend[t - 1]:
            fallback_armed = False
            continue

        buy_pct = act["buy_pct"]
        if act["dir"] == 1 and buy_pct >= mb and not piv_l and top_hist[t - 1] is not None and lo[t] > act["top"] and lo[t - 1] <= top_hist[t - 1]:
            signals.append((t, "long", buy_pct, act["bot"], act["top"], trend[t]))
            fallback_armed = False
            continue
        if act["dir"] == -1 and (1.0 - buy_pct) >= ms and not piv_h and bot_hist[t - 1] is not None and h[t] < act["bot"] and h[t - 1] >= bot_hist[t - 1]:
            signals.append((t, "short", buy_pct, act["bot"], act["top"], trend[t]))
            fallback_armed = False
            continue

        if hist[t] is None or hist[t - 1] is None: continue
        near_tol = min(OBE_MACD_NEAR_MAX, max(OBE_MACD_NEAR_MIN, max(act["top"] - act["bot"], 0.0) * OBE_MACD_NEAR_FRACTION))
        if act["dir"] == 1:
            if lo[t] > act["top"] and lo[t] <= act["top"] + near_tol and hist[t] < 0:
                fallback_armed = True
            if fallback_armed and hist[t - 1] <= 0 < hist[t]:
                if lo[t] > act["top"]:
                    macd_signals.append((t, "long", act["buy_pct"], act["bot"], act["top"], trend[t], near_tol))
                fallback_armed = False
        else:
            if h[t] < act["bot"] and h[t] >= act["bot"] - near_tol and hist[t] > 0:
                fallback_armed = True
            if fallback_armed and hist[t - 1] >= 0 > hist[t]:
                if h[t] < act["bot"]:
                    macd_signals.append((t, "short", act["buy_pct"], act["bot"], act["top"], trend[t], near_tol))
                fallback_armed = False

    return {"signals": signals, "macd_signals": macd_signals}


def check_ob_engine(candles, tf, state):
    name = "OB Engine " + tf
    need = OBE_ST_LENGTH + 2 * OBE_PIVOT + 10
    if not candles or len(candles) < need:
        return None, None

    seen = state.setdefault("obe_last_bar", {})
    last_seen = seen.get(tf)
    last_bar = candles[-1]["open_time"]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    feed_lag_min = (now_ms - last_bar) / 60000.0
    if feed_lag_min > OBE_MAX_FEED_LAG_MIN[tf]:
        return None, None

    res = obe_compute(candles)
    n = len(candles)
    max_age = OBE_MAX_AGE_BARS[tf]

    candidates = []
    for s in res["signals"]:
        candidates.append((s[0], s[1], s[2], s[3], s[4], "OB Retest", None))
    for s in res["macd_signals"]:
        candidates.append((s[0], s[1], s[2], s[3], s[4], "MACD Fallback", s[6]))

    fresh = [s for s in candidates if (n - 1 - s[0]) < max_age and (last_seen is None or candles[s[0]]["open_time"] > last_seen)]
    if not fresh:
        seen[tf] = last_bar
        return None, None

    (i, direction, bpct, ob_bot, ob_top, kind, near_tol) = fresh[-1]
    bar_ms = OBE_BAR_MS[tf]
    sig_age_min = (now_ms - (candles[i]["open_time"] + bar_ms)) / 60000.0
    if sig_age_min > OBE_MAX_SIGNAL_AGE_MIN[tf]:
        seen[tf] = last_bar
        return None, None

    entry = candles[i]["close"]
    sl_p = entry - OBE_SL_POINTS if direction == "long" else entry + OBE_SL_POINTS
    tp_p = entry + OBE_TP_POINTS if direction == "long" else entry - OBE_TP_POINTS

    for c in candles[i + 1:]:
        hit_sl = c["low"] <= sl_p if direction == "long" else c["high"] >= sl_p
        hit_tp = c["high"] >= tp_p if direction == "long" else c["low"] <= tp_p
        if hit_sl or hit_tp:
            seen[tf] = last_bar
            return None, None

    sig_open = candles[i]["open_time"]
    OBE_PLAN[name] = {"entry": entry, "sl_price": sl_p, "tp_price": tp_p, "scan_from_ms": sig_open + bar_ms, "timeframe": tf}
    seen[tf] = last_bar
    return direction, f"OB Engine {tf} {direction.upper()} @ {entry:.2f} ({kind})"


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=12,
        )
    except Exception as e:
        print(f"[TG ERROR] {e}")


def format_signal(direction, entry, strategy, info, sl, tp1, tp2, sl_pts, tp1_pts, tp2_pts, session, bias_1h, timeframe="5m"):
    arrow = "🟢 BUY" if direction == "long" else "🔴 SELL"
    return (
        f"<b>{arrow} XAUUSD ({timeframe})</b>\n"
        f"Strategy: <b>{strategy}</b>\n"
        f"Entry: <b>{entry:.2f}</b> | {info}\n"
        f"1H Trend: <b>{bias_1h.upper()}</b> | Session: {session}\n"
        f"─────────────────────\n"
        f"🛑 SL: <b>{sl:.2f}</b> (-{sl_pts} pts)\n"
        f"🎯 TP1: <b>{tp1:.2f}</b> (+{tp1_pts} pts)\n"
        f"🏁 TP2: <b>{tp2:.2f}</b> (+{tp2_pts} pts)\n"
        f"─────────────────────\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
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
    return any(t.get("strategy") == strategy and t.get("status") == "open" for t in trades)


def can_send(state, strategy, direction):
    if has_open_trade(strategy):
        return False
    now = time.time()
    last_t = state.get("last_signal_time", {}).get(strategy, 0)
    last_d = state.get("last_signal_direction", {}).get(strategy)
    return not (now - last_t < COOLDOWN_SECONDS and last_d == direction)


def record(state, strategy, direction):
    state.setdefault("last_signal_time", {})[strategy] = time.time()
    state.setdefault("last_signal_direction", {})[strategy] = direction


def global_signal_blocked(state, candle_open_time):
    now = time.time()
    last_time = state.get("global_last_signal_time", 0)
    last_candle = state.get("global_last_signal_candle")
    if last_candle == candle_open_time:
        return True
    return bool(last_time and now - last_time < GLOBAL_SIGNAL_COOLDOWN_SECONDS)


def mark_global_signal(state, candle_open_time):
    state["global_last_signal_time"] = time.time()
    state["global_last_signal_candle"] = candle_open_time
    save_state(state)


def run_strategy(strategy_name, func, *args):
    try:
        direction, info = func(*args)
    except Exception as exc:
        print(f"[STRATEGY ERROR] {strategy_name}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return None, None
    return direction, info


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Missing secrets", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    now_utc = datetime.now(timezone.utc)

    if not is_octa_xauusd_open(now_utc):
        hour_min = now_utc.hour * 60 + now_utc.minute
        today = now_utc.date().isoformat()
        if now_utc.weekday() == 5 and 150 <= hour_min < 160 and state.get("last_weekly_summary_date") != today:
            send_weekly_summary(now_utc)
            state["last_weekly_summary_date"] = today
        save_state(state)
        return

    candles, data_source = get_klines()
    if candles is None:
        save_state(state)
        return

    OBE_FEED_SOURCE["5m"] = data_source
    live_spot = fetch_spot()

    if live_spot is not None and FEED_ALIGN_TO_SPOT:
        candles, feed_basis = reconcile_feed_to_spot(candles, live_spot)
        if abs(feed_basis) >= FEED_BASIS_MAX_POINTS:
            save_state(state)
            return

    price = live_spot if live_spot is not None else candles[-1]["close"]
    session = get_session(now_utc.hour)
    bias_1h = get_1h_bias(candles, ema_period=20)

    resolve_obe_trades(candles, OBE_BAR_MS["5m"])
    resolve_closed_candle_trades(candles, OBE_BAR_MS["5m"])
    check_open_trades(price)

    today = now_utc.date().isoformat()
    if state.get("last_summary_date") is None:
        state["last_summary_date"] = today
    elif today != state["last_summary_date"]:
        y, m, d = map(int, state["last_summary_date"].split("-"))
        send_daily_summary(date(y, m, d))
        state["last_summary_date"] = today

    raw = []
    vwap = session_vwap(candles)
    profile = volume_profile(candles[-200:])

    d, info = run_strategy("VWAP+VP", check_vwap_vp, candles, vwap, profile)
    if d: raw.append(("VWAP+VP", d, info or ""))

    d, info = run_strategy("Liquidity Sweep", check_liquidity_sweep, candles)
    if d: raw.append(("Liquidity Sweep", d, info or ""))

    d, info = run_strategy("EMA+RSI", check_ema_rsi, candles)
    if d: raw.append(("EMA+RSI", d, info or ""))

    # ---- SMA6 & SMA8 Strategies (Offset: 1) ----
    d, info = run_strategy("SMA6", check_sma6, candles)
    if d: raw.append(("SMA6", d, info or ""))

    d, info = run_strategy("SMA8", check_sma8, candles)
    if d: raw.append(("SMA8", d, info or ""))

    d, info = run_strategy("MA4/45 Pullback", check_ma_pullback, candles)
    if d: raw.append(("MA4/45 Pullback", d, info or ""))

    d, info = run_strategy("MA3/25 Cross", check_ma2_148_cross, candles, price)
    if d: raw.append(("MA3/25 Cross", d, info or ""))

    d, info = run_strategy("SMA18 Touch", check_sma18_touch, candles, price)
    if d: raw.append(("SMA18 Touch", d, info or ""))

    vp_ctx = vp_context(candles)
    if vp_ctx:
        for vp_name, vp_func in (
            ("VP POC Bounce", check_vp_poc_bounce),
            ("VP Reversal", check_vp_reversal),
            ("VP Breakout", check_vp_breakout),
        ):
            d, info = run_strategy(vp_name, vp_func, candles, vp_ctx)
            if d and not vp_already_fired(state, vp_name, d):
                raw.append((vp_name, d, info or ""))

    d, info = run_strategy("SMA14/EMA28", check_sma14_ema28, candles)
    if d: raw.append(("SMA14/EMA28", d, info or ""))

    # MSB-OB: ZigZag 9 + Fib 0.5 pivot-based market structure break
    d, info = run_strategy("MSB-OB", check_msb_ob, candles, state)
    if d: raw.append(("MSB-OB", d, info or ""))

    d, info = run_strategy("HalfTrend", check_halftrend, candles, state)
    if d: raw.append(("HalfTrend", d, info or ""))

    d, info = run_strategy("Dynamic Grid", check_dynamic_grid, candles)
    if d: raw.append(("Dynamic Grid", d, info or ""))

    d, info = run_strategy("OB Engine 5m", check_ob_engine, candles, "5m", state)
    if d: raw.append(("OB Engine 5m", d, info or ""))

    final = []
    for strategy, direction, info in raw:
        skip_bias = strategy in NO_BIAS_STRATEGIES or (strategy in OBE_STRATEGIES and not OBE_USE_1H_BIAS)
        if not skip_bias and bias_1h == "bullish" and direction == "short":
            continue
        if not skip_bias and bias_1h == "bearish" and direction == "long":
            continue
        if not can_send(state, strategy, direction):
            continue
        final.append((strategy, direction, info))

    closed_candle_time = candles[-1].get("open_time")
    for strategy, direction, info in final:
        obe_free = strategy in OBE_STRATEGIES and not OBE_USE_GLOBAL_LIMIT
        if not obe_free and global_signal_blocked(state, closed_candle_time):
            continue

        if strategy in VP_STRATEGIES and vp_ctx:
            mark_vp_fired(state, strategy, direction)

        if obe_free:
            save_state(state)
        else:
            mark_global_signal(state, closed_candle_time)

        if strategy in OBE_STRATEGIES:
            sl_pts, tp1_pts, tp2_pts = OBE_SL_POINTS, OBE_TP_POINTS, OBE_TP_POINTS
        elif strategy in VP_STRATEGIES:
            sl_pts, tp1_pts, tp2_pts = VP_PLAN[strategy]
        elif strategy in STRATEGY_SL_TP_OVERRIDE:
            o_sl, o_tp = STRATEGY_SL_TP_OVERRIDE[strategy]
            sl_pts, tp1_pts, tp2_pts = o_sl, o_tp, o_tp
        else:
            sl_pts, tp1_pts, tp2_pts = FIXED_SL_POINTS, FIXED_TP_POINTS, FIXED_TP_POINTS

        signal_price = (
            candles[-1]["close"]
            if strategy in CLOSED_CANDLE_ENTRY_STRATEGIES
            else price
        )
        obe_plan = OBE_PLAN.get(strategy) if strategy in OBE_STRATEGIES else None
        if obe_plan:
            signal_price = obe_plan["entry"]
            lp = price
            dead = (lp <= obe_plan["sl_price"] or lp >= obe_plan["tp_price"]) if direction == "long" else (lp >= obe_plan["sl_price"] or lp <= obe_plan["tp_price"])
            if dead:
                continue

        if direction == "long":
            sl = round(signal_price - sl_pts, 2)
            tp1 = round(signal_price + tp1_pts, 2)
            tp2 = round(signal_price + tp2_pts, 2)
        else:
            sl = round(signal_price + sl_pts, 2)
            tp1 = round(signal_price - tp1_pts, 2)
            tp2 = round(signal_price - tp2_pts, 2)

        msg = format_signal(
            direction, signal_price, strategy, info, sl, tp1, tp2,
            sl_pts, tp1_pts, tp2_pts, session, bias_1h,
            timeframe=(obe_plan["timeframe"] if obe_plan else "5m")
        )
        send_telegram(msg)

        trade_extra = None
        if obe_plan:
            trade_extra = {"scan_from_ms": obe_plan["scan_from_ms"], "timeframe": obe_plan["timeframe"]}
        elif strategy in CLOSED_CANDLE_ENTRY_STRATEGIES:
            trade_extra = {"scan_from_ms": candles[-1]["open_time"] + OBE_BAR_MS["5m"], "timeframe": "5m", "signal_feed": data_source}

        open_trade(
            direction, signal_price, sl, tp1, tp2, strategy, info,
            breakeven_enabled=False,
            single_target=True,
            extra=trade_extra,
        )
        record(state, strategy, direction)

    now_ts = time.time()
    state["zone_last_fired"] = {k: v for k, v in state.get("zone_last_fired", {}).items() if now_ts - v < ZONE_DEDUP_SECONDS}
    save_state(state)


if __name__ == "__main__":
    main()
