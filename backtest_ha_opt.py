"""
HA Reversal - variant tester with slippage and out-of-sample check.

Run next to main.py and backtest_ha.py:
    python backtest_ha_opt.py                  (120 days, slippage 0.4)
    python backtest_ha_opt.py --days 120 --slip 0.4
    python backtest_ha_opt.py --csv data.csv   (columns: datetime,open,high,low,close, UTC)

What it does
- Builds Heiken Ashi candles once, then tests many rule variants:
    rule      strict (candle right before trigger must be opposite colour, old rule)
              or loose (1-2 same-colour candles may sit right before trigger, new rule)
    run       4 or 5 opposite HA candles, 1 or 2 counter candles allowed
    wick      flat-wick tolerance 0.05 or 0.30
    body      minimum trigger-candle HA body 0 / 2 points
    trend     none, or only trade WITH the trend (price vs 5m EMA 100 / 300)
    hours     all hours, or London+NY only (07:00-20:00 UTC)
    side      both / buy only / sell only
    SL/TP     7/6, 6/6, 5/5, 8/6
- Slippage: every trade is filled `slip` points worse (win = TP - slip, loss = -(SL + slip)).
- A candle touching both SL and TP counts as a loss. One trade at a time.
- The data is split in time: first 2/3 = TRAIN (ranking), last 1/3 = TEST (never used to rank).
  Trust only variants that are positive in BOTH.
Needs env TWELVEDATA_API_KEY (unless --csv). Telegram report if TELEGRAM_* env set.
"""
import argparse
import csv
import itertools
import os
import sys
from datetime import datetime, timezone

import backtest_ha as bt
import main as bot

MIN_TRAIN_TRADES = 60
MIN_TEST_TRADES = 20


def ema_series(vals, period):
    k = 2.0 / (period + 1)
    out, e = [], vals[0]
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def prepare(candles):
    n = len(candles)
    ha = bot.heiken_ashi(candles)
    d = {
        "n": n,
        "ho": [h["open"] for h in ha], "hc": [h["close"] for h in ha],
        "hh": [h["high"] for h in ha], "hl": [h["low"] for h in ha],
        "hi": [c["high"] for c in candles], "lo": [c["low"] for c in candles],
        "cl": [c["close"] for c in candles],
        "t": [c["open_time"] for c in candles],
    }
    d["hour"], d["open_ok"] = [], []
    for c in candles:
        cd = datetime.fromtimestamp((c["open_time"] + bt.BAR_MS) / 1000, tz=timezone.utc)
        d["hour"].append(cd.hour)
        d["open_ok"].append(bot.is_octa_xauusd_open(cd))
    d["ema"] = {100: ema_series(d["cl"], 100), 300: ema_series(d["cl"], 300)}
    return d


def build_signals(d, cfg):
    ho, hc, hh, hl, cl = d["ho"], d["hc"], d["hh"], d["hl"], d["cl"]
    strict, reds, counter = cfg["mode"] == "strict", cfg["reds"], cfg["counter"]
    wick, body, trend, hours = cfg["wick"], cfg["body"], cfg["trend"], cfg["hours"]
    ema = d["ema"].get(trend) if trend else None
    sigs = []
    for i in range(300, d["n"]):          # 300 bars warm-up (HA + EMA settle)
        if not d["open_ok"][i]:
            continue
        if hours and not (7 <= d["hour"][i] < 20):
            continue
        o, c = ho[i], hc[i]
        if c > o:
            green = True
            if o - hl[i] > wick:
                continue
        elif c < o:
            green = False
            if hh[i] - o > wick:
                continue
        else:
            continue
        if abs(c - o) < body:
            continue
        if ema is not None:      # trade WITH the trend: buy dips in uptrend, sell rallies in downtrend
            if green and cl[i] < ema[i]:
                continue
            if (not green) and cl[i] > ema[i]:
                continue
        k = i - 1
        if strict and not ((hc[k] < ho[k]) if green else (hc[k] > ho[k])):
            continue
        need = cnt = 0
        ok = True
        while k >= 0 and need < reds:
            opp = (hc[k] < ho[k]) if green else (hc[k] > ho[k])
            if opp:
                need += 1
            else:
                cnt += 1
                if cnt > counter:
                    ok = False
                    break
            k -= 1
        if ok and need >= reds:
            sigs.append((i, "long" if green else "short"))
    return sigs


def simulate(d, sigs, side, sl, tp, slip):
    hi, lo, cl, n = d["hi"], d["lo"], d["cl"], d["n"]
    out, busy = [], -1
    for i, dr in sigs:
        if i < busy or (side != "both" and dr != side):
            continue
        e = cl[i]
        long_ = dr == "long"
        sl_p, tp_p = (e - sl, e + tp) if long_ else (e + sl, e - tp)
        for j in range(i + 1, n):
            hs = lo[j] <= sl_p if long_ else hi[j] >= sl_p
            ht = hi[j] >= tp_p if long_ else lo[j] <= tp_p
            if hs or ht:
                out.append((i, -(sl + slip) if hs else tp - slip))
                busy = j
                break
        else:
            break
    return out


def stats(trs):
    n = len(trs)
    if not n:
        return 0, 0.0, 0.0, 0.0
    w = sum(1 for _, p in trs if p > 0)
    net = sum(p for _, p in trs)
    return n, w / n * 100, net, net / n


def label(cfg, side, sl, tp):
    return (f"{cfg['mode']} r{cfg['reds']} c{cfg['counter']} w{cfg['wick']:g} b{cfg['body']:g} "
            f"{'EMA' + str(cfg['trend']) if cfg['trend'] else 'notrend'} "
            f"{'LDN+NY' if cfg['hours'] else 'allhrs'} {side.upper()} SL{sl:g}/TP{tp:g}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--slip", type=float, default=0.4)
    ap.add_argument("--csv")
    a = ap.parse_args()

    candles, start_ms = bt.load_csv(a.csv) if a.csv else bt.fetch_twelvedata(a.days)
    if len(candles) < 2000:
        sys.exit("Not enough candles.")
    d = prepare(candles)
    first = next((i for i, t in enumerate(d["t"]) if t >= start_ms), 300)
    first = max(first, 300)
    split = first + 2 * (d["n"] - first) // 3
    print(f"[data] {d['n']} candles | train {split - first} bars, test {d['n'] - split} bars")

    grid = itertools.product(
        ("strict", "loose"), (4, 5), (1, 2), (0.05, 0.3), (0, 2), (None, 100, 300), (False, True))
    sltp = [(7, 6), (6, 6), (5, 5), (8, 6)]
    rows = []
    for mode, reds, counter, wick, body, trend, hours in grid:
        cfg = dict(mode=mode, reds=reds, counter=counter, wick=wick, body=body, trend=trend, hours=hours)
        sigs = [s for s in build_signals(d, cfg) if s[0] >= first]
        for side in ("both", "long", "short"):
            for sl, tp in sltp:
                trs = simulate(d, sigs, side, sl, tp, a.slip)
                tr = [x for x in trs if x[0] < split]
                te = [x for x in trs if x[0] >= split]
                rows.append((label(cfg, "buy" if side == "long" else "sell" if side == "short" else "both", sl, tp),
                             stats(tr), stats(te), stats(trs)))
    print(f"[done] {len(rows)} variants tested")

    def fmt(r):
        lab, tr, te, al = r
        return (f"{lab}\n  TRAIN {tr[0]}t {tr[1]:.0f}% {tr[2]:+.0f}pts ({tr[3]:+.2f}/t) | "
                f"TEST {te[0]}t {te[1]:.0f}% {te[2]:+.0f}pts ({te[3]:+.2f}/t)")

    eligible = [r for r in rows if r[1][0] >= MIN_TRAIN_TRADES and r[2][0] >= MIN_TEST_TRADES]
    by_train = sorted(eligible, key=lambda r: r[1][3], reverse=True)
    both_pos = [r for r in by_train if r[1][2] > 0 and r[2][2] > 0]
    robust = sorted(both_pos, key=lambda r: min(r[1][3], r[2][3]), reverse=True)

    base = {r[0]: r for r in rows}
    old = base.get("strict r4 c2 w0.05 b0 notrend allhrs BOTH SL7/TP6")
    new = base.get("loose r4 c2 w0.05 b0 notrend allhrs BOTH SL7/TP6")

    msg = [f"HA Reversal variant test | {d['n']} candles | slippage {a.slip:g} | "
           f"{len(rows)} variants | TRAIN = first 2/3, TEST = last 1/3 (not used for ranking)"]
    if old:
        msg.append("OLD strict rule (7/6):\n" + fmt(old))
    if new:
        msg.append("NEW loose rule (7/6):\n" + fmt(new))
    msg.append(f"Variants positive in BOTH train and test: {len(both_pos)} of {len(eligible)} "
               f"({len(both_pos) / max(len(eligible), 1) * 100:.1f}%). On pure random prices about 1-2% pass by luck, "
               f"so only trust it if this is clearly higher and the winners share the same settings.")
    msg.append("TOP 8 by robustness (worst of train/test per-trade):")
    msg += [f"{k + 1}. " + fmt(r) for k, r in enumerate(robust[:8])] or ["none - no variant was profitable in both periods"]
    msg.append("TOP 5 by TRAIN only (watch the TEST numbers):")
    msg += [f"{k + 1}. " + fmt(r) for k, r in enumerate(by_train[:5])]
    msg.append("A variant that wins in TRAIN but loses in TEST is just luck. Don't trade it.")
    text = "\n\n".join(msg)

    with open("ha_variants.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["variant", "train_n", "train_wr", "train_net", "train_avg",
                    "test_n", "test_wr", "test_net", "test_avg", "all_n", "all_net"])
        for lab, tr, te, al in by_train + [r for r in rows if r not in by_train]:
            w.writerow([lab, tr[0], f"{tr[1]:.1f}", f"{tr[2]:.0f}", f"{tr[3]:.3f}",
                        te[0], f"{te[1]:.1f}", f"{te[2]:.0f}", f"{te[3]:.3f}", al[0], f"{al[2]:.0f}"])
    print("\n" + text)
    bt.send_tg(text)
    print("\nSaved ha_variants.csv")


if __name__ == "__main__":
    main()
