"""
XAUUSD Multi-Strategy Telegram Bot
FVG and Doji strategies removed; Volume Profile strategies added.

SMA6 rule:
- BUY: previous closed M5 candle close <= previous SMA6,
        latest closed M5 candle close > latest SMA6,
        and SMA6 is rising.
- SELL: opposite conditions.
- The live/current M5 candle is excluded from calculations.

Volume Profile strategies (from "The BEST Volume Profile Trading Guide"):
Levels (POC / VAH / VAL, 70% value area) are built from the PREVIOUS day's
session and traded on the CURRENT session, M5 closed candles only.
- VP POC Bounce : prev session closed OUTSIDE the value area, price pulls
                  back to the POC and prints a confirmation candle
                  (engulfing / rejection) -> trade in the direction of the
                  prior session's close.
- VP Reversal   : prev session closed INSIDE the value area, price closes
                  outside VAH/VAL then closes back inside -> trade back
                  across the value area toward the opposite edge.
- VP Breakout   : price closes significantly outside the value area, pulls
                  back to the VA edge and holds, then breaks structure ->
                  trade the breakout direction.

All other strategies use a fixed SL 7 / TP 6 (single target, no breakeven);
the Volume Profile strategies keep structure SL / 2R TP.

Other strategies retained:
VWAP+VP, Liquidity Sweep, EMA+RSI, SMA6, MA4/45 Pullback,
MA3/25 Cross, SMA18 Touch, SMA13/EMA18, Dynamic Grid, HalfTrend. SMA6 and HalfTrend: no 1H filter, SL 10 / TP 8.
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
# 800 M5 bars (~66h) so the whole previous day's session is always in the
# window for the Volume Profile strategies (500 bars cut it off late in the day).
LOOKBACK_BARS = 800
POINT_SIZE = 1.0

COOLDOWN_SECONDS = 240
ZONE_DEDUP_SECONDS = 3600

# Global signal protection: one signal per completed M5 candle and a
# 5-minute cooldown across all strategies to prevent duplicate/whipsaw alerts.
GLOBAL_SIGNAL_COOLDOWN_SECONDS = 300

ATR_PERIOD = 14
MIN_SL_POINTS = 6.0
MAX_SL_POINTS = 10.0
MIN_TP_POINTS = 8.0
MAX_TP_POINTS = 12.0
BE_BUFFER = 0.50

# ---- Global fixed SL/TP for every strategy EXCEPT the Volume Profile ones ----
# SL 7 points, TP 6 points, single target, no breakeven.
FIXED_SL_POINTS = 7.0
FIXED_TP_POINTS = 6.0

# ---- Volume-Trend Order Block Engine [BigBeluga] strategy (5m only) ----
# Settings matched to the TradingView chart status line "50 2.5 3 50 5":
#   Volatility SMA Length 50, Multiplier 2.5, Pivot Strength 5,
#   Delete OB on Complete Break = on, Min Buy % = Min Sell % = 50.
# The OB box is drawn only AFTER pivot_strength (5) bars confirm the pivot;
# that confirmation candle is the signal bar (entry = its close).
# Entry mode "confirm": trade the moment a new order block is CONFIRMED
#   in the direction of the box: bullish box with Buy % >= Min Buy -> BUY,
#   bearish box with Sell % >= Min Sell -> SELL.
# Entry mode "retest": the indicator's retest marker instead (old behaviour).
OBE_ENTRY_MODE = "video"
OBE_STRATEGIES = ("OB Engine 5m",)
OBE_TIMEFRAMES = {"OB Engine 5m": "5m"}
OBE_ST_LENGTH = 50
OBE_ST_MULT = 2.5
OBE_PIVOT = 5                 # box appears only after 5 candles close past the pivot
OBE_DELETE_ON_BREAK = True
OBE_MIN_BUY_PCT = 50.0        # bullish box needs Buy %  >= 50
OBE_MIN_SELL_PCT = 50.0       # bearish box needs Sell % >= 50
OBE_SL_POINTS = 7.0
OBE_TP_POINTS = 6.0           # single target, no breakeven
# Video fallback: MACD histogram 13/34 when price pulls back near, but does
# not actually retest, the active order block. The video does not specify an
# exact "near" distance, so use half the active OB depth, with a 1-point
# minimum, capped at 3 points to avoid turning distant pullbacks into signals.
OBE_MACD_FAST = 13
OBE_MACD_SLOW = 34
OBE_MACD_NEAR_MIN = 1.0
OBE_MACD_NEAR_MAX = 3.0
OBE_MACD_NEAR_FRACTION = 0.50

# ---- Dynamic Grid [BigBeluga] standalone strategy ----
# HMA 150 baseline + Wilder ATR(100) * 1.6 grid step. Three levels are
# used because the video shows 1/2/3 as the default grid; the engine can
# support up to five levels. A valid Dynamic Grid signal is a completed
# candle crossing back through a lower band for BUY or an upper band for SELL,
# with the HMA slope agreeing with the trade direction.
DG_ENABLED = True
DG_HMA_LENGTH = 150
DG_ATR_LENGTH = 100
DG_ATR_MULT = 1.6
DG_NUM_LEVELS = 3
DG_MIN_GAP_BARS = 10
DG_CONFIRM_WINDOW_BARS = 3
DG_REQUIRE_TREND = True   # kept for compatibility; DG_MODE now controls this
# Transcript rule: price CLOSES beyond a band (level k), then the NEXT candle
# reverses colour (green for buy, red for sell) -> signal with degree k.
#   "reversion": counter-trend / range signals only, need degree >= DG_MIN_REVERSION_LEVEL
#   "trend"    : only signals in the baseline direction (-k buy in green, +k sell in red)
#   "both"     : trend-aligned signals (any level) + counter-trend at high degree
DG_MODE = "both"
DG_MIN_REVERSION_LEVEL = 2

OBE_BAR_MS = {"5m": 300_000}
OBE_FEED_BARS = {"5m": 800}
# When the OB box first appears (confirmation candle = 5th close after pivot),
# fire the alert on the next scan. Allow up to pivot-length bars of lag so a
# poll that runs a few minutes late still catches the new box.
OBE_MAX_AGE_BARS = {"5m": 5}
# Freshness guards (wall-clock, minutes).
OBE_MAX_FEED_LAG_MIN = {"5m": 12.0}      # now - newest candle open
OBE_MAX_SIGNAL_AGE_MIN = {"5m": 30.0}    # now - signal candle CLOSE
OBE_FEED_SOURCE = {}          # tf -> label of the feed used this run (shown in alerts)
OBE_CLOCK = None              # tests may set a callable returning now in ms
OBE_USE_1H_BIAS = False       # indicator already trades with its own trend
OBE_USE_GLOBAL_LIMIT = False  # OB Engine runs independently of other strategies
OBE_PLAN = {}                 # strategy -> signal details, set by the check

# ---- SMA13 + EMA18 smoothed trend-cross strategy ----
# Based on the attached TradingView setup: SMA(12) Close with EMA smoothing
# length 13. Signals are evaluated only on completed M5 candles.
SMA13_PERIOD = 13
SMA13_SMOOTH_PERIOD = 18
SMA13_SLOPE_LOOKBACK = 3
SMA13_MIN_SLOPE = 0.10       # points/bar; rejects flat/choppy MA movement
SMA13_MIN_GAP = 0.50         # minimum SMA13 vs smoothed-EMA separation
SMA13_CHOP_LOOKBACK = 4      # if the two lines recently crossed, skip signal
SMA13_FIXED_SL = 7.0
SMA13_FIXED_TP = 6.0
SMA13_COOLDOWN_BARS = 3
# Live-price integrity guard for this strategy only. A signal is blocked if
# the completed signal candle is too far from the current XAUUSD spot.
SMA13_MAX_LIVE_GAP = 5.0

# ---- HalfTrend Long/Short Signal Engine [BigBeluga] strategy ----
# Settings from the chart: Amplitude 12, Channel Deviation 2. Signal = the
# HalfTrend flips direction on the latest CLOSED candle (green line = BUY,
# red line = SELL). Fixed SL 10 / TP 8 points, single target, no breakeven.
HT_ENABLED = True
HT_AMPLITUDE = 12
HT_CHANNEL_DEVIATION = 2.0
HT_ATR_LENGTH = 100        # HalfTrend uses ATR(100) / 2 internally
HT_SL_POINTS = 10.0
HT_TP_POINTS = 8.0

# ---- Per-strategy fixed settings / filters ----
# These strategies skip the 1H bias filter and use their own fixed SL/TP
# (single target, no breakeven).
NO_BIAS_STRATEGIES = {"SMA6", "HalfTrend", "MSB-OB"}
STRATEGY_SL_TP_OVERRIDE = {
    "SMA6": (10.0, 8.0),
    "HalfTrend": (10.0, 8.0),
    # MSB-OB settings from the supplied TradingView screenshot.
    "MSB-OB": (10.0, 10.0),
}

# ---- Market Structure Break & Order Block (MSB-OB) ----
# Original MSB-OB inputs shown in the screenshot: ZigZag 9, Fib factor 0.5,
# ZigZag display on, old/broken boxes deleted.  The strategy uses the
# indicator's confirmed MSB direction as the entry signal. No additional
# trend/bias filter is applied. SL 10 / TP 10, no breakeven.
MSBOB_ENABLED = True
MSBOB_ZIGZAG_LEN = 9
MSBOB_FIB_FACTOR = 0.5
MSBOB_SL_POINTS = 10.0
MSBOB_TP_POINTS = 10.0

STRATEGY_ATR_CONFIG = {
    "VWAP+VP":         {"sl_mult": 1.2, "tp1_ratio": 0.75, "tp2_ratio": 1.25},
    "EMA+RSI":         {"sl_mult": 1.2, "tp1_ratio": 0.75, "tp2_ratio": 1.25},
    "Liquidity Sweep": {"sl_mult": 1.4, "tp1_ratio": 0.80, "tp2_ratio": 1.35},
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

# SMA18 touch strategy — price crossing a single SMA(18), filtered by
# the SMA's own slope so flat/chop touches are skipped.
SMA18_PERIOD = 18
SMA18_FIXED_SL = 7.0
SMA18_FIXED_TP1 = 7.0
SMA18_FIXED_TP2 = 11.0
SMA18_SLOPE_LOOKBACK = 3
SMA18_MIN_SLOPE = 0.02

# ---- Volume Profile strategies (previous-session POC / VAH / VAL) ----
# Settings taken from the video: FRVP, Rows Layout "Number of Rows",
# Row Size 60, Value Area Volume 70, VAH/VAL/POC on, profile drawn on the
# PREVIOUS day's session (chart on exchange time) and traded on the CURRENT one.
VP_STRATEGIES = ("VP POC Bounce", "VP Reversal", "VP Breakout")
VP_NUM_ROWS = 60              # FRVP "Number of Rows" = 60
VP_VALUE_AREA_PCT = 0.70      # Value Area Volume = 70%
VP_MIN_RANGE = 4.0            # skip profiles whose session range is tiny (points)
# Session ("day") boundary in UTC. Gold's exchange/broker day rolls over in the
# daily break (~22:00 UTC; 21:00-22:00 in summer), so 22 keeps each session whole.
# Set to 0 to use calendar UTC days instead.
VP_DAY_START_UTC_HOUR = 22
VP_MIN_SESSION_BARS = 150     # previous session needs >= 150 M5 bars (~12.5h)
VP_OUTSIDE_BUFFER = 0.50      # prev close must be this far outside VAH/VAL
VP_TOUCH_TOL = 0.60           # POC touch tolerance (points)
VP_BREAKOUT_LOOKBACK = 60     # M5 bars (~5h) searched for the breakout structure
VP_BREAKOUT_MIN_DIST = 2.0    # "significant" close beyond VAH/VAL (points)
VP_RETEST_TOL = 2.0           # pullback must come back within this of the VA edge
VP_HOLD_TOL = 0.5             # closes may not fall back through the edge by more
VP_SL_BUFFER = 0.5            # "slightly" beyond the structure level
VP_RR = 2.0                   # video: take profit = 2R on every setup
VP_MAX_SL = MAX_SL_POINTS     # skip a setup if its structure SL is wider than this
VP_REFIRE_SECONDS = 3600      # same strategy+direction may re-fire after 1 hour
VP_PLAN = {}                  # strategy -> (sl_pts, tp1_pts, tp2_pts), set by checks

MA_CROSS_SLOPE_LOOKBACK = 3  # bars back to measure the slow MA's slope
MA_CROSS_MIN_SLOPE = 0.02    # min pts/bar slope required — filters out flat chop
MA_CROSS_TOUCH_TOLERANCE = 0.05  # price must actually touch/cross MA25 (points)

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
    """
    Close open OB Engine trades from candle highs/lows instead of a single
    spot price. The bot only looks at the market every ~5 minutes, and a
    7-point SL/TP can be touched and left again in between.
    Starts at the first candle AFTER the signal candle; if one candle touches
    both SL and TP the trade counts as a loss (same rule as the backtest).
    """
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
            if t.get("single_target", False) and price >= t["tp1_price"]:
                t["pnl_points"] = price_to_points(t["tp1_price"] - entry)
                t.update(
                    status="closed",
                    result="win",
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
                changed = True
                save_trades(trades)
                notify_result(t)
            elif not t.get("tp1_hit", False) and price >= t["tp1_price"]:
                t["tp1_hit"] = True
                if t.get("breakeven_enabled", True):
                    t["sl_price"] = round(entry + BE_BUFFER, 2)
                t["pnl_points"] += price_to_points(
                    (t["tp1_price"] - entry) * 0.5
                )
                changed = True
                save_trades(trades)
                if t.get("breakeven_enabled", True):
                    send_telegram(
                        f"🎯 <b>TP1 HIT (+{round(t['tp1_price'] - entry, 2)} pts) — "
                        f"{t['strategy']}</b>\n"
                        f"50% closed. SL moved to Breakeven: "
                        f"<b>{t['sl_price']:.2f}</b>"
                    )
                else:
                    send_telegram(
                        f"🎯 <b>TP1 HIT (+{round(t['tp1_price'] - entry, 2)} pts) — "
                        f"{t['strategy']}</b>\n"
                        f"50% closed. Breakeven is disabled for this strategy."
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
            if t.get("single_target", False) and price <= t["tp1_price"]:
                t["pnl_points"] = price_to_points(entry - t["tp1_price"])
                t.update(
                    status="closed",
                    result="win",
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
                changed = True
                save_trades(trades)
                notify_result(t)
            elif not t.get("tp1_hit", False) and price <= t["tp1_price"]:
                t["tp1_hit"] = True
                if t.get("breakeven_enabled", True):
                    t["sl_price"] = round(entry - BE_BUFFER, 2)
                t["pnl_points"] += price_to_points(
                    (entry - t["tp1_price"]) * 0.5
                )
                changed = True
                save_trades(trades)
                if t.get("breakeven_enabled", True):
                    send_telegram(
                        f"🎯 <b>TP1 HIT (+{round(entry - t['tp1_price'], 2)} pts) — "
                        f"{t['strategy']}</b>\n"
                        f"50% closed. SL moved to Breakeven: "
                        f"<b>{t['sl_price']:.2f}</b>"
                    )
                else:
                    send_telegram(
                        f"🎯 <b>TP1 HIT (+{round(entry - t['tp1_price'], 2)} pts) — "
                        f"{t['strategy']}</b>\n"
                        f"50% closed. Breakeven is disabled for this strategy."
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


def halftrend_compute(candles, amplitude=None, dev_mult=None):
    """
    HalfTrend (the engine behind the BigBeluga HalfTrend Long/Short indicator).

    Tracks the highest high / lowest low over `amplitude` bars plus the SMA of
    highs/lows. The trend flips DOWN when the high-SMA drops under the tracked
    swing low and the candle closes under the previous low, and flips UP when
    the low-SMA rises over the tracked swing high and the candle closes over the
    previous high. The HalfTrend line is a ratchet that only moves with the
    trend. A signal is the bar on which the trend flips (needs ATR(100) warm-up).

    Returns dict(trend, line, signal) lists; signal is "long" / "short" / None.
    """
    amplitude = amplitude or HT_AMPLITUDE
    dev_mult = HT_CHANNEL_DEVIATION if dev_mult is None else dev_mult
    n = len(candles)
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    atr = _wilder_atr(candles, HT_ATR_LENGTH)

    trend_list = [0] * n
    line_list = [None] * n
    signal = [None] * n
    if n == 0:
        return {"trend": trend_list, "line": line_list, "signal": signal}

    trend = 0
    next_trend = 0
    max_low = lows[0]
    min_high = highs[0]
    up = None
    down = None
    prev_trend = None

    for i in range(n):
        start = max(0, i - amplitude + 1)
        high_price = max(highs[start:i + 1])
        low_price = min(lows[start:i + 1])
        highma = sum(highs[start:i + 1]) / (i + 1 - start)
        lowma = sum(lows[start:i + 1]) / (i + 1 - start)
        prev_low = lows[i - 1] if i > 0 else lows[i]
        prev_high = highs[i - 1] if i > 0 else highs[i]
        atr2 = atr[i] / 2 if atr[i] is not None else None

        if next_trend == 1:
            max_low = max(low_price, max_low)
            if highma < max_low and closes[i] < prev_low:
                trend = 1
                next_trend = 0
                min_high = high_price
        else:
            min_high = min(high_price, min_high)
            if lowma > min_high and closes[i] > prev_high:
                trend = 0
                next_trend = 1
                max_low = low_price

        arrow = False
        if trend == 0:
            if prev_trend is not None and prev_trend != 0:
                up = down if down is not None else 0.0
                arrow = atr2 is not None
            else:
                up = max_low if up is None else max(max_low, up)
            line_list[i] = up
            if arrow and prev_trend == 1:
                signal[i] = "long"
        else:
            if prev_trend is not None and prev_trend != 1:
                down = up if up is not None else 0.0
                arrow = atr2 is not None
            else:
                down = min_high if down is None else min(min_high, down)
            line_list[i] = down
            if arrow and prev_trend == 0:
                signal[i] = "short"

        trend_list[i] = trend
        prev_trend = trend

    return {"trend": trend_list, "line": line_list, "signal": signal}


def check_halftrend(candles, state):
    """
    HalfTrend Long/Short: BUY when the HalfTrend flips up on the latest CLOSED
    candle, SELL when it flips down. One alert per flip candle (zone-deduped).
    SL/TP are fixed (8 / 8 points) and applied in main().
    """
    if not HT_ENABLED:
        return None, None
    if len(candles) < HT_ATR_LENGTH + HT_AMPLITUDE + 5:
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

    close = candles[last]["close"]
    line = ht["line"][last]
    side = "Up" if direction == "long" else "Down"
    return direction, (
        f"HalfTrend Flip {side} @ {close:.2f} "
        f"(line {line:.2f}, amp {HT_AMPLITUDE}, dev {HT_CHANNEL_DEVIATION:g})"
    )


def msbob_compute(candles):
    """
    MSB-OB (EmreKb) market-structure engine adapted from the open-source
    TradingView logic.

    Inputs from the user's screenshot:
      ZigZag Length = 9
      Fib Factor for breakout confirmation = 0.5

    The original indicator changes `market` only after a confirmed structural
    break, while `alert_market` additionally requires the CLOSED candle to
    close beyond the Fib-confirmed breakout level.  We use that confirmed
    `alert_market` transition as the BUY/SELL strategy signal.
    """
    n = len(candles)
    z = MSBOB_ZIGZAG_LEN
    fib = MSBOB_FIB_FACTOR
    if n < z * 3 + 10:
        return None

    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    opens = [float(c["open"]) for c in candles]

    # The Pine script starts its pivot arrays with five NA slots and appends
    # pivots only when the zigzag trend changes. Keep the same useful state in
    # Python as a list of (price, index) pairs.
    high_pivots = []
    low_pivots = []

    trend = 1
    market = 1
    alert_market = 1
    last_market_l0 = None
    last_market_h0 = None
    last_alert = 1
    signal = None

    # Pine's ta.barssince(condition): number of bars since the most recent
    # true condition. For the current bar, the source condition is to_up[1]
    # / to_down[1].
    last_to_up = None
    last_to_down = None

    def rolling_extreme(values, end_i, length, is_min):
        length = max(1, int(length))
        start = max(0, end_i - length + 1)
        window = values[start:end_i + 1]
        return (min(window) if is_min else max(window), start)

    def most_recent_equal(values, end_i, target, length):
        length = max(1, int(length))
        start = max(0, end_i - length + 1)
        for j in range(end_i, start - 1, -1):
            if abs(values[j] - target) <= 1e-12:
                return j
        return end_i

    for i in range(n):
        start = max(0, i - z + 1)
        to_up = highs[i] >= max(highs[start:i + 1])
        to_down = lows[i] <= min(lows[start:i + 1])

        prev_trend = trend
        if trend == 1 and to_down:
            trend = -1
        elif trend == -1 and to_up:
            trend = 1

        # barssince(to_up[1]) / barssince(to_down[1])
        up_since = 1 if last_to_up is None else max(1, i - last_to_up)
        down_since = 1 if last_to_down is None else max(1, i - last_to_down)

        low_val, _ = rolling_extreme(lows, i, up_since, True)
        low_index = most_recent_equal(lows, i, low_val, up_since)
        high_val, _ = rolling_extreme(highs, i, down_since, False)
        high_index = most_recent_equal(highs, i, high_val, down_since)

        if trend != prev_trend:
            if trend == 1:
                low_pivots.append((low_val, low_index))
                if len(low_pivots) > 5:
                    low_pivots.pop(0)
            else:
                high_pivots.append((high_val, high_index))
                if len(high_pivots) > 5:
                    high_pivots.pop(0)

        # Most recent two confirmed pivots of each side.
        h0 = high_pivots[-1] if len(high_pivots) >= 1 else None
        h1 = high_pivots[-2] if len(high_pivots) >= 2 else None
        l0 = low_pivots[-1] if len(low_pivots) >= 1 else None
        l1 = low_pivots[-2] if len(low_pivots) >= 2 else None

        if h0 and h1 and l0 and l1:
            h0p, h0i = h0
            h1p, h1i = h1
            l0p, l0i = l0
            l1p, l1i = l1

            # `last_l0` / `last_h0` in the original script are the pivot
            # values at the previous market-state transition.
            pivots_changed = not (
                last_market_l0 is not None
                and last_market_h0 is not None
                and abs(last_market_l0 - l0p) <= 1e-12
                and abs(last_market_h0 - h0p) <= 1e-12
            )

            new_market = market
            if pivots_changed:
                if (
                    market == 1
                    and l0p < l1p
                    and l0p < l1p - abs(h0p - l1p) * fib
                ):
                    new_market = -1
                elif (
                    market == -1
                    and h0p > h1p
                    and h0p > h1p + abs(h1p - l0p) * fib
                ):
                    new_market = 1

            if new_market != market:
                market = new_market
                last_market_l0 = l0p
                last_market_h0 = h0p

            # The alert version additionally requires trend direction and a
            # CLOSED candle beyond the Fib-confirmed level.
            new_alert = alert_market
            if pivots_changed:
                if (
                    alert_market == 1
                    and trend == -1
                    and closes[i] < l0p
                    and closes[i] < l0p - abs(h0p - l0p) * fib
                ):
                    new_alert = -1
                elif (
                    alert_market == -1
                    and trend == 1
                    and closes[i] > h0p
                    and closes[i] > h0p + abs(h0p - l0p) * fib
                ):
                    new_alert = 1

            # Keep the pivot transition values available even if the alert
            # has not fired yet; this mirrors valuewhen(change(market), ...)
            # only when market itself actually changes.
            if new_alert != alert_market:
                alert_market = new_alert
                if alert_market == 1:
                    signal = {
                        "direction": "long",
                        "index": i,
                        "level": h0p,
                        "pivot_high": h0p,
                        "pivot_low": l0p,
                        "ob": _msbob_order_block(opens, closes, highs, lows, h1i, l0i, i, bullish=True),
                    }
                else:
                    signal = {
                        "direction": "short",
                        "index": i,
                        "level": l0p,
                        "pivot_high": h0p,
                        "pivot_low": l0p,
                        "ob": _msbob_order_block(opens, closes, highs, lows, l1i, h0i, i, bullish=False),
                    }

        last_to_up = i if to_up else last_to_up
        last_to_down = i if to_down else last_to_down

    return signal


def _msbob_order_block(opens, closes, highs, lows, start_idx, end_idx, current_idx, bullish=True):
    """Return the latest opposite-colour candle used by the MSB-OB box."""
    lo = max(0, min(start_idx, end_idx))
    hi = min(current_idx, max(start_idx, end_idx))
    if lo > hi:
        return None
    for j in range(hi, lo - 1, -1):
        if bullish and opens[j] > closes[j]:
            return (highs[j], lows[j], j)
        if not bullish and opens[j] < closes[j]:
            return (highs[j], lows[j], j)
    return None


def check_msb_ob(candles, state):
    if not MSBOB_ENABLED:
        return None, None

    sig = msbob_compute(candles)
    if not sig:
        return None, None

    idx = sig["index"]
    candle_key = candles[idx].get("open_time", idx)
    direction = sig["direction"]
    key = f"MSBOB-{direction}-{candle_key}"
    if zone_already_fired(state, key):
        return None, None
    mark_zone_fired(state, key)

    ob = sig.get("ob")
    if ob:
        ob_hi, ob_lo, ob_idx = ob
        ob_text = f"OB {ob_lo:.2f}-{ob_hi:.2f}"
    else:
        ob_text = "OB n/a"

    side = "Bullish MSB / BUY" if direction == "long" else "Bearish MSB / SELL"
    return direction, (
        f"{side} | ZigZag {MSBOB_ZIGZAG_LEN} | Fib {MSBOB_FIB_FACTOR:g} | "
        f"break {sig['level']:.2f} | {ob_text}"
    )


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
    MA(3) / MA(25) price-touch strategy, evaluated on CLOSED candles only.

    IMPORTANT: a MA3/MA25 line crossover by itself is NOT a signal.
    The actual closed candle must touch/cross the MA25 price line. This
    prevents signals when the MA3/MA25 lines change position while price
    is still several points away from MA25.

    SELL: closed candle touches MA25, closes below it, and MA25 is falling.
    BUY:  closed candle touches MA25, closes above it, and MA25 is rising.

    Only the current closed candle is evaluated, and the normal strategy
    cooldown/open-trade filters in main() apply.
    """
    if len(candles) < MA148_PERIOD + MA_CROSS_SLOPE_LOOKBACK + 2:
        return None, None

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    opens = [c["open"] for c in candles]

    slow = [None] * len(closes)
    fast = [None] * len(closes)
    for i in range(MA148_PERIOD - 1, len(closes)):
        slow[i] = sum(closes[i - MA148_PERIOD + 1:i + 1]) / MA148_PERIOD
    for i in range(MA2_PERIOD - 1, len(closes)):
        fast[i] = sum(closes[i - MA2_PERIOD + 1:i + 1]) / MA2_PERIOD

    last = len(closes) - 1
    slope_ref = last - MA_CROSS_SLOPE_LOOKBACK
    if slow[last] is None or slope_ref < 0 or slow[slope_ref] is None:
        return None, None

    slope = (slow[last] - slow[slope_ref]) / MA_CROSS_SLOPE_LOOKBACK
    ma25 = slow[last]

    # REAL PRICE TOUCH: the closed candle's range must reach MA25.
    # The small tolerance only compensates for rounding, never for a
    # multi-point gap between price and the line.
    touched = (
        lows[last] <= ma25 + MA_CROSS_TOUCH_TOLERANCE
        and highs[last] >= ma25 - MA_CROSS_TOUCH_TOLERANCE
    )
    if not touched:
        return None, None

    close = closes[last]
    open_ = opens[last]

    # Require directional rejection/confirmation from the touched line.
    if close < ma25 and close <= open_ and slope <= -MA_CROSS_MIN_SLOPE:
        return "short", (
            f"MA3/25 Touch Down @ {close:.2f} "
            f"(MA25 {ma25:.2f}, slope {slope:+.2f}, "
            f"candle {lows[last]:.2f}-{highs[last]:.2f})"
        )

    if close > ma25 and close >= open_ and slope >= MA_CROSS_MIN_SLOPE:
        return "long", (
            f"MA3/25 Touch Up @ {close:.2f} "
            f"(MA25 {ma25:.2f}, slope {slope:+.2f}, "
            f"candle {lows[last]:.2f}-{highs[last]:.2f})"
        )

    return None, None


def check_sma18_touch(candles, live_price):
    """
    Price touch/cross of a single SMA(18), evaluated on CLOSED candles
    only (same reasoning as MA3/25 Cross — never mix a live tick into
    the historical comparison, only used for the entry price itself).

    A slope filter on the SMA18 avoids the flat/chop zones where price
    just oscillates sideways across the line with no real direction —
    matching the "don't take trades like the red marked areas" request.

    BUY:  price closed at/below SMA18, now closes above it, SMA18 rising.
    SELL: price closed at/above SMA18, now closes below it, SMA18 falling.
    """
    if len(candles) < SMA18_PERIOD + SMA18_SLOPE_LOOKBACK + 2:
        return None, None

    closes = [c["close"] for c in candles]

    sma = [None] * len(closes)
    for i in range(SMA18_PERIOD - 1, len(closes)):
        sma[i] = sum(closes[i - SMA18_PERIOD + 1:i + 1]) / SMA18_PERIOD

    last = len(closes) - 1
    prev = last - 1

    if sma[prev] is None or sma[last] is None:
        return None, None

    slope_ref = last - SMA18_SLOPE_LOOKBACK
    if slope_ref < 0 or sma[slope_ref] is None:
        return None, None

    slope = (sma[last] - sma[slope_ref]) / SMA18_SLOPE_LOOKBACK

    close_prev, close_now = closes[prev], closes[last]
    sma_prev, sma_now = sma[prev], sma[last]

    entry_price = live_price if live_price is not None else close_now

    if close_prev <= sma_prev and close_now > sma_now:
        if slope < SMA18_MIN_SLOPE:
            return None, None
        return "long", f"SMA18 Touch Up @ {entry_price:.2f} (SMA18 {sma_now:.2f}, slope {slope:+.2f})"

    if close_prev >= sma_prev and close_now < sma_now:
        if slope > -SMA18_MIN_SLOPE:
            return None, None
        return "short", f"SMA18 Touch Down @ {entry_price:.2f} (SMA18 {sma_now:.2f}, slope {slope:+.2f})"

    return None, None


# ================= VOLUME PROFILE STRATEGIES =================
def _session_date(c):
    """Session label of a candle (day boundary = VP_DAY_START_UTC_HOUR UTC)."""
    t = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc)
    return (t - timedelta(hours=VP_DAY_START_UTC_HOUR)).date()


def vp_session_profile(session_candles, num_rows=VP_NUM_ROWS, va_pct=VP_VALUE_AREA_PCT):
    """
    Fixed-range volume profile of one session (like TradingView's FRVP,
    video settings: Number of Rows layout, 60 rows, Value Area Volume 70%).
    The session's high-low range is split into 60 equal rows; each candle's
    volume is spread over the rows its high-low range covers (zero-volume
    feeds count 1 per candle). POC = busiest row, value area = rows around
    the POC holding 70% of volume, expanded by comparing the next two rows
    above vs below (TradingView's method).
    """
    if not session_candles:
        return None

    lo = min(c["low"] for c in session_candles)
    hi = max(c["high"] for c in session_candles)
    if hi - lo < VP_MIN_RANGE:
        return None

    n = num_rows
    row = (hi - lo) / n
    vols = [0.0] * n

    for c in session_candles:
        v = c["volume"] if c["volume"] > 0 else 1.0
        i0 = max(0, min(n - 1, int((c["low"] - lo) / row)))
        i1 = max(i0, min(n - 1, int((c["high"] - lo) / row)))
        share = v / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            vols[i] += share

    mid = (n - 1) / 2.0
    poc_i = max(range(n), key=lambda i: (vols[i], -abs(i - mid)))

    target = sum(vols) * va_pct
    acc = vols[poc_i]
    lo_i = hi_i = poc_i

    while acc < target and (lo_i > 0 or hi_i < n - 1):
        up = sum(vols[hi_i + 1:hi_i + 3]) if hi_i < n - 1 else -1
        dn = sum(vols[max(lo_i - 2, 0):lo_i]) if lo_i > 0 else -1

        if up >= dn:
            take = vols[hi_i + 1:hi_i + 3]
            hi_i += len(take)
            acc += sum(take)
        else:
            start = max(lo_i - 2, 0)
            acc += sum(vols[start:lo_i])
            lo_i = start

    return {
        "poc": round(lo + (poc_i + 0.5) * row, 2),
        "vah": round(lo + (hi_i + 1) * row, 2),
        "val": round(lo + lo_i * row, 2),
    }


def vp_context(candles):
    """
    Build the profile of the PREVIOUS session and classify where that
    session closed relative to its value area: "above", "below", "inside"
    or "edge" (within the buffer of VAH/VAL - ambiguous, so no trade).
    Returns None if there is not enough data.
    """
    if not candles or len(candles) < 20:
        return None

    by_day = {}
    for c in candles:
        by_day.setdefault(_session_date(c), []).append(c)

    today = _session_date(candles[-1])
    today_c = by_day.get(today, [])
    prev_days = sorted(
        d for d in by_day
        if d < today and len(by_day[d]) >= VP_MIN_SESSION_BARS
    )

    if not prev_days or len(today_c) < 8:
        return None

    prev_c = by_day[prev_days[-1]]
    profile = vp_session_profile(prev_c)
    if not profile:
        return None

    prev_close = prev_c[-1]["close"]

    if prev_close > profile["vah"] + VP_OUTSIDE_BUFFER:
        position = "above"
    elif prev_close < profile["val"] - VP_OUTSIDE_BUFFER:
        position = "below"
    elif profile["val"] <= prev_close <= profile["vah"]:
        position = "inside"
    else:
        position = "edge"

    print(
        f"[VP] prev session {prev_days[-1]} ({len(prev_c)} bars): "
        f"POC {profile['poc']:.2f} VAH {profile['vah']:.2f} "
        f"VAL {profile['val']:.2f} | close {prev_close:.2f} -> {position}"
    )

    return {
        "profile": profile,
        "prev_close": prev_close,
        "position": position,
        "today": today_c,
        "day": today.isoformat(),
    }


def vp_already_fired(state, strategy, direction):
    last = state.get("vp_fired", {}).get(f"{strategy}|{direction}")
    return last is not None and (time.time() - last) < VP_REFIRE_SECONDS


def mark_vp_fired(state, strategy, direction):
    fired = state.setdefault("vp_fired", {})
    fired[f"{strategy}|{direction}"] = time.time()
    state["vp_fired"] = {
        k: v for k, v in fired.items()
        if time.time() - v < 2 * VP_REFIRE_SECONDS
    }


def vp_set_plan(strategy, direction, entry, sl_ref):
    """
    Video rule: stop loss just beyond the setup's structure level, take
    profit at 2R. SL is floored at the bot's minimum (spread/noise) and the
    setup is skipped if the structure SL is wider than the bot's maximum.
    No breakeven / partials: single target, like the video.
    """
    raw_sl = (entry - sl_ref) if direction == "long" else (sl_ref - entry)

    if raw_sl > VP_MAX_SL:
        print(f"[VP] {strategy} {direction} skipped: structure SL "
              f"{raw_sl:.2f} pts > max {VP_MAX_SL}")
        return False

    sl_pts = round(max(raw_sl, MIN_SL_POINTS), 2)
    tp_pts = round(sl_pts * VP_RR, 2)

    VP_PLAN[strategy] = (sl_pts, tp_pts, tp_pts)
    return True


def _bull_confirm(prev, cur, level):
    engulf = (
        prev["close"] < prev["open"]
        and cur["close"] > cur["open"]
        and cur["close"] >= prev["open"]
        and cur["open"] <= prev["close"]
    )
    if engulf:
        return "bullish engulfing"
    if is_rejection(cur, level, "long"):
        return "bullish rejection"
    return None


def _bear_confirm(prev, cur, level):
    engulf = (
        prev["close"] > prev["open"]
        and cur["close"] < cur["open"]
        and cur["close"] <= prev["open"]
        and cur["open"] >= prev["close"]
    )
    if engulf:
        return "bearish engulfing"
    if is_rejection(cur, level, "short"):
        return "bearish rejection"
    return None


def check_vp_poc_bounce(candles, ctx):
    """
    Strategy #1 - POC Bounce (video).
    1. Previous session ended OUTSIDE the value area (inside = invalid).
    2. Price moves to the previous session's POC.
    3. Confirmation at the POC (bullish/bearish engulfing or rejection).
    Price returns toward the POC from the side the session closed on, so:
      prev closed ABOVE VAH -> price falls to POC, bullish confirm -> BUY
      prev closed BELOW VAL -> price rallies to POC, bearish confirm -> SELL
    SL slightly beyond the POC, TP 2R.
    """
    if not ctx or ctx["position"] not in ("above", "below"):
        return None, None

    today = ctx["today"]
    if len(today) < 8:
        return None, None

    prof = ctx["profile"]
    poc, vah, val = prof["poc"], prof["vah"], prof["val"]
    c, p = today[-1], today[-2]
    approach = today[-7:-1]

    if not (c["low"] <= poc + VP_TOUCH_TOL and c["high"] >= poc - VP_TOUCH_TOL):
        return None, None

    entry = c["close"]
    avg_close = sum(x["close"] for x in approach) / len(approach)

    if ctx["position"] == "above":
        if not (avg_close > poc and entry > poc):
            return None, None
        conf = _bull_confirm(p, c, poc)
        if not conf:
            return None, None
        if not vp_set_plan("VP POC Bounce", "long", entry, poc - VP_SL_BUFFER):
            return None, None
        direction = "long"
    else:
        if not (avg_close < poc and entry < poc):
            return None, None
        conf = _bear_confirm(p, c, poc)
        if not conf:
            return None, None
        if not vp_set_plan("VP POC Bounce", "short", entry, poc + VP_SL_BUFFER):
            return None, None
        direction = "short"

    return direction, (
        f"POC Bounce {conf} @ POC {poc:.2f} "
        f"(VAH {vah:.2f}, VAL {val:.2f}, prev close {ctx['prev_close']:.2f} "
        f"{ctx['position']} VA)"
    )


def _excursion_extreme(today, edge, side):
    """Lowest low (side='below') / highest high (side='above') of the run of
    consecutive closes outside the value area that just ended, plus the
    re-entry candle itself."""
    i = len(today) - 2
    ext = today[-1]["low"] if side == "below" else today[-1]["high"]
    while i >= 0:
        cl = today[i]["close"]
        outside = cl < edge if side == "below" else cl > edge
        if not outside:
            break
        ext = min(ext, today[i]["low"]) if side == "below" else max(ext, today[i]["high"])
        i -= 1
    return ext


def check_vp_reversal(candles, ctx):
    """
    Strategy #2 - Value Area Reversal (video).
    1. Previous session ended INSIDE the value area.
    2. Price crosses outside the value area of the current session.
    3. A candle CLOSES back inside the value area (a wick back in is not
       enough) -> trade back into the area. Can repeat all session, both sides.
    BUY after a close back above VAL, SELL after a close back below VAH.
    SL at the swing low/high of the excursion, TP 2R.
    """
    if not ctx or ctx["position"] != "inside":
        return None, None

    today = ctx["today"]
    if len(today) < 8:
        return None, None

    prof = ctx["profile"]
    poc, vah, val = prof["poc"], prof["vah"], prof["val"]
    c, p = today[-1], today[-2]
    entry = c["close"]

    if p["close"] < val and val <= entry <= vah:
        swing_low = _excursion_extreme(today, val, "below")
        if not vp_set_plan("VP Reversal", "long", entry, swing_low - VP_SL_BUFFER):
            return None, None
        return "long", (
            f"VA Reversal: closed back above VAL {val:.2f} "
            f"(POC {poc:.2f}, VAH {vah:.2f}, swing low {swing_low:.2f})"
        )

    if p["close"] > vah and val <= entry <= vah:
        swing_high = _excursion_extreme(today, vah, "above")
        if not vp_set_plan("VP Reversal", "short", entry, swing_high + VP_SL_BUFFER):
            return None, None
        return "short", (
            f"VA Reversal: closed back below VAH {vah:.2f} "
            f"(POC {poc:.2f}, VAL {val:.2f}, swing high {swing_high:.2f})"
        )

    return None, None


def _vp_breakout_setup(window, level, direction):
    """
    Breakout structure on closed M5 candles (window[-1] = latest candle):
      1. a close significantly beyond `level` (VAH for long, VAL for short)
         with no later close falling back through it,
      2. a pullback that comes back toward the level and holds
         (does not have to be perfect, just not too deep into the VA),
      3. the latest candle breaks structure (closes beyond the swing
         extreme made before the pullback) - higher high / lower low.
    Returns the broken swing level or None.
    """
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
    """
    Strategy #3 - Breakout (video). Works whether the previous session ended
    inside or outside the value area.
    1. Price significantly breaks outside the value area in the current session.
    2. Price pulls back toward the VA edge and holds.
    3. Break of structure in the breakout direction.
    SL slightly beyond the broken structure level, TP 2R.
    """
    if not ctx:
        return None, None

    today = ctx["today"]
    if len(today) < 10:
        return None, None

    prof = ctx["profile"]
    poc, vah, val = prof["poc"], prof["vah"], prof["val"]
    window = today[-VP_BREAKOUT_LOOKBACK:]
    entry = window[-1]["close"]

    swing = _vp_breakout_setup(window, vah, "long")
    if swing is not None:
        if vp_set_plan("VP Breakout", "long", entry, swing - VP_SL_BUFFER):
            return "long", (
                f"VA Breakout: held VAH {vah:.2f} after pullback, "
                f"BOS above {swing:.2f} (POC {poc:.2f}, VAL {val:.2f})"
            )

    swing = _vp_breakout_setup(window, val, "short")
    if swing is not None:
        if vp_set_plan("VP Breakout", "short", entry, swing + VP_SL_BUFFER):
            return "short", (
                f"VA Breakout: rejected at VAL {val:.2f} after pullback, "
                f"BOS below {swing:.2f} (POC {poc:.2f}, VAH {vah:.2f})"
            )

    return None, None


def _sma_series(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1:i + 1]) / period
    return out


def check_sma13_ema18(candles, live_price=None):
    """
    SMA13 + EMA18 smoothing strategy from the attached chart.

    BUY:
      - previous closed candle is at/below SMA13 and latest closed candle
        closes above SMA13;
      - SMA13 is rising and its EMA18 smoothing is rising;
      - SMA13 is above the smoothed EMA18 by at least SMA13_MIN_GAP;
      - the SMA13/smoothed-EMA pair has NOT crossed recently (chop filter).

    SELL is the exact opposite.

    The last/live candle is never used. The red-marked type of sideways
    area is rejected by the minimum-gap, slope and recent-line-cross filters.
    """
    need = SMA13_PERIOD + SMA13_SMOOTH_PERIOD + SMA13_SLOPE_LOOKBACK + SMA13_CHOP_LOOKBACK + 5
    if len(candles) < need:
        return None, None

    closes = [c["close"] for c in candles]
    sma = _sma_series(closes, SMA13_PERIOD)
    valid = [x for x in sma if x is not None]
    if len(valid) < SMA13_SMOOTH_PERIOD + SMA13_SLOPE_LOOKBACK + 2:
        return None, None

    smooth_valid = ema(valid, SMA13_SMOOTH_PERIOD)
    smooth = [None] * len(sma)
    sma_first = SMA13_PERIOD - 1
    for i, x in enumerate(sma):
        if x is not None:
            smooth[i] = smooth_valid[i - sma_first]

    last = len(closes) - 1
    prev = last - 1
    ref = last - SMA13_SLOPE_LOOKBACK
    if any(x is None for x in (sma[prev], sma[last], smooth[last], smooth[ref])):
        return None, None

    sma_slope = (sma[last] - sma[ref]) / SMA13_SLOPE_LOOKBACK
    smooth_slope = (smooth[last] - smooth[ref]) / SMA13_SLOPE_LOOKBACK
    gap = sma[last] - smooth[last]

    # Choppy/entangled MA filter: the two lines must not have crossed in the
    # recent bars immediately before the trigger.
    recent = []
    start = max(SMA13_PERIOD - 1, last - SMA13_CHOP_LOOKBACK - 1)
    for i in range(start, last + 1):
        if sma[i] is not None and smooth[i] is not None:
            recent.append(sma[i] - smooth[i])
    recent_cross = any(recent[i - 1] * recent[i] <= 0 for i in range(1, len(recent)))
    if recent_cross:
        return None, None

    prev_close = closes[prev]
    now_close = closes[last]

    # DATA-INTEGRITY GUARD — SMA13/EMA18 only.
    # The technical setup is calculated from the latest completed M5 candle,
    # but the trade must not be alerted when that candle is stale/far away
    # from the current live XAUUSD price. This prevents errors such as a
    # 4203.60 signal while the live market is around 4170.
    if live_price is None:
        print("[SMA13/EMA18] blocked: live XAUUSD price unavailable")
        return None, None
    live_gap = abs(float(live_price) - now_close)
    if live_gap > SMA13_MAX_LIVE_GAP:
        print(
            f"[SMA13/EMA18] blocked: signal close {now_close:.2f} vs "
            f"live {float(live_price):.2f} (gap {live_gap:.2f} > "
            f"{SMA13_MAX_LIVE_GAP:.2f})"
        )
        return None, None

    if (
        prev_close <= sma[prev]
        and now_close > sma[last]
        and gap >= SMA13_MIN_GAP
        and sma_slope >= SMA13_MIN_SLOPE
        and smooth_slope >= SMA13_MIN_SLOPE * 0.50
    ):
        return "long", (
            f"SMA13/EMA18 Trend Cross Up @ {now_close:.2f} "
            f"(SMA13 {sma[last]:.2f}, EMA18 {smooth[last]:.2f}, "
            f"gap {gap:+.2f}, slopes {sma_slope:+.2f}/{smooth_slope:+.2f})"
        )

    if (
        prev_close >= sma[prev]
        and now_close < sma[last]
        and gap <= -SMA13_MIN_GAP
        and sma_slope <= -SMA13_MIN_SLOPE
        and smooth_slope <= -SMA13_MIN_SLOPE * 0.50
    ):
        return "short", (
            f"SMA13/EMA18 Trend Cross Down @ {now_close:.2f} "
            f"(SMA13 {sma[last]:.2f}, EMA18 {smooth[last]:.2f}, "
            f"gap {gap:+.2f}, slopes {sma_slope:+.2f}/{smooth_slope:+.2f})"
        )

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
    timeframe="5m",
):
    arrow = "🟢 BUY" if direction == "long" else "🔴 SELL"

    return (
        f"<b>{arrow} XAUUSD ({timeframe})</b>\n"
        f"Strategy: <b>{strategy}</b>\n"
        f"Entry: <b>{entry:.2f}</b> | {info}\n"
        f"1H Trend: <b>{bias_1h.upper()}</b> | Session: {session}\n"
        f"─────────────────────\n"
        f"🛑 SL: <b>{sl:.2f}</b> (-{sl_pts} pts) [Max 10.0]\n"
        f"🎯 TP1: <b>{tp1:.2f}</b> (+{tp1_pts} pts)\n"
        f"🏁 TP2: <b>{tp2:.2f}</b> (+{tp2_pts} pts)\n"
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
        "SMA6", "MA4/45 Pullback",
        "MA3/25 Cross", "SMA18 Touch", "SMA13/EMA18", "HalfTrend", "MSB-OB",
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

def global_signal_blocked(state, candle_open_time):
    now = time.time()
    last_time = state.get("global_last_signal_time", 0)
    last_candle = state.get("global_last_signal_candle")

    if last_candle == candle_open_time:
        return True
    if last_time and now - last_time < GLOBAL_SIGNAL_COOLDOWN_SECONDS:
        return True
    return False


def mark_global_signal(state, candle_open_time):
    # Persist BEFORE sending Telegram/opening the trade so retries do not duplicate.
    state["global_last_signal_time"] = time.time()
    state["global_last_signal_candle"] = candle_open_time
    save_state(state)



# ================= MAIN =================
# ================= VOLUME-TREND ORDER BLOCK ENGINE (BigBeluga) =================
def _wma(values, period):
    out = [None] * len(values)
    if period <= 0:
        return out
    denom = period * (period + 1) / 2.0
    for i in range(period - 1, len(values)):
        total = 0.0
        for j in range(period):
            total += values[i - period + 1 + j] * (j + 1)
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


def dynamic_grid_compute(candles):
    """
    Dynamic Grid [BigBeluga], rule taken from the video transcript:

    Baseline = HMA 150 (green when rising, red when falling). Bands = baseline
    +/- k * (Wilder ATR 100 * 1.6). A signal needs the PREVIOUS candle to CLOSE
    beyond band k, and the CURRENT candle to reverse colour (green candle after a
    close below a lower band = BUY, red candle after a close above an upper band
    = SELL). The degree k is the deepest band the previous close was beyond.

    Trend-aligned signals (BUY with rising baseline, SELL with falling) are the
    video's "same direction as the trend" setup. Counter-trend signals are the
    mean-reversion setup and need degree >= DG_MIN_REVERSION_LEVEL.
    """
    closes = [c["close"] for c in candles]
    hma = _hma(closes, DG_HMA_LENGTH)
    atr_raw = _wilder_atr(candles, DG_ATR_LENGTH)
    step = [None if a is None else a * DG_ATR_MULT for a in atr_raw]
    n = len(candles)

    raw = [None] * n
    level = [None] * n
    kind = [None] * n
    last_signal = -10**9

    for i in range(1, n):
        if hma[i] is None or hma[i - 1] is None or step[i] is None or step[i - 1] is None:
            continue
        if step[i - 1] <= 0:
            continue

        rising = hma[i] > hma[i - 1]
        falling = hma[i] < hma[i - 1]
        prev_close = closes[i - 1]
        dist = (prev_close - hma[i - 1]) / step[i - 1]   # in grid steps
        k_low = min(DG_NUM_LEVELS, int(-dist)) if dist <= -1 else 0
        k_up = min(DG_NUM_LEVELS, int(dist)) if dist >= 1 else 0
        green = candles[i]["close"] > candles[i]["open"]
        red = candles[i]["close"] < candles[i]["open"]

        direction = None
        lvl = None
        sig_kind = None
        if k_low >= 1 and green:
            aligned = rising
            direction, lvl = "long", k_low
        elif k_up >= 1 and red:
            aligned = falling
            direction, lvl = "short", k_up
        if direction is not None:
            if aligned and DG_MODE in ("trend", "both"):
                sig_kind = "trend"
            elif (not aligned) and DG_MODE in ("reversion", "both") \
                    and lvl >= DG_MIN_REVERSION_LEVEL:
                sig_kind = "reversion"
            else:
                direction = None

        if direction is not None and i - last_signal >= DG_MIN_GAP_BARS:
            raw[i] = direction
            level[i] = lvl
            kind[i] = sig_kind
            last_signal = i

    return {"hma": hma, "atr": atr_raw, "step": step, "signal": raw,
            "level": level, "kind": kind}


def obe_compute(candles, st_length=None, mult=None, pivot=None, delete_on_break=None,
                min_buy=None, min_sell=None):
    """
    Volume-Trend Order Block Engine + the two entry paths taught in the video.

    Core OB construction is kept from the existing implementation:
      - volatility SMA(high-low), Supertrend-style trend
      - pivot strength = OBE_PIVOT (kept at 5)
      - multiplier = OBE_ST_MULT (kept at 2.5)
      - delete-on-break remains enabled
      - minimum Buy/Sell volume = 50%

    Entry path A — native OB retest:
      trend + active OB -> price retests the OB -> price exits/bounces from
      the OB -> required volume percentage -> signal.

    Entry path B — video MACD fallback:
      trend + active OB -> pullback comes NEAR the OB but does not touch it ->
      MACD histogram (13/34) shows the pullback color -> histogram crosses
      back through zero into the trend color -> signal.

    The video does not give a numerical definition for "near", so the code
    uses a bounded tolerance derived from the active OB depth rather than
    inventing a fixed multi-point distance.
    """
    L = OBE_ST_LENGTH if st_length is None else st_length
    M = OBE_ST_MULT if mult is None else mult
    P = OBE_PIVOT if pivot is None else pivot
    dob = OBE_DELETE_ON_BREAK if delete_on_break is None else delete_on_break
    mb = (OBE_MIN_BUY_PCT if min_buy is None else min_buy) / 100.0
    ms = (OBE_MIN_SELL_PCT if min_sell is None else min_sell) / 100.0

    n = len(candles)
    o = [c["open"] for c in candles]
    h = [c["high"] for c in candles]
    lo = [c["low"] for c in candles]
    cl = [c["close"] for c in candles]
    vols = [max(float(c.get("volume") or 0.0), 0.0) for c in candles]
    if sum(vols) <= 0:
        vols = [1.0] * n

    # ---- volatility SMA and custom Supertrend ----
    rng = [h[i] - lo[i] for i in range(n)]
    atr = [None] * n
    run = 0.0
    for i in range(n):
        run += rng[i]
        if i >= L:
            run -= rng[i - L]
        if i >= L - 1:
            atr[i] = run / L

    lower = [None] * n
    upper = [None] * n
    trend = [1] * n
    for i in range(n):
        if atr[i] is None:
            continue
        src = (h[i] + lo[i]) / 2.0
        up_b = src + M * atr[i]
        dn_b = src - M * atr[i]
        if i == 0 or lower[i - 1] is None:
            lower[i], upper[i] = dn_b, up_b
            trend[i] = 1
            continue
        lower[i] = dn_b if (dn_b > lower[i - 1] or cl[i - 1] < lower[i - 1]) else lower[i - 1]
        upper[i] = up_b if (up_b < upper[i - 1] or cl[i - 1] > upper[i - 1]) else upper[i - 1]
        if trend[i - 1] == -1:
            trend[i] = 1 if cl[i] > upper[i] else -1
        else:
            trend[i] = -1 if cl[i] < lower[i] else 1

    # ---- MACD histogram 13/34 (video fallback) ----
    fast = ema(cl, OBE_MACD_FAST)
    slow = ema(cl, OBE_MACD_SLOW)
    hist = [None] * n
    for i in range(n):
        if fast[i] is not None and slow[i] is not None:
            hist[i] = fast[i] - slow[i]

    # ---- order block state machine ----
    act = None                    # {dir, top, bot, created_i, buy_pct}
    top_hist = [None] * n
    bot_hist = [None] * n
    signals = []                  # native OB retest signals
    confirms = []                 # retained for diagnostics/backward compatibility
    macd_signals = []             # video fallback signals
    created = []
    fallback_armed = False

    for t in range(n):
        if atr[t] is None or t < 2 * P:
            continue

        win_lo = min(lo[t - 2 * P:t + 1])
        win_hi = max(h[t - 2 * P:t + 1])
        piv_l = lo[t - P] == win_lo
        piv_h = h[t - P] == win_hi

        wv = vols[t - P:t + 1]
        wtot = sum(wv)
        wbuy = sum(vols[j] for j in range(t - P, t + 1) if cl[j] >= o[j])
        win_buy_pct = (wbuy / wtot) if wtot > 0 else 0.5

        bull_ref = min(o[t - P], cl[t - P])
        bear_ref = max(o[t - P], cl[t - P])
        a = atr[t]

        def free_of_overlap(new_bot, new_top):
            if act is None:
                return True
            return new_bot > act["top"] or new_top < act["bot"]

        # New active order block.
        if trend[t] == 1 and piv_l and free_of_overlap(bull_ref - a, bull_ref):
            top_, bot_ = bull_ref, bull_ref - a
            if dob and h[t] < bot_:
                act = None
                fallback_armed = False
            else:
                act = {"dir": 1, "top": top_, "bot": bot_,
                       "created_i": t, "buy_pct": win_buy_pct}
                fallback_armed = False
                created.append((t, 1, win_buy_pct))
                # Diagnostic only: creation is NOT an entry anymore.
                if win_buy_pct >= mb:
                    confirms.append((t, "long", win_buy_pct, bot_, top_, trend[t]))

        elif trend[t] == -1 and piv_h and free_of_overlap(bear_ref, bear_ref + a):
            top_, bot_ = bear_ref + a, bear_ref
            if dob and lo[t] > top_:
                act = None
                fallback_armed = False
            else:
                act = {"dir": -1, "top": top_, "bot": bot_,
                       "created_i": t, "buy_pct": win_buy_pct}
                fallback_armed = False
                created.append((t, -1, win_buy_pct))
                if (1.0 - win_buy_pct) >= ms:
                    confirms.append((t, "short", win_buy_pct, bot_, top_, trend[t]))

        elif dob and act is not None:
            if act["dir"] == 1 and h[t] < act["bot"]:
                act = None
                fallback_armed = False
            elif act["dir"] == -1 and lo[t] > act["top"]:
                act = None
                fallback_armed = False

        top_hist[t] = act["top"] if act else None
        bot_hist[t] = act["bot"] if act else None

        if act is None or t <= act["created_i"]:
            continue

        market_change = t > 0 and trend[t] != trend[t - 1]
        if market_change:
            # The indicator's own trend has changed; do not carry an old OB
            # setup into the opposite trend.
            fallback_armed = False
            continue

        # ---- PATH A: native OB retest / bounce ----
        buy_pct = act["buy_pct"]
        if (act["dir"] == 1 and buy_pct >= mb
                and not piv_l
                and top_hist[t - 1] is not None
                and lo[t] > act["top"]
                and lo[t - 1] <= top_hist[t - 1]):
            signals.append((t, "long", buy_pct, act["bot"], act["top"], trend[t]))
            # A successful OB retest consumes the fallback setup.
            fallback_armed = False
            continue

        if (act["dir"] == -1 and (1.0 - buy_pct) >= ms
                and not piv_h
                and bot_hist[t - 1] is not None
                and h[t] < act["bot"]
                and h[t - 1] >= bot_hist[t - 1]):
            signals.append((t, "short", buy_pct, act["bot"], act["top"], trend[t]))
            fallback_armed = False
            continue

        # ---- PATH B: MACD fallback when price comes near but DOES NOT touch OB ----
        if hist[t] is None or hist[t - 1] is None:
            continue

        ob_depth = max(act["top"] - act["bot"], 0.0)
        near_tol = min(
            OBE_MACD_NEAR_MAX,
            max(OBE_MACD_NEAR_MIN, ob_depth * OBE_MACD_NEAR_FRACTION),
        )

        if act["dir"] == 1:
            # Bullish setup: pullback comes close above the OB but does not
            # actually touch its top. MACD histogram must first be red.
            near_ob = lo[t] > act["top"] and lo[t] <= act["top"] + near_tol
            if near_ob and hist[t] < 0:
                fallback_armed = True

            # Pullback ended: histogram crosses from <=0 to >0 (green).
            if fallback_armed and hist[t - 1] <= 0 < hist[t]:
                # Never allow fallback to fire after the candle has touched
                # the OB; a real retest belongs to Path A.
                if lo[t] > act["top"]:
                    macd_signals.append((t, "long", act["buy_pct"],
                                         act["bot"], act["top"], trend[t], near_tol))
                fallback_armed = False

        else:
            # Bearish setup: pullback comes close below the OB but does not
            # actually touch its bottom. MACD histogram must first be green.
            near_ob = h[t] < act["bot"] and h[t] >= act["bot"] - near_tol
            if near_ob and hist[t] > 0:
                fallback_armed = True

            # Pullback ended: histogram crosses from >=0 to <0 (red).
            if fallback_armed and hist[t - 1] >= 0 > hist[t]:
                if h[t] < act["bot"]:
                    macd_signals.append((t, "short", act["buy_pct"],
                                         act["bot"], act["top"], trend[t], near_tol))
                fallback_armed = False

    return {
        "signals": signals,
        "confirms": confirms,
        "macd_signals": macd_signals,
        "trend": trend,
        "atr": atr,
        "hist": hist,
        "created": created,
    }


def _obe_now_ms():
    if OBE_CLOCK is not None:
        return int(OBE_CLOCK())
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def check_dynamic_grid(candles):
    """Standalone Dynamic Grid strategy on completed M5 candles.

    BUY: previous candle closed below a lower ATR band and the latest completed
         candle is green (video rule). SELL is the mirror with a red candle.
    Trend-aligned signals and high-degree counter-trend signals, see DG_MODE.

    This function is completely independent of the Order Block Engine.
    """
    if not DG_ENABLED:
        return None, None
    need = max(DG_HMA_LENGTH + 20, DG_ATR_LENGTH + 20)
    if len(candles) < need:
        return None, None

    grid = dynamic_grid_compute(candles)
    i = len(candles) - 1
    direction = grid["signal"][i]
    level = grid["level"][i]
    if direction is None:
        return None, None

    close = candles[i]["close"]
    h = grid["hma"][i]
    atr = grid["atr"][i]
    step = grid["step"][i]
    if h is None or atr is None or step is None:
        return None, None

    side = "BUY" if direction == "long" else "SELL"
    rail = h - step * level if direction == "long" else h + step * level
    kind = (grid.get("kind") or [None] * len(candles))[i] or "trend"
    info = (
        f"Dynamic Grid {side} ({kind}) | Degree {level} | HMA {DG_HMA_LENGTH} "
        f"| ATR {DG_ATR_LENGTH} x {DG_ATR_MULT:.1f} | "
        f"band {rail:.2f} | baseline {h:.2f} | close {close:.2f}"
    )
    return direction, info


def check_ob_engine(candles, tf, state):
    """
    Video-correct OB Engine entry logic.

    Path A: actual OB retest + minimum volume percentage.
    Path B: near-OB pullback + MACD 13/34 histogram reversal through zero.

    Pivot remains 5 and OB multiplier remains 2.5. The bot uses completed
    M5 candles, fixed 7-point SL / 6-point TP, one target, no breakeven.
    """
    name = "OB Engine " + tf
    need = OBE_ST_LENGTH + 2 * OBE_PIVOT + 10
    if not candles or len(candles) < need:
        return None, None

    seen = state.setdefault("obe_last_bar", {})
    last_seen = seen.get(tf)
    last_bar = candles[-1]["open_time"]

    now_ms = _obe_now_ms()
    feed_lag_min = (now_ms - last_bar) / 60000.0
    if feed_lag_min > OBE_MAX_FEED_LAG_MIN[tf]:
        print(f"[OBE] {name} skipped: {tf} feed is stale - newest candle opened "
              f"{feed_lag_min:.1f} min ago (limit {OBE_MAX_FEED_LAG_MIN[tf]:.0f})")
        return None, None

    res = obe_compute(candles)
    n = len(candles)
    max_age = OBE_MAX_AGE_BARS[tf]

    candidates = []
    for s in res["signals"]:
        candidates.append((s[0], s[1], s[2], s[3], s[4], "OB Retest", None))
    for s in res["macd_signals"]:
        candidates.append((s[0], s[1], s[2], s[3], s[4], "MACD Fallback", s[6]))

    fresh = [
        s for s in candidates
        if (n - 1 - s[0]) < max_age
        and (last_seen is None or candles[s[0]]["open_time"] > last_seen)
    ]
    if not fresh:
        # Mark the newest bar only after evaluation. This prevents the current
        # completed candle from being re-evaluated on every polling cycle.
        seen[tf] = last_bar
        return None, None

    # OB Engine is standalone. Dynamic Grid is evaluated separately below
    # and must never approve/reject an OB Engine signal.
    (i, direction, bpct, ob_bot, ob_top, kind, near_tol) = fresh[-1]
    bar_ms = OBE_BAR_MS[tf]
    sig_age_min = (now_ms - (candles[i]["open_time"] + bar_ms)) / 60000.0
    if sig_age_min > OBE_MAX_SIGNAL_AGE_MIN[tf]:
        print(f"[OBE] {name} {direction} skipped: signal candle closed "
              f"{sig_age_min:.1f} min ago (limit {OBE_MAX_SIGNAL_AGE_MIN[tf]:.0f})")
        seen[tf] = last_bar
        return None, None

    entry = candles[i]["close"]
    if direction == "long":
        sl_p, tp_p = entry - OBE_SL_POINTS, entry + OBE_TP_POINTS
    else:
        sl_p, tp_p = entry + OBE_SL_POINTS, entry - OBE_TP_POINTS

    # Do not alert on a setup that has already hit its fixed exit before the
    # polling cycle caught it.
    for c in candles[i + 1:]:
        hit_sl = c["low"] <= sl_p if direction == "long" else c["high"] >= sl_p
        hit_tp = c["high"] >= tp_p if direction == "long" else c["low"] <= tp_p
        if hit_sl or hit_tp:
            print(f"[OBE] {name} {direction} skipped: signal candle already "
                  f"resolved ({'SL' if hit_sl else 'TP'} touched since)")
            seen[tf] = last_bar
            return None, None

    sig_open = candles[i]["open_time"]
    OBE_PLAN[name] = {
        "entry": entry,
        "sl_price": sl_p,
        "tp_price": tp_p,
        "scan_from_ms": sig_open + bar_ms,
        "timeframe": tf,
    }

    age_min = max(sig_age_min, 0.0)
    sig_time = datetime.fromtimestamp(sig_open / 1000, tz=timezone.utc).strftime("%H:%M")
    if direction == "long":
        side = f"Buy {bpct * 100:.0f}% (min {OBE_MIN_BUY_PCT:.0f}%)"
    else:
        side = f"Sell {(1 - bpct) * 100:.0f}% (min {OBE_MIN_SELL_PCT:.0f}%)"

    if kind == "MACD Fallback":
        info = (
            f"MACD Fallback BUY/SELL {tf} | {side} | MACD {OBE_MACD_FAST}/{OBE_MACD_SLOW} "
            f"zero-line reversal | OB {ob_bot:.2f}-{ob_top:.2f} | "
            f"near <= {near_tol:.2f} | candle {sig_time} UTC (closed {age_min:.0f}m ago)"
        )
    else:
        info = (
            f"OB Retest {'BUY' if direction == 'long' else 'SELL'} {tf} | {side} | "
            f"OB {ob_bot:.2f}-{ob_top:.2f} | candle {sig_time} UTC "
            f"(closed {age_min:.0f}m ago)"
        )

    seen[tf] = last_bar
    return direction, info

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

    OBE_FEED_SOURCE["5m"] = data_source

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

    resolve_obe_trades(candles, OBE_BAR_MS["5m"])
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

    # NEW: SMA6 strategy
    d, info = run_strategy("SMA6", check_sma6, candles)
    if d:
        raw.append(("SMA6", d, info or ""))

    # NEW: MA4/45 trend-pullback strategy
    d, info = run_strategy("MA4/45 Pullback", check_ma_pullback, candles)
    if d:
        raw.append(("MA4/45 Pullback", d, info or ""))

    # NEW: MA3/25 touch-cross strategy
    # Fully closed-candle based (fast and slow MA both come from the same
    # candle series), so it no longer depends on which data feed supplied
    # the candles and runs on any source, like SMA18 Touch.
    d, info = run_strategy(
        "MA3/25 Cross", check_ma2_148_cross, candles, price,
        current_atr if current_atr else fallback_atr,
    )
    if d:
        raw.append(("MA3/25 Cross", d, info or ""))

    # NEW: SMA18 touch strategy — closed-candle based, same slope-filter
    # approach as MA3/25, goes through the standard 1H bias + cooldown/
    # one-open-trade filters below (no exemption requested for this one).
    d, info = run_strategy("SMA18 Touch", check_sma18_touch, candles, price)
    if d:
        raw.append(("SMA18 Touch", d, info or ""))

    # NEW: Volume Profile strategies (previous-session POC/VAH/VAL, closed
    # M5 candles). 2R single target, same strategy+direction waits 1 hour.
    vp_ctx = None
    try:
        vp_ctx = vp_context(candles)
    except Exception as exc:
        print(f"[VP ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()

    if vp_ctx:
        for vp_name, vp_func in (
            ("VP POC Bounce", check_vp_poc_bounce),
            ("VP Reversal", check_vp_reversal),
            ("VP Breakout", check_vp_breakout),
        ):
            d, info = run_strategy(vp_name, vp_func, candles, vp_ctx)
            if d and vp_already_fired(state, vp_name, d):
                print(f"[FILTERED] {vp_name} {d}: already fired today")
                continue
            if d:
                raw.append((vp_name, d, info or ""))
    else:
        print("[VP] no usable previous-session profile this run")

    # SMA13 + EMA18 smoothed trend-cross strategy from the attached chart.
    d, info = run_strategy("SMA13/EMA18", check_sma13_ema18, candles, price)
    if d:
        raw.append(("SMA13/EMA18", d, info or ""))

    # MSB-OB: confirmed Market Structure Break using the screenshot settings.
    # No 1H/directional filter; fixed SL 10 / TP 10, no breakeven.
    d, info = run_strategy("MSB-OB", check_msb_ob, candles, state)
    if d:
        raw.append(("MSB-OB", d, info or ""))

    # HalfTrend Long/Short [BigBeluga]: closed-candle flip, fixed SL 8 / TP 8.
    d, info = run_strategy("HalfTrend", check_halftrend, candles, state)
    if d:
        raw.append(("HalfTrend", d, info or ""))

    # Dynamic Grid [BigBeluga]: standalone 5m strategy. It has no dependency
    # on the OB Engine and does not modify/filter OB Engine signals.
    d, info = run_strategy("Dynamic Grid", check_dynamic_grid, candles)
    if d:
        raw.append(("Dynamic Grid", d, info or ""))

    # Volume-Trend Order Block Engine [BigBeluga]: 5m only.
    d, info = run_strategy("OB Engine 5m", check_ob_engine, candles, "5m", state)
    if d:
        raw.append(("OB Engine 5m", d, info or ""))

    final = []

    for strategy, direction, info in raw:
        # MA3/25 now uses the normal 1H bias, cooldown, and one-open-trade
        # filters. This prevents rapid up/down re-signals from line noise.
        # Keep the existing 1H bias filter.
        skip_bias = (
            (strategy in OBE_STRATEGIES and not OBE_USE_1H_BIAS)
            or strategy in NO_BIAS_STRATEGIES
        )

        if not skip_bias and bias_1h == "bullish" and direction == "short":
            print(f"[FILTERED] {strategy} {direction}: blocked by 1H bullish bias")
            continue

        if not skip_bias and bias_1h == "bearish" and direction == "long":
            print(f"[FILTERED] {strategy} {direction}: blocked by 1H bearish bias")
            continue

        if not can_send(state, strategy, direction):
            reason = "already has an open trade" if has_open_trade(strategy) else "cooldown/dedup"
            print(f"[FILTERED] {strategy} {direction}: blocked by {reason} (can_send)")
            continue

        final.append((strategy, direction, info))

    closed_candle_time = candles[-1].get("open_time")
    for strategy, direction, info in final:
        obe_free = strategy in OBE_STRATEGIES and not OBE_USE_GLOBAL_LIMIT

        if not obe_free and global_signal_blocked(state, closed_candle_time):
            print(
                f"[GLOBAL FILTERED] {strategy} {direction}: "
                "blocked by one-signal-per-candle / 5-minute cooldown"
            )
            continue

        if strategy in VP_STRATEGIES and vp_ctx:
            # Persist the re-fire mark before sending (same idea as the
            # global mark) so a retry can never duplicate the alert.
            mark_vp_fired(state, strategy, direction)

        if obe_free:
            # OB Engine signals neither use nor consume the global limit; just
            # persist state (incl. the last-processed bar) before sending.
            save_state(state)
        else:
            mark_global_signal(state, closed_candle_time)

        if strategy in OBE_STRATEGIES:
            # Fixed SL = 7 / TP = 6 points, single target.
            sl_pts, tp1_pts, tp2_pts = OBE_SL_POINTS, OBE_TP_POINTS, OBE_TP_POINTS
        elif strategy in VP_STRATEGIES:
            # Volume Profile strategies keep their own rules (video): SL at
            # the setup's structure, TP 2R, computed by the check function.
            sl_pts, tp1_pts, tp2_pts = VP_PLAN[strategy]
        elif strategy in STRATEGY_SL_TP_OVERRIDE:
            # Per-strategy fixed SL / TP (single target, no breakeven).
            o_sl, o_tp = STRATEGY_SL_TP_OVERRIDE[strategy]
            sl_pts, tp1_pts, tp2_pts = o_sl, o_tp, o_tp
        elif strategy == "HalfTrend":
            # Fallback (normally covered by STRATEGY_SL_TP_OVERRIDE above).
            sl_pts, tp1_pts, tp2_pts = HT_SL_POINTS, HT_TP_POINTS, HT_TP_POINTS
        else:
            # Every other strategy: fixed SL 7 / TP 6, single target.
            sl_pts, tp1_pts, tp2_pts = FIXED_SL_POINTS, FIXED_TP_POINTS, FIXED_TP_POINTS

        # Closed-candle strategies must use the exact completed candle close
        # as their signal/entry price. Do not replace it with a later live
        # spot price, which creates the 1-2+ point variation seen in alerts.
        CLOSED_CANDLE_ENTRY_STRATEGIES = {
            "MA3/25 Cross", "Dynamic Grid", "HalfTrend", "MSB-OB", *VP_STRATEGIES,
        }
        signal_price = (
            candles[-1]["close"]
            if strategy in CLOSED_CANDLE_ENTRY_STRATEGIES
            else price
        )
        obe_plan = OBE_PLAN.get(strategy) if strategy in OBE_STRATEGIES else None
        if obe_plan:
            # Entry = close of the candle that produced the retest signal.
            signal_price = obe_plan["entry"]
            # Last line of defence against a late alert: if the LIVE price has
            # already gone through the stop or target, the trade is over.
            lp = price
            if direction == "long":
                dead = lp <= obe_plan["sl_price"] or lp >= obe_plan["tp_price"]
            else:
                dead = lp >= obe_plan["sl_price"] or lp <= obe_plan["tp_price"]
            if dead:
                print(f"[OBE] {strategy} {direction} skipped: live {lp:.2f} already "
                      f"beyond SL/TP of entry {signal_price:.2f}")
                continue
            info = (f"{info} | live {lp:.2f} ({lp - signal_price:+.2f} vs entry)"
                    f" | feed {OBE_FEED_SOURCE.get(obe_plan['timeframe'], '?')}")

        if direction == "long":
            sl = round(signal_price - sl_pts, 2)
            tp1 = round(signal_price + tp1_pts, 2)
            tp2 = round(signal_price + tp2_pts, 2)
        else:
            sl = round(signal_price + sl_pts, 2)
            tp1 = round(signal_price - tp1_pts, 2)
            tp2 = round(signal_price - tp2_pts, 2)

        msg = format_signal(
            direction,
            signal_price,
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
            timeframe=(obe_plan["timeframe"] if obe_plan else "5m"),
        )

        send_telegram(msg)
        open_trade(
            direction,
            signal_price,
            sl,
            tp1,
            tp2,
            strategy,
            info,
            breakeven_enabled=False,   # no breakeven for any strategy
            single_target=True,        # one target: close fully at TP or SL
            extra=(
                {"scan_from_ms": obe_plan["scan_from_ms"],
                 "timeframe": obe_plan["timeframe"]}
                if obe_plan else None
            ),
        )
        record(state, strategy, direction)

        print(
            f"[SIGNAL] {strategy} {direction.upper()} @ {signal_price:.2f} | "
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
