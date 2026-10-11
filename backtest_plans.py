"""
Range Fade backtest - results only: Plan 1 (SL10/TP10) vs Plan 2 (SL10/TP15).
Uses the Range Fade settings from main.py (24h range, zone, colour confirmation, 07-20 UTC, both sides).

    python backtest_plans.py                 (120 days, slippage 0.4)
    python backtest_plans.py --days 90 --slip 0.4
    python backtest_plans.py --csv data.csv  (columns: datetime,open,high,low,close, UTC)

Sends to Telegram (if TELEGRAM_* env set) and prints: summary, weekly and daily results per plan.
Slippage is taken off every trade. A candle touching both SL and TP counts as a loss.
"""
import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone

import backtest_ha as bt
import backtest_ha_opt as opt
import backtest_range as rng
import main as bot


def when(d, i):
    return datetime.fromtimestamp((d["t"][i] + bt.BAR_MS) / 1000, tz=timezone.utc)


def report(name, trs, d):
    n = len(trs)
    w = sum(1 for _, p in trs if p > 0)
    net = sum(p for _, p in trs)
    by_day, by_week = defaultdict(list), defaultdict(list)
    for i, p in trs:
        dt = when(d, i)
        by_day[dt.date().isoformat()].append(p)
        iso = dt.isocalendar()
        by_week[f"{iso[0]}-W{iso[1]:02d}"].append(p)

    def line(k, v):
        wins = sum(1 for p in v if p > 0)
        return f"{k}: {wins}W/{len(v) - wins}L | {sum(v):+.0f} pts"

    cum = peak = dd = 0.0
    for _, p in trs:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    summary = (f"{name}\nTrades {n} | {w}W/{n - w}L | Win rate {w / n * 100 if n else 0:.1f}% | "
               f"Net {net:+.0f} pts | Avg {net / n if n else 0:+.2f}/trade | Max drawdown {dd:.0f} pts | "
               f"Trading days {len(by_day)}")
    weekly = "Weekly\n" + "\n".join(line(k, v) for k, v in sorted(by_week.items()))
    daily = "Daily\n" + "\n".join(line(k, v) for k, v in sorted(by_day.items()))
    return summary, weekly, daily


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--slip", type=float, default=0.4)
    ap.add_argument("--csv")
    a = ap.parse_args()

    candles, start_ms = bt.load_csv(a.csv) if a.csv else bt.fetch_twelvedata(a.days)
    if len(candles) < 2000:
        sys.exit("Not enough candles.")
    d = opt.prepare(candles)
    d["op"] = [c["open"] for c in candles]
    first = max(next((i for i, t in enumerate(d["t"]) if t >= start_ms), 300), 300)

    L = bot.RF_LOOKBACK
    rmax, rmin = rng.rolling(d["hi"], L, True), rng.rolling(d["lo"], L, False)
    hours = bool(bot.RF_HOURS_UTC)
    sigs = [s for s in rng.build(d, L, bot.RF_ZONE, "color" if bot.RF_CONFIRM_COLOR else "none", hours, rmax, rmin)
            if s[0] >= first]
    side = {"both": "both", "short": "short", "long": "long"}[bot.RF_SIDE]

    head = f"Range Fade backtest | last {a.days} days | {d['n']} candles | slippage {a.slip:g}"
    messages = [head]
    print(head)
    for name, sl, tp in bot.RF_PLANS:
        trs = opt.simulate(d, sigs, side, sl, tp, a.slip)
        s, w, dl = report(f"{name} (SL {sl:g} / TP {tp:g})", trs, d)
        for m in (s, w, dl):
            messages.append(m)
            print("\n" + m)
    for m in messages:
        bt.send_tg(m)


if __name__ == "__main__":
    main()
