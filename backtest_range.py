"""
Range High/Low strategy tester (XAUUSD 5m)
Idea: when a 5m candle closes in the HIGH area of the recent range -> SELL entry at that close.
      when a 5m candle closes in the LOW area of the recent range  -> BUY entry at that close.

The TradingView "visible range" can't be used by a bot, so the range is the highest high / lowest low
of the last L candles (L=144 = 12h, 288 = 24h, 576 = 48h).

Run next to main.py, backtest_ha.py, backtest_ha_opt.py:
    python backtest_range.py                (120 days, slippage 0.4)
    python backtest_range.py --csv data.csv
Variants: lookback, zone size (how close to the high/low counts as "in the area"),
          confirmation candle (none / red-green candle / HA red-green), hours, side, SL/TP.
Slippage is subtracted from every trade. Both SL+TP in one candle = loss. One trade at a time.
TRAIN = first 2/3 of the data (ranking), TEST = last 1/3 (never used to rank).
"""
import argparse
import csv
import itertools
import sys
from collections import deque

import backtest_ha as bt
import backtest_ha_opt as opt

MIN_TRAIN, MIN_TEST = 60, 20


def rolling(vals, L, fn_max):
    out, dq = [], deque()
    for i, v in enumerate(vals):
        while dq and ((vals[dq[-1]] <= v) if fn_max else (vals[dq[-1]] >= v)):
            dq.pop()
        dq.append(i)
        if dq[0] <= i - L:
            dq.popleft()
        out.append(vals[dq[0]])
    return out


def build(d, L, zone, confirm, hours, rmax, rmin):
    hi, lo, cl, op, ho, hc = d["hi"], d["lo"], d["cl"], d["op"], d["ho"], d["hc"]
    sigs = []
    for i in range(max(300, L), d["n"]):
        if not d["open_ok"][i]:
            continue
        if hours and not (7 <= d["hour"][i] < 20):
            continue
        at_high = hi[i] >= rmax[i] - zone
        at_low = lo[i] <= rmin[i] + zone
        if at_high == at_low:          # neither, or range too tiny to tell
            continue
        if at_high:
            ok = (confirm == "none" or (confirm == "color" and cl[i] < op[i]) or (confirm == "ha" and hc[i] < ho[i]))
            if ok:
                sigs.append((i, "short"))
        else:
            ok = (confirm == "none" or (confirm == "color" and cl[i] > op[i]) or (confirm == "ha" and hc[i] > ho[i]))
            if ok:
                sigs.append((i, "long"))
    return sigs


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
    split = first + 2 * (d["n"] - first) // 3
    print(f"[data] {d['n']} candles | train {split - first} bars, test {d['n'] - split} bars")

    roll = {L: (rolling(d["hi"], L, True), rolling(d["lo"], L, False)) for L in (144, 288, 576)}
    sltp = [(7, 6), (8, 6), (6, 6), (5, 5), (10, 10)]
    rows = []
    for L, zone, confirm, hours in itertools.product((144, 288, 576), (1, 3, 6), ("none", "color", "ha"), (False, True)):
        sigs = [s for s in build(d, L, zone, confirm, hours, *roll[L]) if s[0] >= first]
        for side in ("both", "long", "short"):
            for sl, tp in sltp:
                trs = opt.simulate(d, sigs, side, sl, tp, a.slip)
                tr = [x for x in trs if x[0] < split]
                te = [x for x in trs if x[0] >= split]
                nm = {"both": "BOTH", "long": "BUY-low", "short": "SELL-high"}[side]
                lab = f"L{L} z{zone} conf-{confirm} {'LDN+NY' if hours else 'allhrs'} {nm} SL{sl}/TP{tp}"
                rows.append((lab, opt.stats(tr), opt.stats(te), opt.stats(trs)))
    print(f"[done] {len(rows)} variants")

    def fmt(r):
        lab, tr, te, _ = r
        return (f"{lab}\n  TRAIN {tr[0]}t {tr[1]:.0f}% {tr[2]:+.0f}pts ({tr[3]:+.2f}/t) | "
                f"TEST {te[0]}t {te[1]:.0f}% {te[2]:+.0f}pts ({te[3]:+.2f}/t)")

    elig = [r for r in rows if r[1][0] >= MIN_TRAIN and r[2][0] >= MIN_TEST]
    by_train = sorted(elig, key=lambda r: r[1][3], reverse=True)
    both_pos = [r for r in by_train if r[1][2] > 0 and r[2][2] > 0]
    robust = sorted(both_pos, key=lambda r: min(r[1][3], r[2][3]), reverse=True)
    base = next((r for r in rows if r[0] == "L288 z3 conf-none allhrs BOTH SL7/TP6"), None)

    msg = [f"Range High/Low test | {d['n']} candles | slippage {a.slip:g} | {len(rows)} variants | "
           f"TRAIN = first 2/3, TEST = last 1/3 (not used for ranking)"]
    if base:
        msg.append("Plain idea (24h range, zone 3, no confirmation, both sides, 7/6):\n" + fmt(base))
    msg.append(f"Variants positive in BOTH train and test: {len(both_pos)} of {len(elig)} "
               f"({len(both_pos) / max(len(elig), 1) * 100:.1f}%). On random prices this number swings from about 1% to 11%, "
               f"so it is NOT proof. Look for winners sharing the same settings, then confirm with live forward testing.")
    msg.append("TOP 8 by robustness:")
    msg += [f"{k + 1}. " + fmt(r) for k, r in enumerate(robust[:8])] or ["none profitable in both periods"]
    msg.append("TOP 5 by TRAIN only (watch TEST):")
    msg += [f"{k + 1}. " + fmt(r) for k, r in enumerate(by_train[:5])]
    text = "\n\n".join(msg)

    with open("range_variants.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["variant", "train_n", "train_wr", "train_net", "train_avg",
                    "test_n", "test_wr", "test_net", "test_avg"])
        for lab, tr, te, _ in by_train:
            w.writerow([lab, tr[0], f"{tr[1]:.1f}", f"{tr[2]:.0f}", f"{tr[3]:.3f}",
                        te[0], f"{te[1]:.1f}", f"{te[2]:.0f}", f"{te[3]:.3f}"])
    print("\n" + text)
    bt.send_tg(text)


if __name__ == "__main__":
    main()
