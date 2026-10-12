"""
Quantum Imbalance Trap (QIT) backtest - XAUUSD 5m, with daily and weekly results.

Settings taken from your indicator screenshots:
    Imbalance lookback 7 bars | ATR period 10 | Body-to-range threshold 0.7
    Stop loss = 1.9 x ATR | TP1 = 1R | TP2 = 1.5R | TP3 = 2R
    RSI divergence, liquidity sweep, higher-timeframe alignment, re-entry zone, session filter: all OFF
    (Volume spike multiplier 1 is not used: the price feed used here has no volume.)

The indicator is closed-source, so its exact entry rule is not known. Two reasonable readings are tested:
  PULLBACK  imbalance candle (strong body, range >= ATR) -> price pulls back into its body (50% level)
            and closes back in the imbalance direction -> trade WITH the imbalance.
  TRAP      imbalance candle -> price sweeps beyond its extreme within 7 bars and closes back inside
            with the opposite colour -> trade AGAINST the imbalance (trapped traders).
Entry = close of the trigger candle. A candle touching both SL and TP counts as a loss.
Slippage is taken off every trade. One trade at a time per plan.

    python backtest_qit.py                 (120 days, slippage 0.4)
    python backtest_qit.py --days 90
    python backtest_qit.py --csv data.csv
Output: summary of all 6 combinations, then weekly + daily results of the best one (by net points).
"""
import argparse
import sys

import backtest_ha as bt
import backtest_ha_opt as opt
from collections import defaultdict
from datetime import datetime, timezone

LOOKBACK = 7
ATR_PERIOD = 10
BODY_MIN = 0.7
SL_ATR = 1.9
TP_PLANS = (("TP 1R", 1.0), ("TP 1.5R", 1.5), ("TP 2R", 2.0))


def atr_series(d, period=14):
    hi, lo, cl = d["hi"], d["lo"], d["cl"]
    tr = [hi[0] - lo[0]]
    for i in range(1, d["n"]):
        tr.append(max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])))
    out, run = [], 0.0
    for i, v in enumerate(tr):
        run += v
        if i >= period:
            run -= tr[i - period]
        out.append(run / min(i + 1, period))
    return out


def report(name, trs, d):
    n = len(trs)
    w = sum(1 for _, p in trs if p > 0)
    net = sum(p for _, p in trs)
    by_day, by_week = defaultdict(list), defaultdict(list)
    for i, p in trs:
        dt = datetime.fromtimestamp((d["t"][i] + bt.BAR_MS) / 1000, tz=timezone.utc)
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


def find_signals(d, atr, mode):
    hi, lo, op, cl, n = d["hi"], d["lo"], d["op"], d["cl"], d["n"]
    imb = [0] * n                     # +1 bullish imbalance candle, -1 bearish, 0 none
    for j in range(n):
        rng = hi[j] - lo[j]
        if rng > 0 and abs(cl[j] - op[j]) / rng >= BODY_MIN and rng >= atr[j]:
            imb[j] = 1 if cl[j] > op[j] else -1
    sigs = []
    for i in range(300, n):
        if not d["open_ok"][i]:
            continue
        j = next((k for k in range(i - 1, i - LOOKBACK - 1, -1) if imb[k]), None)
        if j is None:
            continue
        top, bot = max(op[j], cl[j]), min(op[j], cl[j])
        mid = (top + bot) / 2.0
        direction = None
        if mode == "pullback":
            if imb[j] == -1:                                   # bearish impulse -> SHORT on pullback rejection
                if all(hi[m] < mid for m in range(j + 1, i)) and hi[i] >= mid and cl[i] < op[i] and cl[i] < mid:
                    direction = "short"
            else:                                              # bullish impulse -> LONG on pullback rejection
                if all(lo[m] > mid for m in range(j + 1, i)) and lo[i] <= mid and cl[i] > op[i] and cl[i] > mid:
                    direction = "long"
        else:  # trap
            if imb[j] == -1:                                   # bearish impulse swept below, closes back -> LONG
                if all(lo[m] >= lo[j] for m in range(j + 1, i)) and lo[i] < lo[j] and cl[i] > lo[j] and cl[i] > op[i]:
                    direction = "long"
            else:                                              # bullish impulse swept above, closes back -> SHORT
                if all(hi[m] <= hi[j] for m in range(j + 1, i)) and hi[i] > hi[j] and cl[i] < hi[j] and cl[i] < op[i]:
                    direction = "short"
        if direction:
            sigs.append((i, direction, SL_ATR * atr[i]))
    return sigs


def simulate(d, sigs, k, slip):
    hi, lo, cl, n = d["hi"], d["lo"], d["cl"], d["n"]
    out, busy = [], -1
    for i, dr, sl in sigs:
        if i < busy or sl <= 0:
            continue
        e, tp = cl[i], k * sl
        long_ = dr == "long"
        stop, tgt = (e - sl, e + tp) if long_ else (e + sl, e - tp)
        for j in range(i + 1, n):
            hs = lo[j] <= stop if long_ else hi[j] >= stop
            ht = hi[j] >= tgt if long_ else lo[j] <= tgt
            if hs:
                out.append((i, -(sl + slip)))
                busy = j
                break
            if ht:
                out.append((i, tp - slip))
                busy = j
                break
        else:
            break
    return out


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
    atr = atr_series(d, ATR_PERIOD)

    results = []
    for mode in ("pullback", "trap"):
        sigs = [s for s in find_signals(d, atr, mode) if s[0] >= first]
        avg_sl = sum(s[2] for s in sigs) / len(sigs) if sigs else 0.0
        for name, k in TP_PLANS:
            trs = simulate(d, sigs, k, a.slip)
            n = len(trs)
            w = sum(1 for _, p in trs if p > 0)
            net = sum(p for _, p in trs)
            results.append((f"QIT {mode.upper()} {name}", n, w, net, avg_sl, trs))

    lines = [f"QIT backtest | last {a.days} days | {d['n']} candles | slippage {a.slip:g} | "
             f"SL 1.9 x ATR(10), lookback 7, body 0.7"]
    for label, n, w, net, avg_sl, _ in results:
        lines.append(f"{label}: {n} trades | {w}W/{n - w}L | WR {w / n * 100 if n else 0:.1f}% | "
                     f"net {net:+.0f} pts | avg {net / n if n else 0:+.2f}/trade | avg SL {avg_sl:.1f} pts")
    summary = "\n".join(lines)
    print(summary)
    bt.send_tg(summary)

    best = max(results, key=lambda r: r[3])
    s, w, dl = report(f"BEST BY NET: {best[0]}", best[5], d)
    for m in (s, w, dl):
        print("\n" + m)
        bt.send_tg(m)


if __name__ == "__main__":
    main()
