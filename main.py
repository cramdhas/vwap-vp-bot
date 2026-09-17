"""
Multi-Strategy XAUUSD Signal Bot (GitHub Actions edition)
---------------------------------------------------------
Strategies included (all independent):
  1. VWAP + Volume Profile (POC/VAH/VAL) + Rejection
  2. Liquidity Sweep + Rejection
  3. 9 EMA / 21 EMA + RSI Filter

Timeframe: 5-minute only.
Each strategy can fire its own signal with clear labeling.
State (open trades, cooldowns, daily summary) persists via JSON files.

Credentials from environment variables (GitHub Secrets).
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

# Risk settings per strategy (points)
# VWAP+VP keeps original values
VWAP_SL_POINTS = 6.0
VWAP_TP_POINTS = 8.0

# New strategies (Liquidity Sweep + EMA+RSI)
NEW_SL_POINTS = 8.0
NEW_TP_POINTS = 10.0

# Cooldown per strategy (seconds)
COOLDOWN_SECONDS = 300

# EMA + RSI settings
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_LONG_LEVEL = 50
RSI_SHORT_LEVEL = 50

# Liquidity Sweep settings
SWING_LOOKBACK = 12          # bars to find recent swing high/low
SWEEP_TOLERANCE = 1.5        # how far beyond swing counts as sweep

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_FILE = os.path.join(SCRIPT_DIR, "trades.json")
STATE_FILE = os.path.join(SCRIPT_DIR, "state.json")


# ============== STATE PERSISTENCE ==============
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
def open_trade(direction, entry, sl_price, tp1_price, strategy_name, level_name=""):
    trades = load_trades()
    trades.append({
        "direction": direction,
        "entry": entry,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
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
        else:  # short
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
    strategy = trade.get("strategy", "Unknown")
    msg = (
        f"<b>{emoji}</b> — XAUUSD {trade['direction'].upper()}\n"
        f"Strategy: {strategy}\n"
        f"Entry: {trade['entry']:.2f} | Level: {trade.get('level', '-')}\n"
        f"Closed: {trade['pnl_points']:+.2f} pts"
    )
    send_telegram(msg)
    print(f"[TRADE CLOSED] {strategy} {trade['direction']} {trade['result']} {trade['pnl_points']:+.2f} pts")


def send_daily_summary(for_date):
    trades = load_trades()
    day_str = for_date.isoformat()
    day_trades = [
        t for t in trades
        if t["status"] == "closed" and t["closed_at"] and t["closed_at"][:10] == day_str
    ]

    if not day_trades:
        msg = f"<b>📊 Daily Summary — {day_str}</b>\nNo closed trades today."
        send_telegram(msg)
        return

    wins = [t for t in day_trades if t["result"] == "win"]
    losses = [t for t in day_trades if t["result"] == "loss"]
    total_points = sum(t["pnl_points"] for t in day_trades)
    win_rate = (len(wins) / len(day_trades)) * 100

    by_strategy = {}
    for t in day_trades:
        s = t.get("strategy", "Unknown")
        by_strategy.setdefault(s, {"w": 0, "l": 0, "pts": 0.0})
        if t["result"] == "win":
            by_strategy[s]["w"] += 1
        else:
            by_strategy[s]["l"] += 1
        by_strategy[s]["pts"] += t["pnl_points"]

    strategy_lines = "\n".join(
        f"• {s}: {v['w']}W/{v['l']}L ({v['pts']:+.1f} pts)"
        for s, v in by_strategy.items()
    )

    msg = (
        f"<b>📊 Daily Summary — {day_str}</b>\n"
        f"Total: {len(day_trades)} | Wins: {len(wins)} | Losses: {len(losses)}\n"
        f"Win rate: {win_rate:.1f}% | Net: {total_points:+.2f} pts\n\n"
        f"{strategy_lines}"
    )
    send_telegram(msg)
    print(f"[DAILY SUMMARY] {day_str}: {len(wins)}W/{len(losses)}L, {total_points:+.2f} pts")


# ============== DATA FETCH ==============
def fetch_spot_gold_price():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    }
    try:
        r = requests.get("https://data-asg.goldprice.org/dbXRates/USD",
                          headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data["items"][0]["xauPrice"])
    except Exception as e:
        print(f"[goldprice.org failed] {e}")

    try:
        r = requests.get("https://api.gold-api.com/price/XAU",
                          headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data["price"])
    except Exception as e:
        print(f"[gold-api.com failed] {e}")

    return None


def calibrate_candles_to_spot(candles):
    spot = fetch_spot_gold_price()
    if spot is None:
        print("[WARNING] Could not fetch real spot gold — using uncalibrated PAXG")
        return candles

    offset = spot - candles[-1]["close"]
    print(f"[Calibration] PAXG={candles[-1]['close']:.2f}, Spot={spot:.2f}, Offset={offset:+.2f}")

    calibrated = []
    for c in candles:
        calibrated.append({
            "open_time": c["open_time"],
            "open": c["open"] + offset,
            "high": c["high"] + offset,
            "low": c["low"] + offset,
            "close": c["close"] + offset,
            "volume": c["volume"],
        })
    return calibrated


def get_klines_kraken(limit):
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": "PAXGUSD", "interval": 5}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")

    result = data["result"]
    pair_key = next(k for k in result.keys() if k != "last")
    raw = result[pair_key][-limit:]

    candles = []
    for row in raw:
        candles.append({
            "open_time": int(row[0]) * 1000,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6]),
        })
    return candles


def get_klines_okx(limit):
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": "PAXG-USDT", "bar": "5m", "limit": str(limit)}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("data", [])
    if not raw:
        raise RuntimeError(f"OKX no data: {data}")

    raw = list(reversed(raw))
    candles = []
    for row in raw:
        candles.append({
            "open_time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        })
    return candles


def get_klines(limit):
    try:
        return get_klines_kraken(limit)
    except Exception as e:
        print(f"[Kraken failed → OKX] {e}")
        return get_klines_okx(limit)


# ============== INDICATORS ==============
def compute_ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    ema = [None] * (period - 1)
    sma = sum(values[:period]) / period
    ema.append(sma)
    multiplier = 2 / (period + 1)
    for i in range(period, len(values)):
        val = (values[i] - ema[-1]) * multiplier + ema[-1]
        ema.append(val)
    return ema


def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)

    rsi = [None] * period
    gains = []
    losses = []

    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        rsi.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi.append(100 - (100 / (1 + rs)))

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - (100 / (1 + rs)))

    return rsi


def compute_session_vwap(candles):
    vwap_values = []
    cum_pv = 0.0
    cum_vol = 0.0
    current_day = None

    for c in candles:
        dt = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc)
        day = dt.date()
        if day != current_day:
            current_day = day
            cum_pv = 0.0
            cum_vol = 0.0

        typical = (c["high"] + c["low"] + c["close"]) / 3.0
        cum_pv += typical * c["volume"]
        cum_vol += c["volume"]
        vwap = cum_pv / cum_vol if cum_vol > 0 else typical
        vwap_values.append(vwap)

    return vwap_values


def vwap_slope_direction(vwap_values, lookback=5):
    if len(vwap_values) < lookback + 1:
        return "flat"
    recent = vwap_values[-lookback:]
    diff = recent[-1] - recent[0]
    if diff > 0.05:
        return "rising"
    elif diff < -0.05:
        return "falling"
    return "flat"


def compute_volume_profile(candles, num_bins=NUM_BINS):
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    price_max = max(highs)
    price_min = min(lows)

    if price_max == price_min:
        return None

    bin_size = (price_max - price_min) / num_bins
    bin_volumes = [0.0] * num_bins

    for c in candles:
        idx = int((c["close"] - price_min) / bin_size)
        idx = max(0, min(num_bins - 1, idx))
        bin_volumes[idx] += c["volume"]

    poc_idx = bin_volumes.index(max(bin_volumes))
    poc_price = price_min + (poc_idx + 0.5) * bin_size

    total_volume = sum(bin_volumes)
    target_volume = total_volume * VALUE_AREA_PCT
    captured = bin_volumes[poc_idx]

    lo_idx, hi_idx = poc_idx, poc_idx
    while captured < target_volume and (lo_idx > 0 or hi_idx < num_bins - 1):
        vol_below = bin_volumes[lo_idx - 1] if lo_idx > 0 else -1
        vol_above = bin_volumes[hi_idx + 1] if hi_idx < num_bins - 1 else -1

        if vol_above >= vol_below:
            hi_idx += 1
            captured += bin_volumes[hi_idx]
        else:
            lo_idx -= 1
            captured += bin_volumes[lo_idx]

    val_price = price_min + lo_idx * bin_size
    vah_price = price_min + (hi_idx + 1) * bin_size

    return {
        "poc": round(poc_price, 2),
        "vah": round(vah_price, 2),
        "val": round(val_price, 2),
    }


# ============== STRATEGY 1: VWAP + Volume Profile ==============
def near_level(price, level, tolerance=TOUCH_TOLERANCE_POINTS):
    return abs(price - level) <= tolerance


def is_rejection_candle(candle, level, direction):
    body_high = max(candle["open"], candle["close"])
    body_low = min(candle["open"], candle["close"])

    if direction == "long":
        lower_wick = body_low - candle["low"]
        return candle["low"] <= level + TOUCH_TOLERANCE_POINTS and lower_wick > 0 and candle["close"] > body_low
    else:
        upper_wick = candle["high"] - body_high
        return candle["high"] >= level - TOUCH_TOLERANCE_POINTS and upper_wick > 0 and candle["close"] < body_high


def volume_confirmed(candles, idx, mult=VOLUME_CONFIRM_MULT):
    if idx < 10:
        return False
    recent_avg = statistics.mean(c["volume"] for c in candles[idx - 10:idx])
    return candles[idx]["volume"] >= recent_avg * mult


def check_vwap_vp_signal(candles, vwap_values, profile):
    if not profile:
        return None, None

    last_idx = len(candles) - 1
    last_candle = candles[last_idx]
    close_price = last_candle["close"]
    slope = vwap_slope_direction(vwap_values)

    levels = {"POC": profile["poc"], "VAH": profile["vah"], "VAL": profile["val"]}

    for level_name, level_price in levels.items():
        if not near_level(close_price, level_price):
            continue

        if slope == "rising" and is_rejection_candle(last_candle, level_price, "long"):
            if volume_confirmed(candles, last_idx):
                return "long", level_name

        if slope == "falling" and is_rejection_candle(last_candle, level_price, "short"):
            if volume_confirmed(candles, last_idx):
                return "short", level_name

    return None, None


# ============== STRATEGY 2: Liquidity Sweep + Rejection ==============
def find_recent_swing(candles, lookback=SWING_LOOKBACK):
    """Find most recent swing high and swing low in the lookback window (excluding current bar)."""
    if len(candles) < lookback + 2:
        return None, None

    window = candles[-(lookback + 1):-1]  # exclude current forming candle
    swing_high = max(c["high"] for c in window)
    swing_low = min(c["low"] for c in window)
    return swing_high, swing_low


def check_liquidity_sweep_signal(candles):
    last = candles[-1]
    prev = candles[-2] if len(candles) > 1 else last

    swing_high, swing_low = find_recent_swing(candles)
    if swing_high is None:
        return None, None

    # Bullish sweep: price took the lows then closed back above with rejection
    if last["low"] < swing_low - SWEEP_TOLERANCE and last["close"] > swing_low:
        lower_wick = min(last["open"], last["close"]) - last["low"]
        if lower_wick > 0 and last["close"] > last["open"]:
            return "long", f"Sweep Low {swing_low:.2f}"

    # Bearish sweep: price took the highs then closed back below with rejection
    if last["high"] > swing_high + SWEEP_TOLERANCE and last["close"] < swing_high:
        upper_wick = last["high"] - max(last["open"], last["close"])
        if upper_wick > 0 and last["close"] < last["open"]:
            return "short", f"Sweep High {swing_high:.2f}"

    return None, None


# ============== STRATEGY 3: 9/21 EMA + RSI ==============
def check_ema_rsi_signal(candles):
    closes = [c["close"] for c in candles]
    if len(closes) < max(EMA_SLOW, RSI_PERIOD) + 5:
        return None, None

    ema_fast = compute_ema(closes, EMA_FAST)
    ema_slow = compute_ema(closes, EMA_SLOW)
    rsi = compute_rsi(closes, RSI_PERIOD)

    # Need valid values
    if ema_fast[-1] is None or ema_slow[-1] is None or rsi[-1] is None:
        return None, None
    if ema_fast[-2] is None or ema_slow[-2] is None:
        return None, None

    # Bullish cross: fast crosses above slow + RSI above mid
    if ema_fast[-2] <= ema_slow[-2] and ema_fast[-1] > ema_slow[-1] and rsi[-1] > RSI_LONG_LEVEL:
        return "long", f"EMA Cross + RSI {rsi[-1]:.1f}"

    # Bearish cross: fast crosses below slow + RSI below mid
    if ema_fast[-2] >= ema_slow[-2] and ema_fast[-1] < ema_slow[-1] and rsi[-1] < RSI_SHORT_LEVEL:
        return "short", f"EMA Cross + RSI {rsi[-1]:.1f}"

    return None, None


# ============== TELEGRAM ==============
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[Telegram error] {e}")


def format_signal(direction, entry, strategy_name, level_info, sl_price, tp1_price, sl_pts, tp_pts):
    arrow = "🟢 BUY" if direction == "long" else "🔴 SELL"
    return (
        f"<b>{arrow} XAUUSD (5m)</b>\n"
        f"Strategy: <b>{strategy_name}</b>\n"
        f"Entry: {entry:.2f}\n"
        f"Info: {level_info}\n"
        f"SL: {sl_price:.2f} ({sl_pts} pts) | TP: {tp1_price:.2f} ({tp_pts} pts)\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )


def can_send_signal(state, strategy_name, direction):
    """Per-strategy cooldown check."""
    now = time.time()
    last_times = state.get("last_signal_time", {})
    last_dirs = state.get("last_signal_direction", {})

    last_t = last_times.get(strategy_name, 0)
    last_d = last_dirs.get(strategy_name)

    if now - last_t < COOLDOWN_SECONDS and last_d == direction:
        return False
    return True


def record_signal(state, strategy_name, direction):
    if "last_signal_time" not in state:
        state["last_signal_time"] = {}
    if "last_signal_direction" not in state:
        state["last_signal_direction"] = {}
    state["last_signal_time"][strategy_name] = time.time()
    state["last_signal_direction"][strategy_name] = direction


# ============== MAIN ==============
def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.", file=sys.stderr)
        sys.exit(1)

    state = load_state()

    candles = get_klines(LOOKBACK_BARS)
    candles = calibrate_candles_to_spot(candles)
    current_price = candles[-1]["close"]

    # 1. Check open virtual trades
    check_open_trades(current_price)

    # 2. Daily summary
    today = datetime.now(timezone.utc).date().isoformat()
    last_summary_date = state.get("last_summary_date")
    if last_summary_date is None:
        state["last_summary_date"] = today
    elif today != last_summary_date:
        y, m, d = map(int, last_summary_date.split("-"))
        send_daily_summary(date(y, m, d))
        state["last_summary_date"] = today

    # 3. Run all three strategies
    signals_to_send = []

    # Strategy 1: VWAP + Volume Profile
    vwap_values = compute_session_vwap(candles)
    profile = compute_volume_profile(candles[-LOOKBACK_BARS:])
    direction, level = check_vwap_vp_signal(candles, vwap_values, profile)
    if direction and can_send_signal(state, "VWAP+VP", direction):
        signals_to_send.append(("VWAP+VP", direction, level or ""))

    # Strategy 2: Liquidity Sweep
    direction, level = check_liquidity_sweep_signal(candles)
    if direction and can_send_signal(state, "Liquidity Sweep", direction):
        signals_to_send.append(("Liquidity Sweep", direction, level or ""))

    # Strategy 3: EMA + RSI
    direction, level = check_ema_rsi_signal(candles)
    if direction and can_send_signal(state, "EMA+RSI", direction):
        signals_to_send.append(("EMA+RSI", direction, level or ""))

    # Send all valid signals
    for strategy_name, direction, level_info in signals_to_send:
        entry = current_price

        # Use original SL/TP for VWAP+VP, new values for the other strategies
        if strategy_name == "VWAP+VP":
            sl_pts = VWAP_SL_POINTS
            tp_pts = VWAP_TP_POINTS
        else:
            sl_pts = NEW_SL_POINTS
            tp_pts = NEW_TP_POINTS

        if direction == "long":
            sl_price = round(entry - sl_pts, 2)
            tp_price = round(entry + tp_pts, 2)
        else:
            sl_price = round(entry + sl_pts, 2)
            tp_price = round(entry - tp_pts, 2)

        msg = format_signal(direction, entry, strategy_name, level_info, sl_price, tp_price, sl_pts, tp_pts)
        send_telegram(msg)
        open_trade(direction, entry, sl_price, tp_price, strategy_name, level_info)
        record_signal(state, strategy_name, direction)
        print(f"[SIGNAL] {strategy_name} → {direction.upper()} @ {entry:.2f} | {level_info}")

    if not signals_to_send:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] No signals this run.")

    save_state(state)


if __name__ == "__main__":
    main()
