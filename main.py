"""
XAUUSD Balanced Multi-Strategy Bot (Improved Quality Version)
-------------------------------------------------------------
Goal: Higher quality signals (target 60-65% win rate)

Features:
- 5 Strategies with ATR-based Dynamic SL & TP
- Stop Loss clamped strictly: 6.0 to 10.0 points
- Take Profit clamped strictly: 8.0 to 12.0 points
- 1-Hour trend bias filter (only trade in direction of 1H EMA)
- Scale-out at TP1 (50% closed) & Stop Loss moved to Breakeven
- Session awareness & Octa MT4 market-hour safety checks
- Exact entry price and diagnostic logging
"""

import json
import os
import statistics
import sys
import time
import requests
from datetime import datetime, timezone, date, timedelta

# ============== CONFIG ==============
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOL = "XAUUSD"
LOOKBACK_BARS = 500  # Increased to 500 bars (~41h) to calculate 1-Hour EMA
POINT_SIZE = 1.0     # 1 point = $1.00 of XAUUSD price movement

COOLDOWN_SECONDS = 240
ZONE_DEDUP_SECONDS = 3600  # don't re-alert the same OB/FVG zone within 1 hour

# ATR Configuration & Hard Bounds
ATR_PERIOD = 14
MIN_SL_POINTS = 6.0   # Strict SL Floor ($6.00)
MAX_SL_POINTS = 10.0  # Strict SL Cap ($10.00)
MIN_TP_POINTS = 8.0   # Strict TP Floor ($8.00)
MAX_TP_POINTS = 12.0  # Strict TP Cap ($12.00)
BE_BUFFER = 0.50      # Profit buffer above entry for Breakeven

# Multipliers for dynamic ATR sizing
STRATEGY_ATR_CONFIG = {
    "VWAP+VP":         {"sl_mult": 1.2, "tp1_ratio": 0.75, "tp2_ratio": 1.25},
    "EMA+RSI":         {"sl_mult": 1.2, "tp1_ratio": 0.75, "tp2_ratio": 1.25},
    "Liquidity Sweep": {"sl_mult": 1.4, "tp1_ratio": 0.80, "tp2_ratio": 1.35},
    "Order Block":     {"sl_mult": 1.4, "tp1_ratio": 0.80, "tp2_ratio": 1.35},
    "FVG":             {"sl_mult": 1.3, "tp1_ratio": 0.80, "tp2_ratio": 1.30},
}

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


# ============== STATE ==============
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
    return load_json(TRADE_LOG_FILE, [])

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


# ============== TRADE MGMT (SCALE-OUT & BREAKEVEN) ==============
def price_to_points(price_distance):
    """Convert XAUUSD price distance into this bot's gold points. 1 point = $1.00."""
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
        if t["status"] != "open":
            continue

        direction = t["direction"]
        entry = t["entry"]

        # ----------------- LONG TRADES -----------------
        if direction == "long":
            # 1. TP1 Trigger: Scale-out 50% and move Stop Loss to Breakeven
            if not t["tp1_hit"] and price >= t["tp1_price"]:
                t["tp1_hit"] = True
                t["sl_price"] = round(entry + BE_BUFFER, 2)
                partial_pnl = price_to_points((t["tp1_price"] - entry) * 0.5)
                t["pnl_points"] += partial_pnl
                changed = True
                save_trades(trades)
                send_telegram(
                    f"🎯 <b>TP1 HIT (+{round(t['tp1_price'] - entry, 2)} pts) — {t['strategy']}</b>\n"
                    f"50% closed. SL moved to Breakeven: <b>{t['sl_price']:.2f}</b>"
                )

            # 2. TP2 Trigger: Final Runner closed
            elif price >= t["tp2_price"]:
                runner_mult = 0.5 if t["tp1_hit"] else 1.0
                t["pnl_points"] += price_to_points((t["tp2_price"] - entry) * runner_mult)
                t.update(status="closed", result="win", closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                save_trades(trades)
                notify_result(t)

            # 3. Stop Loss Trigger
            elif price <= t["sl_price"]:
                if t["tp1_hit"]:
                    # Closed remaining position at Breakeven
                    t.update(status="closed", result="win", closed_at=datetime.now(timezone.utc).isoformat())
                    changed = True
                    save_trades(trades)
                    send_telegram(f"🛡️ <b>BREAKEVEN HIT — {t['strategy']}</b>\nRemaining 50% closed at {t['sl_price']:.2f}.")
                else:
                    # Full Stop Loss
                    t["pnl_points"] = -price_to_points(entry - t["sl_price"])
                    t.update(status="closed", result="loss", closed_at=datetime.now(timezone.utc).isoformat())
                    changed = True
                    save_trades(trades)
                    notify_result(t)

        # ----------------- SHORT TRADES -----------------
        else:
            # 1. TP1 Trigger: Scale-out 50% and move Stop Loss to Breakeven
            if not t["tp1_hit"] and price <= t["tp1_price"]:
                t["tp1_hit"] = True
                t["sl_price"] = round(entry - BE_BUFFER, 2)
                partial_pnl = price_to_points((entry - t["tp1_price"]) * 0.5)
                t["pnl_points"] += partial_pnl
                changed = True
                save_trades(trades)
                send_telegram(
                    f"🎯 <b>TP1 HIT (+{round(entry - t['tp1_price'], 2)} pts) — {t['strategy']}</b>\n"
                    f"50% closed. SL moved to Breakeven: <b>{t['sl_price']:.2f}</b>"
                )

            # 2. TP2 Trigger: Final Runner closed
            elif price <= t["tp2_price"]:
                runner_mult = 0.5 if t["tp1_hit"] else 1.0
                t["pnl_points"] += price_to_points((entry - t["tp2_price"]) * runner_mult)
                t.update(status="closed", result="win", closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                save_trades(trades)
                notify_result(t)

            # 3. Stop Loss Trigger
            elif price >= t["sl_price"]:
                if t["tp1_hit"]:
                    t.update(status="closed", result="win", closed_at=datetime.now(timezone.utc).isoformat())
                    changed = True
                    save_trades(trades)
                    send_telegram(f"🛡️ <b>BREAKEVEN HIT — {t['strategy']}</b>\nRemaining 50% closed at {t['sl_price']:.2f}.")
                else:
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

def notify_result(t):
    emoji = "✅ WIN" if t["result"] == "win" else "❌ LOSS"
    wins, losses, net_points, wr = trade_stats(load_trades())
    msg = (f"<b>{emoji}</b> — XAUUSD {t['direction'].upper()}\n"
           f"Strategy: <b>{t.get('strategy')}</b>\n"
           f"Entry: {t['entry']:.2f} | {t.get('level','-')}\n"
           f"Result: {t['result'].upper()} | {int(t['pnl_points']):+d} points\n"
           f"Overall: {wins}W / {losses}L | WR {wr:.1f}% | Net {net_points:+.0f} points")
    send_telegram(msg)

def send_daily_summary(for_date):
    trades = load_trades()
    day_str = for_date.isoformat()
    day_trades = [t for t in trades if t["status"] == "closed" and t.get("closed_at", "")[:10] == day_str]
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
        if t["result"] == "win": by[s]["w"] += 1
        else: by[s]["l"] += 1
        by[s]["pts"] += int(t.get("pnl_points") or 0)
    lines = "\n".join(f"• {s}: {v['w']}W/{v['l']}L | {v['pts']:+d} pts" for s, v in by.items())
    send_telegram(f"<b>📊 Daily Summary — {day_str}</b>\n"
                  f"Total: {len(day_trades)} | {len(wins)}W/{len(losses)}L | WR {wr:.1f}% | Net {int(total):+d} points\n\n{lines}")


# ============== DATA ==============
def fetch_xaus_json(path, params=None, timeout=15):
    """Fetch free XAU/USD data from XAUS (no API key)."""
    url = f"https://xaus.com{path}"
    headers = {"User-Agent": "XAUUSD-Multi-Strategy-Bot/1.0"}
    r = requests.get(url, params=params or {}, headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        state = data.get("data_state") or {}
        if state.get("status") == "unavailable":
            raise RuntimeError("XAUS data is unavailable")
    return data

def fetch_spot():
    try:
        data = fetch_xaus_json("/api/v1/spot", {"currency": "USD", "unit": "oz", "compact": "1"}, timeout=10)
        price = data.get("spot_usd_oz")
        if price is not None:
            return float(price)
    except Exception as e:
        print(f"[XAUS spot fail] {e}")

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
        raise RuntimeError(f"XAUS returned only {len(candles)} XAUUSD M5 candles")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    current_bucket_ms = (now_ms // (5 * 60 * 1000)) * (5 * 60 * 1000)
    candles = [c for c in candles if c["open_time"] < current_bucket_ms]
    if len(candles) < min(limit, 30):
        raise RuntimeError("Not enough completed XAUUSD M5 candles after removing the live candle")
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
    candles = [c for c in candles if c["open_time"] < current_bucket_ms]
    if len(candles) < min(limit, 30):
        raise RuntimeError(f"XAUS intraday fallback returned only {len(candles)} M5 candles")
    return candles[-limit:]

def get_klines(limit=LOOKBACK_BARS):
    try:
        candles = _get_xauusd_m5_chart(limit)
        print(f"[DATA] XAUUSD M5 via XAUS chart: {len(candles)} completed candles")
        return candles
    except Exception as e:
        print(f"[XAUS chart fail] {e}")
        candles = _get_xauusd_m5_intraday_fallback(limit)
        print(f"[DATA] XAUUSD M5 via XAUS 2m fallback: {len(candles)} completed candles")
        return candles


# ============== INDICATORS ==============
def ema(values, period):
    if len(values) < period: return [None] * len(values)
    out = [None] * (period - 1)
    s = sum(values[:period]) / period
    out.append(s)
    k = 2 / (period + 1)
    for i in range(period, len(values)):
        out.append((values[i] - out[-1]) * k + out[-1])
    return out

def rsi(closes, period=14):
    if len(closes) < period + 1: return [None] * len(closes)
    out = [None] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0)); losses.append(max(-ch, 0))
    ag, al = sum(gains) / period, sum(losses) / period
    out.append(100 if al == 0 else 100 - (100 / (1 + ag / al)))
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(ch, 0)) / period
        al = (al * (period - 1) + max(-ch, 0)) / period
        out.append(100 if al == 0 else 100 - (100 / (1 + ag / al)))
    return out

def atr(candles, period=14):
    """Calculate the Average True Range (ATR) over closed candles."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        true_range = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(true_range)
    if len(trs) < period:
        return None
    return statistics.mean(trs[-period:])

def session_vwap(candles):
    vals, cum_pv, cum_vol, day = [], 0.0, 0.0, None
    for c in candles:
        d = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc).date()
        if d != day:
            day = d; cum_pv = cum_vol = 0.0
        typ = (c["high"] + c["low"] + c["close"]) / 3
        vol = c["volume"] if c["volume"] > 0 else 1.0
        cum_pv += typ * vol
        cum_vol += vol
        vals.append(cum_pv / cum_vol if cum_vol else typ)
    return vals

def vwap_slope(vals, n=5):
    if len(vals) < n + 1: return "flat"
    d = vals[-1] - vals[-n]
    return "rising" if d > 0.05 else "falling" if d < -0.05 else "flat"

def volume_profile(candles, bins=10):
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    mx, mn = max(highs), min(lows)
    if mx == mn: return None
    size = (mx - mn) / bins
    vols = [0.0] * bins
    for c in candles:
        idx = max(0, min(bins - 1, int((c["close"] - mn) / size)))
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
            hi += 1; captured += vols[hi]
        else:
            lo -= 1; captured += vols[lo]
    return {"poc": round(poc, 2), "vah": round(mn + (hi + 1) * size, 2), "val": round(mn + lo * size, 2)}


# ============== SESSION & BIAS ==============
def get_session(utc_hour):
    if 0 <= utc_hour < 7: return "Asian"
    if 7 <= utc_hour < 12: return "London"
    if 12 <= utc_hour < 21: return "NewYork"
    return "Late"

def get_1h_bias(candles_5m, ema_period=20):
    """Aggregate 5-minute candles into 1-Hour closes to determine HTF trend."""
    if len(candles_5m) < (ema_period * 12):
        return "neutral"
    groups = {}
    for c in candles_5m:
        ts = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc)
        bucket = ts.replace(minute=0, second=0, microsecond=0)
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
    elif current_price < e_1h[-1]:
        return "bearish"