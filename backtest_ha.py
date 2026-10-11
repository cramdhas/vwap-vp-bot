"""
HA Reversal backtest (XAUUSD 5m, Heiken Ashi)
Run next to main.py:   python backtest_ha.py            (default 90 days)
                       python backtest_ha.py --days 60
                       python backtest_ha.py --csv data.csv   (columns: datetime,open,high,low,close, UTC)

Uses the SAME check_ha_reversal() as the live bot.
Rules: entry = real close of trigger candle, SL/TP from main.py (7 / 6),
one trade at a time, only when the market is open (is_octa_xauusd_open).
If one candle touches both SL and TP, it is counted as a LOSS (conservative).
Needs env TWELVEDATA_API_KEY (free plan is fine). Telegram report is sent if
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.
"""
import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

import main as bot

BAR_MS = 300_000
WINDOW = 300          # candles given to the strategy per step (HA seed effect is gone well before this)
MIN_BARS = 60


# ---------------- data ----------------
def _parse_values(values):
    out = []
    for v in values:
        try:
            dt = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            out.append({
                "open_time": int(dt.timestamp() * 1000),
                "open": float(v["open"]), "high": float(v["high"]),
                "low": float(v["low"]), "close": float(v["close"]), "volume": 0.0,
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_twelvedata(days):
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        sys.exit("Set TWELVEDATA_API_KEY first.")
    start = datetime.now(timezone.utc) - timedelta(days=days)
    end_date = None
    got = {}
    for page in range(12):
        params = {"symbol": "XAU/USD", "interval": "5min", "outputsize": 5000,
                  "apikey": key, "timezone": "UTC", "order": "desc"}
        if end_date:
            params["end_date"] = end_date
        r = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=40)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "error":
            if got:
                print(f"[data] stopped: {data.get('message')}")
                break
            sys.exit(f"TwelveData error: {data.get('message')}")
        chunk = _parse_values(data.get("values") or [])
        if not chunk:
            break
        for c in chunk:
            got[c["open_time"]] = c
        oldest = min(c["open_time"] for c in chunk)
        print(f"[data] page {page + 1}: {len(chunk)} candles, back to "
              f"{datetime.fromtimestamp(oldest / 1000, tz=timezone.utc):%Y-%m-%d %H:%M}")
        if datetime.fromtimestamp(oldest / 1000, tz=timezone.utc) <= start or len(chunk) < 100:
            break
        end_date = (datetime.fromtimestamp(oldest / 1000, tz=timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(8)      # free plan: 8 requests / minute
    candles = sorted(got.values(), key=lambda c: c["open_time"])
    cut = int(start.timestamp() * 1000) - WINDOW * BAR_MS   # extra warm-up bars before the test start
    return [c for c in candles if c["open_time"] >= cut], int(start.timestamp() * 1000)


def load_csv(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({"datetime": r["datetime"][:19].replace("T", " "), "open": r["open"],
                         "high": r["high"], "low": r["low"], "close": r["close"]})
    candles = sorted(_parse_values(rows), key=lambda c: c["open_time"])
    return candles, candles[0]["open_time"] if candles else 0


# ---------------- backtest ----------------
def run(candles, test_start_ms):
    bot.HA_ENABLED = True
    state = {}
    trades = []
    open_trade = None
    sl_pts, tp_pts = bot.HA_SL_POINTS, bot.HA_TP_POINTS

    for i in range(MIN_BARS, len(candles)):
        c = candles[i]
        # 1) manage open trade on this candle
        if open_trade:
            t = open_trade
            long_ = t["dir"] == "long"
            hit_sl = c["low"] <= t["sl"] if long_ else c["high"] >= t["sl"]
            hit_tp = c["high"] >= t["tp"] if long_ else c["low"] <= t["tp"]
            if hit_sl or hit_tp:
                t["result"] = "loss" if hit_sl else "win"      # both touched -> loss
                t["pnl"] = -sl_pts if hit_sl else tp_pts
                t["closed_ms"] = c["open_time"] + BAR_MS
                trades.append(t)
                open_trade = None
        if c["open_time"] < test_start_ms:
            continue
        # 2) look for a new signal at this candle's close
        close_dt = datetime.fromtimestamp((c["open_time"] + BAR_MS) / 1000, tz=timezone.utc)
        if open_trade or not bot.is_octa_xauusd_open(close_dt):
            continue
        window = candles[max(0, i - WINDOW + 1): i + 1]
        direction, info = bot.check_ha_reversal(window, state)
        if not direction:
            continue
        e = c["close"]
        open_trade = {
            "dir": direction, "entry": e, "opened_ms": c["open_time"] + BAR_MS,
            "sl": e - sl_pts if direction == "long" else e + sl_pts,
            "tp": e + tp_pts if direction == "long" else e - tp_pts,
        }
    return trades


# ---------------- report ----------------
def day_of(t):
    return datetime.fromtimestamp(t["closed_ms"] / 1000, tz=timezone.utc).date()


def agg(ts):
    w = sum(1 for t in ts if t["result"] == "win")
    l = len(ts) - w
    net = sum(t["pnl"] for t in ts)
    wr = w / len(ts) * 100 if ts else 0.0
    return w, l, net, wr


def build_reports(trades, days):
    sl_pts, tp_pts = bot.HA_SL_POINTS, bot.HA_TP_POINTS
    be = sl_pts / (sl_pts + tp_pts) * 100
    w, l, net, wr = agg(trades)
    longs = [t for t in trades if t["dir"] == "long"]
    shorts = [t for t in trades if t["dir"] == "short"]

    streak = best = 0
    eq = peak = dd = 0.0
    for t in trades:
        streak = streak + 1 if t["result"] == "loss" else 0
        best = max(best, streak)
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)

    by_day, by_week = defaultdict(list), defaultdict(list)
    for t in trades:
        d = day_of(t)
        by_day[d].append(t)
        iso = d.isocalendar()
        by_week[f"{iso[0]}-W{iso[1]:02d}"].append(t)

    days_with = len(by_day)
    summary = (
        f"HA Reversal backtest - last {days} days (5m, SL {sl_pts:g} / TP {tp_pts:g})\n"
        f"Trades: {len(trades)} | {w}W / {l}L | Win rate {wr:.1f}% (break-even {be:.1f}%)\n"
        f"Net: {net:+.0f} pts | Avg per trade: {net / len(trades) if trades else 0:+.2f} pts\n"
        f"BUY: {agg(longs)[0]}W/{agg(longs)[1]}L {agg(longs)[2]:+.0f} pts | "
        f"SELL: {agg(shorts)[0]}W/{agg(shorts)[1]}L {agg(shorts)[2]:+.0f} pts\n"
        f"Max losing streak: {best} | Max drawdown: {dd:.0f} pts | Trading days: {days_with}\n"
        f"(a candle touching both SL and TP counts as a loss; no spread/slippage included)"
    )
    weekly = "Weekly results\n" + "\n".join(
        f"{k}: {agg(v)[0]}W/{agg(v)[1]}L | {agg(v)[2]:+.0f} pts | WR {agg(v)[3]:.0f}%"
        for k, v in sorted(by_week.items()))
    daily_lines = [
        f"{k}: {agg(v)[0]}W/{agg(v)[1]}L | {agg(v)[2]:+.0f} pts"
        for k, v in sorted(by_day.items())
    ]
    return summary, weekly, daily_lines


def send_tg(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    for i in range(0, len(text), 3800):
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": text[i:i + 3800]}, timeout=15)
        except Exception as exc:
            print(f"[TG ERROR] {exc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--csv")
    a = ap.parse_args()

    candles, start_ms = load_csv(a.csv) if a.csv else fetch_twelvedata(a.days)
    if len(candles) < MIN_BARS + 50:
        sys.exit("Not enough candles.")
    print(f"[data] {len(candles)} candles loaded")

    trades = run(candles, start_ms)
    summary, weekly, daily_lines = build_reports(trades, a.days)

    with open("ha_backtest_trades.csv", "w", newline="") as f:
        wtr = csv.writer(f)
        wtr.writerow(["opened_utc", "closed_utc", "direction", "entry", "sl", "tp", "result", "pnl"])
        for t in trades:
            fm = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            wtr.writerow([fm(t["opened_ms"]), fm(t["closed_ms"]), t["dir"], f"{t['entry']:.2f}",
                          f"{t['sl']:.2f}", f"{t['tp']:.2f}", t["result"], t["pnl"]])

    print("\n" + summary + "\n\n" + weekly + "\n\nDaily results\n" + "\n".join(daily_lines))
    send_tg(summary)
    send_tg(weekly)
    send_tg("Daily results\n" + "\n".join(daily_lines))
    print("\nSaved ha_backtest_trades.csv")


if __name__ == "__main__":
    main()
