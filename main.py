"""
XAUUSD Balanced Multi-Strategy Bot (Improved Quality Version)
-------------------------------------------------------------
Goal: Higher quality signals (target 60-65% win rate, 8-12 signals/day)

Features:
- 5 Strategies with individual SL/TP
- 15-minute trend bias filter (only trade with higher timeframe)
- Session awareness:
    • London + New York → normal filters
    • Asian session → stricter (requires confluence)
- Exact entry price
- Clear strategy name in every signal
"""

import json
import os
import statistics
import sys
import time
import requests
import lzma
import struct
from datetime import datetime, timezone, date, timedelta

# ============== CONFIG ==============
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOL = "XAUUSD"
LOOKBACK_BARS = 250

# Risk per strategy
STRATEGY_RISK = {
    "VWAP+VP":         {"sl": 6.0,  "tp": 8.0},
    "Liquidity Sweep": {"sl": 8.0,  "tp": 10.0},
    "EMA+RSI":         {"sl": 8.0,  "tp": 10.0},
    "Order Block":     {"sl": 8.0,  "tp": 12.0},
    "FVG":             {"sl": 8.0,  "tp": 12.0},
}

COOLDOWN_SECONDS = 240

# Session times (UTC)
# Asian: 00:00 - 07:00
# London: 07:00 - 12:00
# NY: 12:00 - 21:00
# Overlap London-NY is strongest

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

ZONE_DEDUP_SECONDS = 3600  # don't re-alert the same OB/FVG zone within 1 hour

def load_state():
    return load_json(STATE_FILE, {
        "last_signal_time": {},
        "last_signal_direction": {},
        "last_summary_date": None,
        "zone_last_fired": {},
    })

def save_state(state):
    save_json(STATE_FILE, state)

def zone_already_fired(state, zone_key):
    last = state.setdefault("zone_last_fired", {}).get(zone_key)
    return last is not None and (time.time() - last) < ZONE_DEDUP_SECONDS

def mark_zone_fired(state, zone_key):
    state.setdefault("zone_last_fired", {})[zone_key] = time.time()


# ============== TRADE MGMT ==============
def open_trade(direction, entry, sl, tp, strategy, info=""):
    trades = load_trades()
    trades.append({
        "direction": direction,
        "entry": entry,
        "sl_price": sl,
        "tp1_price": tp,
        "strategy": strategy,
        "level": info,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "closed_at": None,
        "result": None,
        "pnl_points": None,
    })
    save_trades(trades)

def check_open_trades(price):
    trades = load_trades()
    changed = False
    for t in trades:
        if t["status"] != "open":
            continue
        if t["direction"] == "long":
            if price >= t["tp1_price"]:
                t.update(status="closed", result="win", pnl_points=round(t["tp1_price"]-t["entry"],2),
                         closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                notify_result(t)
            elif price <= t["sl_price"]:
                t.update(status="closed", result="loss", pnl_points=round(t["sl_price"]-t["entry"],2),
                         closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                notify_result(t)
        else:
            if price <= t["tp1_price"]:
                t.update(status="closed", result="win", pnl_points=round(t["entry"]-t["tp1_price"],2),
                         closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                notify_result(t)
            elif price >= t["sl_price"]:
                t.update(status="closed", result="loss", pnl_points=round(t["entry"]-t["sl_price"],2),
                         closed_at=datetime.now(timezone.utc).isoformat())
                changed = True
                notify_result(t)
    if changed:
        save_trades(trades)

def notify_result(t):
    emoji = "✅ WIN" if t["result"] == "win" else "❌ LOSS"
    msg = (f"<b>{emoji}</b> — XAUUSD {t['direction'].upper()}\n"
           f"Strategy: {t.get('strategy')}\n"
           f"Entry: {t['entry']:.2f} | {t.get('level','-')}\n"
           f"Result: {t['pnl_points']:+.2f} pts")
    send_telegram(msg)

def send_daily_summary(for_date):
    trades = load_trades()
    day_str = for_date.isoformat()
    day_trades = [t for t in trades if t["status"]=="closed" and t.get("closed_at","")[:10]==day_str]
    if not day_trades:
        send_telegram(f"<b>📊 Daily Summary — {day_str}</b>\nNo closed trades.")
        return
    wins = [t for t in day_trades if t["result"]=="win"]
    losses = [t for t in day_trades if t["result"]=="loss"]
    total = sum(t["pnl_points"] for t in day_trades)
    wr = len(wins)/len(day_trades)*100
    by = {}
    for t in day_trades:
        s = t.get("strategy","?")
        by.setdefault(s, {"w":0,"l":0,"pts":0.0})
        if t["result"]=="win": by[s]["w"] += 1
        else: by[s]["l"] += 1
        by[s]["pts"] += t["pnl_points"]
    lines = "\n".join(f"• {s}: {v['w']}W/{v['l']}L ({v['pts']:+.1f})" for s,v in by.items())
    send_telegram(f"<b>📊 Daily Summary — {day_str}</b>\n"
                  f"Total: {len(day_trades)} | {len(wins)}W/{len(losses)}L | WR {wr:.1f}% | Net {total:+.2f}\n\n{lines}")


# ============== DATA ==============
def fetch_spot():
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get("https://data-asg.goldprice.org/dbXRates/USD", headers=headers, timeout=8)
        return float(r.json()["items"][0]["xauPrice"])
    except: pass
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", headers=headers, timeout=8)
        return float(r.json()["price"])
    except: pass
    return None

def get_klines_dukascopy(limit=LOOKBACK_BARS):
    """Fetch genuine XAUUSD ticks from Dukascopy and aggregate them into 5m candles."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=28)
    ticks = []
    rec = struct.Struct(">IIIff")
    cursor = start.replace(minute=0, second=0, microsecond=0)
    end_hour = now.replace(minute=0, second=0, microsecond=0)
    while cursor <= end_hour:
        url = (f"https://datafeed.dukascopy.com/datafeed/XAUUSD/"
               f"{cursor.year}/{cursor.month-1:02d}/{cursor.day:02d}/{cursor.hour:02d}h_ticks.bi5")
        try:
            r = requests.get(url, timeout=12)
            if r.ok and r.content:
                raw = lzma.decompress(r.content)
                for off in range(0, len(raw) - rec.size + 1, rec.size):
                    ms, ask_i, bid_i, ask_vol, bid_vol = rec.unpack_from(raw, off)
                    ts = cursor + timedelta(milliseconds=ms)
                    price = (ask_i + bid_i) / 2000.0
                    volume = max(float(ask_vol) + float(bid_vol), 0.0)
                    ticks.append((ts, price, volume))
        except Exception as e:
            print(f"[Dukascopy fail] {cursor.isoformat()} {e}")
        cursor += timedelta(hours=1)
    if not ticks:
        raise RuntimeError("No XAUUSD tick data returned by Dukascopy")
    ticks.sort(key=lambda x: x[0])
    buckets = {}
    for ts, price, volume in ticks:
        bucket = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
        if bucket not in buckets:
            buckets[bucket] = {"open_time": int(bucket.timestamp()*1000), "open": price, "high": price, "low": price, "close": price, "volume": volume}
        else:
            c = buckets[bucket]; c["high"] = max(c["high"], price); c["low"] = min(c["low"], price); c["close"] = price; c["volume"] += volume
    candles = [buckets[k] for k in sorted(buckets)]
    current_bucket = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    candles = [c for c in candles if c["open_time"] < int(current_bucket.timestamp()*1000)]
    if len(candles) < min(limit, 30):
        raise RuntimeError(f"Only {len(candles)} XAUUSD 5m candles available")
    return candles[-limit:]

def get_klines(limit=LOOKBACK_BARS):
    return get_klines_dukascopy(limit)


# ============== INDICATORS ==============
def ema(values, period):
    if len(values) < period: return [None]*len(values)
    out = [None]*(period-1)
    s = sum(values[:period])/period
    out.append(s)
    k = 2/(period+1)
    for i in range(period, len(values)):
        out.append((values[i]-out[-1])*k + out[-1])
    return out

def rsi(closes, period=14):
    if len(closes) < period+1: return [None]*len(closes)
    out = [None]*period
    gains, losses = [], []
    for i in range(1, period+1):
        ch = closes[i]-closes[i-1]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    ag, al = sum(gains)/period, sum(losses)/period
    out.append(100 if al==0 else 100-(100/(1+ag/al)))
    for i in range(period+1, len(closes)):
        ch = closes[i]-closes[i-1]
        ag = (ag*(period-1)+max(ch,0))/period
        al = (al*(period-1)+max(-ch,0))/period
        out.append(100 if al==0 else 100-(100/(1+ag/al)))
    return out

def session_vwap(candles):
    vals, cum_pv, cum_vol, day = [], 0.0, 0.0, None
    for c in candles:
        d = datetime.fromtimestamp(c["open_time"]/1000, tz=timezone.utc).date()
        if d != day:
            day = d; cum_pv = cum_vol = 0.0
        typ = (c["high"]+c["low"]+c["close"])/3
        cum_pv += typ * c["volume"]
        cum_vol += c["volume"]
        vals.append(cum_pv/cum_vol if cum_vol else typ)
    return vals

def vwap_slope(vals, n=5):
    if len(vals) < n+1: return "flat"
    d = vals[-1] - vals[-n]
    return "rising" if d > 0.05 else "falling" if d < -0.05 else "flat"

def volume_profile(candles, bins=10):
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    mx, mn = max(highs), min(lows)
    if mx == mn: return None
    size = (mx-mn)/bins
    vols = [0.0]*bins
    for c in candles:
        idx = max(0, min(bins-1, int((c["close"]-mn)/size)))
        vols[idx] += c["volume"]
    poc_i = vols.index(max(vols))
    poc = mn + (poc_i+0.5)*size
    total = sum(vols)
    target = total * 0.70
    captured = vols[poc_i]
    lo = hi = poc_i
    while captured < target and (lo>0 or hi<bins-1):
        bel = vols[lo-1] if lo>0 else -1
        abv = vols[hi+1] if hi<bins-1 else -1
        if abv >= bel:
            hi += 1; captured += vols[hi]
        else:
            lo -= 1; captured += vols[lo]
    return {"poc": round(poc,2), "vah": round(mn+(hi+1)*size,2), "val": round(mn+lo*size,2)}


# ============== SESSION & BIAS ==============
def get_session(utc_hour):
    if 0 <= utc_hour < 7:
        return "Asian"
    if 7 <= utc_hour < 12:
        return "London"
    if 12 <= utc_hour < 21:
        return "NewYork"
    return "Late"

def get_15m_bias(candles_5m):
    """Calculate the 15m bias from actual 15m closes aggregated from XAUUSD 5m candles."""
    if len(candles_5m) < 75:
        return "neutral"
    groups = {}
    for c in candles_5m:
        ts = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc)
        bucket = ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)
        groups[bucket] = c["close"]
    closes = [groups[k] for k in sorted(groups)]
    if len(closes) < 25:
        return "neutral"
    e9 = ema(closes, 9); e21 = ema(closes, 21)
    if e9[-1] is None or e21[-1] is None or e9[-3] is None:
        return "neutral"
    if e9[-1] > e21[-1] and e9[-1] > e9[-3]: return "bullish"
    if e9[-1] < e21[-1] and e9[-1] < e9[-3]: return "bearish"
    return "neutral"


# ============== STRATEGIES ==============
def near(price, level, tol=TOUCH_TOLERANCE):
    return abs(price - level) <= tol

def is_rejection(c, level, direction):
    bh = max(c["open"], c["close"])
    bl = min(c["open"], c["close"])
    if direction == "long":
        return c["low"] <= level + TOUCH_TOLERANCE and (bl - c["low"]) > 0 and c["close"] > bl
    return c["high"] >= level - TOUCH_TOLERANCE and (c["high"] - bh) > 0 and c["close"] < bh

def vol_ok(candles, idx):
    if idx < 10: return False
    avg = statistics.mean(c["volume"] for c in candles[idx-10:idx])
    return candles[idx]["volume"] >= avg * VOLUME_MULT

def check_vwap_vp(candles, vwap, profile):
    if not profile: return None, None
    last = candles[-1]
    slope = vwap_slope(vwap)
    for name, price in [("POC",profile["poc"]),("VAH",profile["vah"]),("VAL",profile["val"])]:
        if not near(last["close"], price): continue
        if slope=="rising" and is_rejection(last, price, "long") and vol_ok(candles, len(candles)-1):
            return "long", name
        if slope=="falling" and is_rejection(last, price, "short") and vol_ok(candles, len(candles)-1):
            return "short", name
    return None, None

def check_liquidity_sweep(candles):
    if len(candles) < SWING_LOOKBACK+3: return None, None
    window = candles[-(SWING_LOOKBACK+1):-1]
    sh = max(c["high"] for c in window)
    sl = min(c["low"] for c in window)
    last = candles[-1]
    if last["low"] < sl - SWEEP_TOLERANCE and last["close"] > sl and last["close"] > last["open"]:
        return "long", f"Sweep Low {sl:.2f}"
    if last["high"] > sh + SWEEP_TOLERANCE and last["close"] < sh and last["close"] < last["open"]:
        return "short", f"Sweep High {sh:.2f}"
    return None, None

def check_ema_rsi(candles):
    closes = [c["close"] for c in candles]
    if len(closes) < max(EMA_SLOW, RSI_PERIOD)+5: return None, None
    ef = ema(closes, EMA_FAST)
    es = ema(closes, EMA_SLOW)
    r = rsi(closes, RSI_PERIOD)
    if None in (ef[-1], es[-1], r[-1], ef[-2], es[-2]): return None, None
    if ef[-2] <= es[-2] and ef[-1] > es[-1] and r[-1] > 50:
        return "long", f"EMA Cross RSI {r[-1]:.1f}"
    if ef[-2] >= es[-2] and ef[-1] < es[-1] and r[-1] < 50:
        return "short", f"EMA Cross RSI {r[-1]:.1f}"
    return None, None

def check_order_block(candles, state):
    if len(candles) < OB_LOOKBACK+5: return None, None
    bodies = [abs(c["close"]-c["open"]) for c in candles[-OB_LOOKBACK-5:-1]]
    avg_body = statistics.mean(bodies) if bodies else 1.0
    last = candles[-1]

    for i in range(len(candles)-3, len(candles)-OB_LOOKBACK-1, -1):
        c = candles[i]
        if c["close"] < c["open"]:
            impulse = any(candles[j]["close"] > candles[j]["open"] and abs(candles[j]["close"]-candles[j]["open"]) > avg_body*OB_IMPULSE_MULT
                          for j in range(i+1, min(i+3, len(candles)-1)))
            if impulse and last["low"] <= c["high"] and last["high"] >= c["low"] and is_rejection(last, c["low"], "long"):
                zone_key = f"OB-long-{round(c['low'],1)}-{round(c['high'],1)}"
                if zone_already_fired(state, zone_key):
                    continue
                mark_zone_fired(state, zone_key)
                return "long", f"Bullish OB {c['low']:.2f}-{c['high']:.2f}"

    for i in range(len(candles)-3, len(candles)-OB_LOOKBACK-1, -1):
        c = candles[i]
        if c["close"] > c["open"]:
            impulse = any(candles[j]["close"] < candles[j]["open"] and abs(candles[j]["close"]-candles[j]["open"]) > avg_body*OB_IMPULSE_MULT
                          for j in range(i+1, min(i+3, len(candles)-1)))
            if impulse and last["low"] <= c["high"] and last["high"] >= c["low"] and is_rejection(last, c["high"], "short"):
                zone_key = f"OB-short-{round(c['low'],1)}-{round(c['high'],1)}"
                if zone_already_fired(state, zone_key):
                    continue
                mark_zone_fired(state, zone_key)
                return "short", f"Bearish OB {c['low']:.2f}-{c['high']:.2f}"
    return None, None

def check_fvg(candles, state):
    if len(candles) < 8: return None, None
    last = candles[-1]
    for i in range(len(candles)-2, max(len(candles)-15, 2), -1):
        c1, c3 = candles[i-2], candles[i]
        if c3["low"] > c1["high"] + FVG_MIN_GAP:
            if last["low"] <= c3["low"] and last["high"] >= c1["high"] and last["close"] > last["open"]:
                zone_key = f"FVG-long-{round(c1['high'],1)}-{round(c3['low'],1)}"
                if zone_already_fired(state, zone_key):
                    continue
                mark_zone_fired(state, zone_key)
                return "long", f"Bullish FVG {c1['high']:.2f}-{c3['low']:.2f}"
        if c3["high"] < c1["low"] - FVG_MIN_GAP:
            if last["high"] >= c3["high"] and last["low"] <= c1["low"] and last["close"] < last["open"]:
                zone_key = f"FVG-short-{round(c3['high'],1)}-{round(c1['low'],1)}"
                if zone_already_fired(state, zone_key):
                    continue
                mark_zone_fired(state, zone_key)
                return "short", f"Bearish FVG {c3['high']:.2f}-{c1['low']:.2f}"
    return None, None


# ============== TELEGRAM ==============
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] Missing credentials")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=12)
    except Exception as e:
        print(f"[TG] {e}")

def format_signal(direction, entry, strategy, info, sl, tp, sl_pts, tp_pts, session, bias):
    arrow = "🟢 BUY" if direction == "long" else "🔴 SELL"
    return (f"<b>{arrow} XAUUSD (5m)</b>\n"
            f"Strategy: <b>{strategy}</b>\n"
            f"Entry: {entry:.2f}\n"
            f"Info: {info}\n"
            f"SL: {sl:.2f} ({sl_pts} pts) | TP: {tp:.2f} ({tp_pts} pts)\n"
            f"Session: {session} | Bias: {bias}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

def can_send(state, strategy, direction):
    now = time.time()
    last_t = state.get("last_signal_time", {}).get(strategy, 0)
    last_d = state.get("last_signal_direction", {}).get(strategy)
    if now - last_t < COOLDOWN_SECONDS and last_d == direction:
        return False
    return True

def record(state, strategy, direction):
    state.setdefault("last_signal_time", {})[strategy] = time.time()
    state.setdefault("last_signal_direction", {})[strategy] = direction


# ============== MAIN ==============
def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Missing secrets", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    candles = get_klines()
    live_spot = fetch_spot()
    price = live_spot if live_spot is not None else candles[-1]["close"]
    now_utc = datetime.now(timezone.utc)
    session = get_session(now_utc.hour)
    bias = get_15m_bias(candles)

    check_open_trades(price)

    today = now_utc.date().isoformat()
    if state.get("last_summary_date") is None:
        state["last_summary_date"] = today
    elif today != state["last_summary_date"]:
        y,m,d = map(int, state["last_summary_date"].split("-"))
        send_daily_summary(date(y,m,d))
        state["last_summary_date"] = today

    # Collect raw signals
    raw = []

    vwap = session_vwap(candles)
    profile = volume_profile(candles[-LOOKBACK_BARS:])
    d, info = check_vwap_vp(candles, vwap, profile)
    if d: raw.append(("VWAP+VP", d, info or ""))

    d, info = check_liquidity_sweep(candles)
    if d: raw.append(("Liquidity Sweep", d, info or ""))

    d, info = check_ema_rsi(candles)
    if d: raw.append(("EMA+RSI", d, info or ""))

    d, info = check_order_block(candles, state)
    if d: raw.append(("Order Block", d, info or ""))

    d, info = check_fvg(candles, state)
    if d: raw.append(("FVG", d, info or ""))

    # Apply filters
    final = []
    for strategy, direction, info in raw:
        # 15m bias filter
        if bias == "bullish" and direction == "short":
            continue
        if bias == "bearish" and direction == "long":
            continue

        # Asian session → stricter (require at least one more confirming strategy of same direction)
        if session == "Asian":
            same_dir = [s for s,d,i in raw if d == direction]
            if len(same_dir) < 2:  # need confluence
                continue

        if can_send(state, strategy, direction):
            final.append((strategy, direction, info))

    # Send
    for strategy, direction, info in final:
        risk = STRATEGY_RISK[strategy]
        sl_pts, tp_pts = risk["sl"], risk["tp"]
        if direction == "long":
            sl = round(price - sl_pts, 2)
            tp = round(price + tp_pts, 2)
        else:
            sl = round(price + sl_pts, 2)
            tp = round(price - tp_pts, 2)

        msg = format_signal(direction, price, strategy, info, sl, tp, sl_pts, tp_pts, session, bias)
        send_telegram(msg)
        open_trade(direction, price, sl, tp, strategy, info)
        record(state, strategy, direction)
        print(f"[SIGNAL] {strategy} {direction.upper()} @ {price:.2f} | {session} | {bias}")

    if not final:
        print(f"[{now_utc.strftime('%H:%M:%S')}] No signals | Session={session} Bias={bias}")

    # Prune stale zone entries so state.json doesn't grow forever
    now_ts = time.time()
    state["zone_last_fired"] = {
        k: v for k, v in state.get("zone_last_fired", {}).items()
        if now_ts - v < ZONE_DEDUP_SECONDS
    }

    save_state(state)


if __name__ == "__main__":
    main()
