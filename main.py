"""
Multi-Strategy XAUUSD Signal Bot (5 Strategies) - GitHub Actions edition
------------------------------------------------------------------------
Strategies (all independent, each with own SL/TP):

1. VWAP + Volume Profile     → SL 6  / TP 8
2. Liquidity Sweep           → SL 8  / TP 10
3. EMA 9/21 + RSI            → SL 8  / TP 10
4. Order Block + Rejection   → SL 8  / TP 12
5. Fair Value Gap (FVG)      → SL 8  / TP 12

Timeframe: 5-minute
Signals 24×5 (no session filter)
Exact entry price shown
Each signal clearly labeled with strategy name
"""

import json
import os
import statistics
import sys
import time
import requests
from datetime import datetime, timezone, date

# ============== CONFIG ==============
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOL = "PAXGUSDT"
INTERVAL = "5m"
LOOKBACK_BARS = 250

# Volume Profile settings
NUM_BINS = 10
VALUE_AREA_PCT = 0.70
TOUCH_TOLERANCE_POINTS = 3.0
VOLUME_CONFIRM_MULT = 1.3

# ========== SL / TP per strategy (points) ==========
STRATEGY_RISK = {
    "VWAP+VP":           {"sl": 6.0,  "tp": 8.0},
    "Liquidity Sweep":   {"sl": 8.0,  "tp": 10.0},
    "EMA+RSI":           {"sl": 8.0,  "tp": 10.0},
    "Order Block":       {"sl": 8.0,  "tp": 12.0},
    "FVG":               {"sl": 8.0,  "tp": 12.0},
}

COOLDOWN_SECONDS = 300

# EMA + RSI
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14

# Liquidity Sweep
SWING_LOOKBACK = 12
SWEEP_TOLERANCE = 1.5

# Order Block
OB_LOOKBACK = 20          # bars to search for order block
OB_IMPULSE_MULT = 1.8     # impulse candle body must be this times average

# FVG
FVG_MIN_GAP = 1.5         # minimum gap size in points to count as valid FVG

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
    return load_json(STATE_FILE, {
        "last_signal_time": {},
        "last_signal_direction": {},
        "last_summary_date": None,
    })


def save_state(state):
    save_json(STATE_FILE, state)


# ============== TRADE MANAGEMENT ==============
def open_trade(direction, entry, sl_price, tp_price, strategy_name, level_name=""):
    trades = load_trades()
    trades.append({
        "direction": direction,
        "entry": entry,
        "sl_price": sl_price,
        "tp1_price": tp_price,
        "strategy": strategy_name,
        "level": level_name,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "closed_at": None,
        "result": None,
        "pnl_points": None,
    })
    save_trades(trades)


def check_open_trades(current_price):
    trades = load_trades()
    changed = False
    for t in trades:
        if t["status"] != "open":
            continue
        if t["direction"] == "long":
            if current_price >= t["tp1_price"]:
                t.update(status="closed", result="win",
                         pnl_points=round(t["tp1_price"] - t["entry"], 2),
                         closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                notify_trade_result(t)
            elif current_price <= t["sl_price"]:
                t.update(status="closed", result="loss",
                         pnl_points=round(t["sl_price"] - t["entry"], 2),
                         closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                notify_trade_result(t)
        else:
            if current_price <= t["tp1_price"]:
                t.update(status="closed", result="win",
                         pnl_points=round(t["entry"] - t["tp1_price"], 2),
                         closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                notify_trade_result(t)
            elif current_price >= t["sl_price"]:
                t.update(status="closed", result="loss",
                         pnl_points=round(t["entry"] - t["sl_price"], 2),
                         closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                notify_trade_result(t)
    if changed:
        save_trades(trades)


def notify_trade_result(trade):
    emoji = "✅ WIN" if trade["result"] == "win" else "❌ LOSS"
    msg = (
        f"<b>{emoji}</b> — XAUUSD {trade['direction'].upper()}\n"
        f"Strategy: {trade.get('strategy', '-')}\n"
        f"Entry: {trade['entry']:.2f} | {trade.get('level', '-')}\n"
        f"Result: {trade['pnl_points']:+.2f} pts"
    )
    send_telegram(msg)
    print(f"[CLOSED] {trade.get('strategy')} {trade['direction']} {trade['result']} {trade['pnl_points']:+.2f}")


def send_daily_summary(for_date):
    trades = load_trades()
    day_str = for_date.isoformat()
    day_trades = [t for t in trades if t["status"] == "closed" and t.get("closed_at", "")[:10] == day_str]

    if not day_trades:
        send_telegram(f"<b>📊 Daily Summary — {day_str}</b>\nNo closed trades today.")
        return

    wins = [t for t in day_trades if t["result"] == "win"]
    losses = [t for t in day_trades if t["result"] == "loss"]
    total = sum(t["pnl_points"] for t in day_trades)
    wr = (len(wins) / len(day_trades)) * 100

    by_strat = {}
    for t in day_trades:
        s = t.get("strategy", "Unknown")
        by_strat.setdefault(s, {"w": 0, "l": 0, "pts": 0.0})
        if t["result"] == "win":
            by_strat[s]["w"] += 1
        else:
            by_strat[s]["l"] += 1
        by_strat[s]["pts"] += t["pnl_points"]

    lines = "\n".join(f"• {s}: {v['w']}W/{v['l']}L ({v['pts']:+.1f})" for s, v in by_strat.items())
    msg = (
        f"<b>📊 Daily Summary — {day_str}</b>\n"
        f"Total: {len(day_trades)} | {len(wins)}W/{len(losses)}L | WR {wr:.1f}% | Net {total:+.2f}\n\n"
        f"{lines}"
    )
    send_telegram(msg)


# ============== DATA ==============
def fetch_spot_gold_price():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        r = requests.get("https://data-asg.goldprice.org/dbXRates/USD", headers=headers, timeout=10)
        r.raise_for_status()
        return float(r.json()["items"][0]["xauPrice"])
    except Exception as e:
        print(f"[goldprice.org] {e}")
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", headers=headers, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        print(f"[gold-api.com] {e}")
    return None


def calibrate_candles_to_spot(candles):
    spot = fetch_spot_gold_price()
    if spot is None:
        print("[WARNING] No spot price — using raw PAXG")
        return candles
    offset = spot - candles[-1]["close"]
    print(f"[Calibration] PAXG={candles[-1]['close']:.2f} Spot={spot:.2f} Offset={offset:+.2f}")
    return [{
        "open_time": c["open_time"],
        "open": c["open"] + offset,
        "high": c["high"] + offset,
        "low": c["low"] + offset,
        "close": c["close"] + offset,
        "volume": c["volume"],
    } for c in candles]


def get_klines_kraken(limit):
    resp = requests.get("https://api.kraken.com/0/public/OHLC",
                        params={"pair": "PAXGUSD", "interval": 5}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    pair_key = next(k for k in data["result"] if k != "last")
    raw = data["result"][pair_key][-limit:]
    return [{
        "open_time": int(row[0]) * 1000,
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[6]),
    } for row in raw]


def get_klines_okx(limit):
    resp = requests.get("https://www.okx.com/api/v5/market/candles",
                        params={"instId": "PAXG-USDT", "bar": "5m", "limit": str(limit)}, timeout=15)
    resp.raise_for_status()
    raw = list(reversed(resp.json().get("data", [])))
    if not raw:
        raise RuntimeError("OKX empty")
    return [{
        "open_time": int(row[0]),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
    } for row in raw]


def get_klines(limit):
    try:
        return get_klines_kraken(limit)
    except Exception as e:
        print(f"[Kraken → OKX] {e}")
        return get_klines_okx(limit)


# ============== INDICATORS ==============
def compute_ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    ema = [None] * (period - 1)
    sma = sum(values[:period]) / period
    ema.append(sma)
    k = 2 / (period + 1)
    for i in range(period, len(values)):
        ema.append((values[i] - ema[-1]) * k + ema[-1])
    return ema


def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)
    rsi = [None] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    rsi.append(100.0 if avg_l == 0 else 100 - (100 / (1 + avg_g / avg_l)))
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(ch, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-ch, 0)) / period
        rsi.append(100.0 if avg_l == 0 else 100 - (100 / (1 + avg_g / avg_l)))
    return rsi


def compute_session_vwap(candles):
    vwap_values = []
    cum_pv = cum_vol = 0.0
    current_day = None
    for c in candles:
        day = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc).date()
        if day != current_day:
            current_day = day
            cum_pv = cum_vol = 0.0
        typical = (c["high"] + c["low"] + c["close"]) / 3.0
        cum_pv += typical * c["volume"]
        cum_vol += c["volume"]
        vwap_values.append(cum_pv / cum_vol if cum_vol else typical)
    return vwap_values


def vwap_slope(vwap_values, lookback=5):
    if len(vwap_values) < lookback + 1:
        return "flat"
    diff = vwap_values[-1] - vwap_values[-lookback]
    if diff > 0.05:
        return "rising"
    if diff < -0.05:
        return "falling"
    return "flat"


def compute_volume_profile(candles, num_bins=NUM_BINS):
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    pmax, pmin = max(highs), min(lows)
    if pmax == pmin:
        return None
    bin_size = (pmax - pmin) / num_bins
    vols = [0.0] * num_bins
    for c in candles:
        idx = max(0, min(num_bins - 1, int((c["close"] - pmin) / bin_size)))
        vols[idx] += c["volume"]
    poc_idx = vols.index(max(vols))
    poc = pmin + (poc_idx + 0.5) * bin_size
    total = sum(vols)
    target = total * VALUE_AREA_PCT
    captured = vols[poc_idx]
    lo = hi = poc_idx
    while captured < target and (lo > 0 or hi < num_bins - 1):
        below = vols[lo - 1] if lo > 0 else -1
        above = vols[hi + 1] if hi < num_bins - 1 else -1
        if above >= below:
            hi += 1
            captured += vols[hi]
        else:
            lo -= 1
            captured += vols[lo]
    return {"poc": round(poc, 2), "vah": round(pmin + (hi + 1) * bin_size, 2), "val": round(pmin + lo * bin_size, 2)}


# ============== STRATEGY 1: VWAP + VP ==============
def near_level(price, level, tol=TOUCH_TOLERANCE_POINTS):
    return abs(price - level) <= tol


def is_rejection(candle, level, direction):
    body_h = max(candle["open"], candle["close"])
    body_l = min(candle["open"], candle["close"])
    if direction == "long":
        return candle["low"] <= level + TOUCH_TOLERANCE_POINTS and (body_l - candle["low"]) > 0 and candle["close"] > body_l
    return candle["high"] >= level - TOUCH_TOLERANCE_POINTS and (candle["high"] - body_h) > 0 and candle["close"] < body_h


def volume_ok(candles, idx, mult=VOLUME_CONFIRM_MULT):
    if idx < 10:
        return False
    avg = statistics.mean(c["volume"] for c in candles[idx-10:idx])
    return candles[idx]["volume"] >= avg * mult


def check_vwap_vp(candles, vwap_vals, profile):
    if not profile:
        return None, None
    last = candles[-1]
    slope = vwap_slope(vwap_vals)
    for name, price in {"POC": profile["poc"], "VAH": profile["vah"], "VAL": profile["val"]}.items():
        if not near_level(last["close"], price):
            continue
        if slope == "rising" and is_rejection(last, price, "long") and volume_ok(candles, len(candles)-1):
            return "long", name
        if slope == "falling" and is_rejection(last, price, "short") and volume_ok(candles, len(candles)-1):
            return "short", name
    return None, None


# ============== STRATEGY 2: Liquidity Sweep ==============
def find_swing(candles, lookback=SWING_LOOKBACK):
    if len(candles) < lookback + 2:
        return None, None
    window = candles[-(lookback+1):-1]
    return max(c["high"] for c in window), min(c["low"] for c in window)


def check_liquidity_sweep(candles):
    last = candles[-1]
    sh, sl = find_swing(candles)
    if sh is None:
        return None, None
    if last["low"] < sl - SWEEP_TOLERANCE and last["close"] > sl:
        if (min(last["open"], last["close"]) - last["low"]) > 0 and last["close"] > last["open"]:
            return "long", f"Sweep Low {sl:.2f}"
    if last["high"] > sh + SWEEP_TOLERANCE and last["close"] < sh:
        if (last["high"] - max(last["open"], last["close"])) > 0 and last["close"] < last["open"]:
            return "short", f"Sweep High {sh:.2f}"
    return None, None


# ============== STRATEGY 3: EMA + RSI ==============
def check_ema_rsi(candles):
    closes = [c["close"] for c in candles]
    if len(closes) < max(EMA_SLOW, RSI_PERIOD) + 5:
        return None, None
    ef = compute_ema(closes, EMA_FAST)
    es = compute_ema(closes, EMA_SLOW)
    rsi = compute_rsi(closes, RSI_PERIOD)
    if None in (ef[-1], es[-1], rsi[-1], ef[-2], es[-2]):
        return None, None
    if ef[-2] <= es[-2] and ef[-1] > es[-1] and rsi[-1] > 50:
        return "long", f"EMA Cross RSI {rsi[-1]:.1f}"
    if ef[-2] >= es[-2] and ef[-1] < es[-1] and rsi[-1] < 50:
        return "short", f"EMA Cross RSI {rsi[-1]:.1f}"
    return None, None


# ============== STRATEGY 4: Order Block + Rejection ==============
def find_order_block(candles, lookback=OB_LOOKBACK):
    """Find most recent bullish/bearish order block."""
    if len(candles) < lookback + 3:
        return None, None

    # Average body size for impulse detection
    bodies = [abs(c["close"] - c["open"]) for c in candles[-lookback-5:-1]]
    avg_body = statistics.mean(bodies) if bodies else 1.0

    # Look for bullish OB: last down candle before strong up move
    for i in range(len(candles) - 3, len(candles) - lookback - 1, -1):
        c = candles[i]
        # Bearish candle (potential bullish OB)
        if c["close"] < c["open"]:
            # Check if next 1-2 candles are strong bullish impulse
            impulse = False
            for j in range(i + 1, min(i + 3, len(candles) - 1)):
                if candles[j]["close"] > candles[j]["open"] and abs(candles[j]["close"] - candles[j]["open"]) > avg_body * OB_IMPULSE_MULT:
                    impulse = True
                    break
            if impulse:
                return "bullish", {"high": c["high"], "low": c["low"], "idx": i}

    # Look for bearish OB: last up candle before strong down move
    for i in range(len(candles) - 3, len(candles) - lookback - 1, -1):
        c = candles[i]
        if c["close"] > c["open"]:
            impulse = False
            for j in range(i + 1, min(i + 3, len(candles) - 1)):
                if candles[j]["close"] < candles[j]["open"] and abs(candles[j]["close"] - candles[j]["open"]) > avg_body * OB_IMPULSE_MULT:
                    impulse = True
                    break
            if impulse:
                return "bearish", {"high": c["high"], "low": c["low"], "idx": i}

    return None, None


def check_order_block(candles):
    ob_type, ob = find_order_block(candles)
    if ob is None:
        return None, None

    last = candles[-1]
    # Price is touching the order block zone
    if last["low"] <= ob["high"] and last["high"] >= ob["low"]:
        if ob_type == "bullish" and is_rejection(last, ob["low"], "long"):
            return "long", f"Bullish OB {ob['low']:.2f}-{ob['high']:.2f}"
        if ob_type == "bearish" and is_rejection(last, ob["high"], "short"):
            return "short", f"Bearish OB {ob['low']:.2f}-{ob['high']:.2f}"
    return None, None


# ============== STRATEGY 5: Fair Value Gap (FVG) ==============
def find_fvg(candles):
    """Find most recent unfilled Fair Value Gap (3-candle pattern)."""
    if len(candles) < 5:
        return None, None

    # Check last few bars for FVG
    for i in range(len(candles) - 2, max(len(candles) - 15, 2), -1):
        c1 = candles[i - 2]  # first candle
        c2 = candles[i - 1]  # middle (impulse)
        c3 = candles[i]      # third candle

        # Bullish FVG: gap between c1 high and c3 low
        if c3["low"] > c1["high"] + FVG_MIN_GAP:
            gap_low = c1["high"]
            gap_high = c3["low"]
            # Check if still unfilled (current price not fully through)
            if candles[-1]["low"] > gap_low:
                return "bullish", {"low": gap_low, "high": gap_high}

        # Bearish FVG: gap between c1 low and c3 high
        if c3["high"] < c1["low"] - FVG_MIN_GAP:
            gap_high = c1["low"]
            gap_low = c3["high"]
            if candles[-1]["high"] < gap_high:
                return "bearish", {"low": gap_low, "high": gap_high}

    return None, None


def check_fvg(candles):
    fvg_type, fvg = find_fvg(candles)
    if fvg is None:
        return None, None

    last = candles[-1]
    # Price is entering the FVG zone
    if last["low"] <= fvg["high"] and last["high"] >= fvg["low"]:
        if fvg_type == "bullish" and last["close"] > last["open"]:
            return "long", f"Bullish FVG {fvg['low']:.2f}-{fvg['high']:.2f}"
        if fvg_type == "bearish" and last["close"] < last["open"]:
            return "short", f"Bearish FVG {fvg['low']:.2f}-{fvg['high']:.2f}"
    return None, None


# ============== TELEGRAM ==============
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Missing credentials")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=15
        ).raise_for_status()
    except Exception as e:
        print(f"[Telegram] {e}")


def format_signal(direction, entry, strategy, info, sl, tp, sl_pts, tp_pts):
    arrow = "🟢 BUY" if direction == "long" else "🔴 SELL"
    return (
        f"<b>{arrow} XAUUSD (5m)</b>\n"
        f"Strategy: <b>{strategy}</b>\n"
        f"Entry: {entry:.2f}\n"
        f"Info: {info}\n"
        f"SL: {sl:.2f} ({sl_pts} pts) | TP: {tp:.2f} ({tp_pts} pts)\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )


def can_send(state, strategy, direction):
    now = time.time()
    last_t = state.get("last_signal_time", {}).get(strategy, 0)
    last_d = state.get("last_signal_direction", {}).get(strategy)
    if now - last_t < COOLDOWN_SECONDS and last_d == direction:
        return False
    return True


def record_signal(state, strategy, direction):
    state.setdefault("last_signal_time", {})[strategy] = time.time()
    state.setdefault("last_signal_direction", {})[strategy] = direction


# ============== MAIN ==============
def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    candles = calibrate_candles_to_spot(get_klines(LOOKBACK_BARS))
    price = candles[-1]["close"]

    check_open_trades(price)

    # Daily summary
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("last_summary_date") is None:
        state["last_summary_date"] = today
    elif today != state["last_summary_date"]:
        y, m, d = map(int, state["last_summary_date"].split("-"))
        send_daily_summary(date(y, m, d))
        state["last_summary_date"] = today

    # Collect signals from all 5 strategies
    signals = []

    # 1. VWAP + VP
    vwap_vals = compute_session_vwap(candles)
    profile = compute_volume_profile(candles[-LOOKBACK_BARS:])
    d, info = check_vwap_vp(candles, vwap_vals, profile)
    if d and can_send(state, "VWAP+VP", d):
        signals.append(("VWAP+VP", d, info or ""))

    # 2. Liquidity Sweep
    d, info = check_liquidity_sweep(candles)
    if d and can_send(state, "Liquidity Sweep", d):
        signals.append(("Liquidity Sweep", d, info or ""))

    # 3. EMA + RSI
    d, info = check_ema_rsi(candles)
    if d and can_send(state, "EMA+RSI", d):
        signals.append(("EMA+RSI", d, info or ""))

    # 4. Order Block
    d, info = check_order_block(candles)
    if d and can_send(state, "Order Block", d):
        signals.append(("Order Block", d, info or ""))

    # 5. Fair Value Gap
    d, info = check_fvg(candles)
    if d and can_send(state, "FVG", d):
        signals.append(("FVG", d, info or ""))

    # Send signals
    for strategy, direction, info in signals:
        risk = STRATEGY_RISK[strategy]
        sl_pts = risk["sl"]
        tp_pts = risk["tp"]

        if direction == "long":
            sl = round(price - sl_pts, 2)
            tp = round(price + tp_pts, 2)
        else:
            sl = round(price + sl_pts, 2)
            tp = round(price - tp_pts, 2)

        msg = format_signal(direction, price, strategy, info, sl, tp, sl_pts, tp_pts)
        send_telegram(msg)
        open_trade(direction, price, sl, tp, strategy, info)
        record_signal(state, strategy, direction)
        print(f"[SIGNAL] {strategy} → {direction.upper()} @ {price:.2f} | {info}")

    if not signals:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] No signals this run.")

    save_state(state)


if __name__ == "__main__":
    main()
