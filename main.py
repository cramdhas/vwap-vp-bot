"""
VWAP + Volume Profile (POC/VAH/VAL) Signal Bot -- GitHub Actions edition
-------------------------------------------------------------------------
Strategy: 5-minute timeframe only. No RSI.

This version runs ONCE per invocation (no infinite loop) because GitHub
Actions triggers it fresh every 5 minutes via a scheduled workflow.
State (open trades, daily summary tracking) persists across runs by
reading/writing JSON files in this repo -- the workflow commits any
changes back after each run.

Signal fires only when ALL of these align:
  1. LEVEL      -> price is touching/near POC, VAH, or VAL
  2. BIAS       -> VWAP slope agrees with trade direction
                   (rising VWAP = only longs, falling VWAP = only shorts)
  3. CONFIRM    -> the touching candle shows a rejection wick back into
                   the value area, AND its volume is above the recent
                   average (real interest, not just a wick pass-through)

Credentials come from environment variables (set as GitHub Secrets),
NOT hardcoded, since this repo may be public.
"""

import json
import os
import statistics
import sys
import requests
from datetime import datetime, timezone

# ============== CONFIG ==============
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOL_BINANCE = "PAXGUSDT"      # gold-pegged proxy, same source family as apex-signals-bot
INTERVAL = "5m"
LOOKBACK_BARS = 250              # matches your "250" volume-profile lookback setting
NUM_BINS = 10                    # matches your "10" bin count setting
VALUE_AREA_PCT = 0.70            # standard 70% value area around POC

TOUCH_TOLERANCE_POINTS = 3.0     # how close price must be to a level to count as a "touch"
VOLUME_CONFIRM_MULT = 1.3        # candle volume must be >= 1.3x recent average to confirm
SL_POINTS = 6.0
TP1_POINTS = 8.0
COOLDOWN_SECONDS = 300           # don't refire same-direction signal within 5 min

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
        "last_signal_time": 0,
        "last_signal_direction": None,
        "last_summary_date": None,
    })


def save_state(state):
    save_json(STATE_FILE, state)


# ============== TRADE MANAGEMENT ==============
def open_trade(direction, entry, sl_price, tp1_price, level_name):
    trades = load_trades()
    trades.append({
        "direction": direction,
        "entry": entry,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
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
    msg = (
        f"<b>{emoji}</b> — XAUUSD {trade['direction'].upper()}\n"
        f"Entry: {trade['entry']:.2f} | Level: {trade['level']}\n"
        f"Closed: {trade['pnl_points']:+.2f} pts"
    )
    send_telegram(msg)
    print(f"[TRADE CLOSED] {trade['direction']} {trade['result']} {trade['pnl_points']:+.2f} pts")


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

    msg = (
        f"<b>📊 Daily Summary — {day_str}</b>\n"
        f"Total signals closed: {len(day_trades)}\n"
        f"Wins: {len(wins)} | Losses: {len(losses)}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"Net points: {total_points:+.2f}"
    )
    send_telegram(msg)
    print(f"[DAILY SUMMARY SENT] {day_str}: {len(wins)}W/{len(losses)}L, {total_points:+.2f} pts")


# ============== DATA FETCH ==============
def fetch_spot_gold_price():
    """
    Fetches a real XAU/USD spot price (not a crypto proxy) to calibrate
    the PAXG-based candle series against. Tries two free, no-signup
    sources. Returns None if both fail (caller should skip calibration).
    """
    try:
        r = requests.get("https://data-asg.goldprice.org/dbXRates/USD", timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data["items"][0]["xauPrice"])
    except Exception as e:
        print(f"[goldprice.org spot fetch failed] {e}")

    try:
        r = requests.get("https://api.metals.live/v1/spot/gold", timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data[-1][1])
    except Exception as e:
        print(f"[metals.live spot fetch failed] {e}")

    return None


def calibrate_candles_to_spot(candles):
    """
    Shifts the whole PAXG-based candle series by a constant offset so its
    last close matches real XAU/USD spot. Preserves the shape (VWAP slope,
    volume profile) while fixing the absolute price level that gets shown
    in signals and used for SL/TP.
    """
    spot = fetch_spot_gold_price()
    if spot is None:
        print("[WARNING] Could not fetch real spot gold price this run -- "
              "using uncalibrated PAXG price, which may not match your broker.")
        return candles

    offset = spot - candles[-1]["close"]
    print(f"[Calibration] PAXG close={candles[-1]['close']:.2f}, "
          f"real spot={spot:.2f}, offset={offset:+.2f}")

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



    """Kraken public API -- PAXG/USD, 5-minute candles. No geo-block for GitHub Actions IPs."""
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": "PAXGUSD", "interval": 5}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")

    result = data["result"]
    pair_key = next(k for k in result.keys() if k != "last")
    raw = result[pair_key][-limit:]

    candles = []
    for row in raw:
        # Kraken OHLC row: [time(sec), open, high, low, close, vwap, volume, count]
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
    """OKX public API fallback -- PAXG-USDT, 5-minute candles."""
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": "PAXG-USDT", "bar": "5m", "limit": str(limit)}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("data", [])
    if not raw:
        raise RuntimeError(f"OKX returned no data: {data}")

    # OKX returns newest-first; reverse to oldest-first to match our convention
    raw = list(reversed(raw))
    candles = []
    for row in raw:
        # OKX row: [ts_ms, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
        candles.append({
            "open_time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        })
    return candles


def get_klines(symbol, interval, limit):
    """
    Tries Kraken first, falls back to OKX if Kraken fails for any reason.
    (Binance is deliberately not used here -- it returns HTTP 451 to
    GitHub Actions' server IPs.)
    """
    try:
        return get_klines_kraken(limit)
    except Exception as e:
        print(f"[Kraken fetch failed, falling back to OKX] {e}")
        return get_klines_okx(limit)


# ============== VWAP ==============
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

        typical_price = (c["high"] + c["low"] + c["close"]) / 3.0
        cum_pv += typical_price * c["volume"]
        cum_vol += c["volume"]
        vwap = cum_pv / cum_vol if cum_vol > 0 else typical_price
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


# ============== VOLUME PROFILE (POC / VAH / VAL) ==============
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


# ============== SIGNAL LOGIC ==============
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


def check_signal(candles, vwap_values, profile):
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


# ============== TELEGRAM ==============
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram error] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env vars")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[Telegram error] {e}")


def format_signal(direction, entry, level_name, level_price, sl_price, tp1_price):
    arrow = "🟢 BUY" if direction == "long" else "🔴 SELL"
    return (
        f"<b>{arrow} XAUUSD (5m)</b>\n"
        f"Strategy: VWAP + Volume Profile\n"
        f"Entry: ~{entry:.2f}\n"
        f"Level: {level_name} @ {level_price:.2f}\n"
        f"SL: {sl_price:.2f} ({SL_POINTS} pts) | TP1: {tp1_price:.2f} ({TP1_POINTS} pts)\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )


# ============== SINGLE-RUN MAIN (for GitHub Actions cron) ==============
def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set. "
              "Add them as GitHub repo Secrets.", file=sys.stderr)
        sys.exit(1)

    state = load_state()

    candles = get_klines(SYMBOL_BINANCE, INTERVAL, LOOKBACK_BARS)
    candles = calibrate_candles_to_spot(candles)
    current_price = candles[-1]["close"]

    # 1. Check open virtual trades against current price
    check_open_trades(current_price)

    # 2. Daily summary rollover check (UTC midnight)
    today = datetime.now(timezone.utc).date().isoformat()
    last_summary_date = state.get("last_summary_date")
    if last_summary_date is None:
        state["last_summary_date"] = today
    elif today != last_summary_date:
        from datetime import date
        y, m, d = map(int, last_summary_date.split("-"))
        send_daily_summary(date(y, m, d))
        state["last_summary_date"] = today

    # 3. Scan for a new signal
    vwap_values = compute_session_vwap(candles)
    profile = compute_volume_profile(candles[-LOOKBACK_BARS:])
    direction, level_name = check_signal(candles, vwap_values, profile)

    import time
    now = time.time()
    last_signal_time = state.get("last_signal_time", 0)
    last_signal_direction = state.get("last_signal_direction")

    if direction and (now - last_signal_time > COOLDOWN_SECONDS or direction != last_signal_direction):
        entry_price = current_price
        level_price = profile[level_name.lower()]

        if direction == "long":
            sl_price = entry_price - SL_POINTS
            tp1_price = entry_price + TP1_POINTS
        else:
            sl_price = entry_price + SL_POINTS
            tp1_price = entry_price - TP1_POINTS

        msg = format_signal(direction, entry_price, level_name, level_price, sl_price, tp1_price)
        send_telegram(msg)
        open_trade(direction, entry_price, sl_price, tp1_price, level_name)

        print(f"[SIGNAL SENT] {direction} at {entry_price} ({level_name})")
        state["last_signal_time"] = now
        state["last_signal_direction"] = direction
    else:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] No signal this run.")

    save_state(state)


if __name__ == "__main__":
    main()
