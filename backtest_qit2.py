"""
QIT (Quantum Imbalance Trap) - backtest using the REAL rules from the indicator's Pine source.

How the indicator works (read from the source):
  * Imbalance candle = body/range >= threshold AND volume spike AND short trend agrees
    (SMA5 vs SMA(2 x lookback)): green candle in an up-trend = bullish, red in a down-trend = bearish.
  * Signal = the FIRST imbalance candle of a run (previous candle was not one). Entry = that candle's CLOSE.
  * SL = entry -/+ SL_mult x ATR ; TP1/TP2/TP3 = 1R / 1.5R / 2R (your settings).
  * Optional filters (all OFF in your settings): liquidity sweep, higher-timeframe (1H SMA20) alignment,
    RSI divergence, London / New York session.

Your settings: lookback 7, ATR 10, body 0.7, volume spike 1.0, SL 1.9 x ATR, TP 1 / 1.5 / 2 R.
Note: the price data used here has no volume, so "volume spike" uses candle RANGE vs its average as a stand-in.

    python backtest_qit2.py                 (120 days, slippage 0.4)
    python backtest_qit2.py --csv data.csv
Output (Telegram + console): your exact settings (3 targets) first, then ~7,000 filter / SL / TP variants
ranked on TRAIN (first 2/3 of data) and checked on TEST (last 1/3, never used for ranking),
then weekly + daily results of the best variant that held up in both periods.
Slippage is taken off every trade. A candle touching both SL and TP counts as a loss. One trade at a time.
"""
import argparse
import itertools
import sys
from datetime import datetime, timezone

import backtest_ha as bt
import backtest_ha_opt as opt
from backtest_qit import report

MIN_TRAIN, MIN_TEST = 60, 20
ATR_LEN = 10


# ---------- indicator helpers ----------
def rma(vals, period):
    out, r = [], 0.0
    for i, v in enumerate(vals):
        r = v if i == 0 else ((r * i + v) / (i + 1) if i < period else (r * (period - 1) + v) / period)
        out.append(r)
    return out


def sma(vals, period):
    out, run = [], 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= period:
            run -= vals[i - period]
        out.append(run / min(i + 1, period))
    return out


def rsi(cl, period=14):
    gains = [0.0] + [max(cl[i] - cl[i - 1], 0.0) for i in range(1, len(cl))]
    losses = [0.0] + [max(cl[i - 1] - cl[i], 0.0) for i in range(1, len(cl))]
    ag, al = rma(gains, period), rma(losses, period)
    return [100.0 if al[i] == 0 else 100.0 - 100.0 / (1.0 + ag[i] / al[i]) for i in range(len(cl))]


def htf_trend(d):
    """1H trend at each 5m bar: last COMPLETED 1H close vs SMA20 of 1H closes. +1 bull, -1 bear, 0 unknown."""
    t, cl, n = d["t"], d["cl"], d["n"]
    HOUR, BAR = 3600000, 300000
    out = [0] * n
    closes = []            # completed hourly closes
    cur_bucket, cur_close = None, None
    for i in range(n):
        b = t[i] // HOUR
        if cur_bucket is None:
            cur_bucket = b
        if b != cur_bucket:
            closes.append(cur_close)
            cur_bucket = b
        cur_close = cl[i]
        done = list(closes)
        if (t[i] + BAR) % HOUR == 0:          # this bar completes the hour
            done = closes + [cl[i]]
        if len(done) >= 20:
            m = sum(done[-20:]) / 20.0
            out[i] = 1 if done[-1] > m else (-1 if done[-1] < m else 0)
    return out


def prepare(candles):
    d = opt.prepare(candles)
    d["op"] = [c["open"] for c in candles]
    n = d["n"]
    d["rng"] = [d["hi"][i] - d["lo"][i] for i in range(n)]
    tr = [d["rng"][0]] + [max(d["hi"][i] - d["lo"][i], abs(d["hi"][i] - d["cl"][i - 1]), abs(d["lo"][i] - d["cl"][i - 1]))
                          for i in range(1, n)]
    d["atr"] = rma(tr, ATR_LEN)
    d["sma5"] = sma(d["cl"], 5)
    d["rsi"] = rsi(d["cl"], 14)
    d["htf"] = htf_trend(d)
    d["utc_min"] = [datetime.fromtimestamp(t / 1000, tz=timezone.utc).hour * 60 +
                    datetime.fromtimestamp(t / 1000, tz=timezone.utc).minute for t in d["t"]]
    return d


def build_signals(d, L, thr, vm, liq, htf, sess, div, cache):
    hi, lo, op, cl, rng, n = d["hi"], d["lo"], d["op"], d["cl"], d["rng"], d["n"]
    if L not in cache:
        cache[L] = (sma(cl, 2 * L), sma(rng, L))
    slow, rng_avg = cache[L]
    sma5, atr, rs, hv = d["sma5"], d["atr"], d["rsi"], d["htf"]
    imb = [0] * n
    for k in range(n):
        if rng[k] <= 0:
            continue
        br = abs(cl[k] - op[k]) / rng[k]
        spike = rng_avg[k] > 0 and rng[k] / rng_avg[k] >= vm
        if br >= thr and spike:
            if cl[k] > op[k] and sma5[k] > slow[k]:
                imb[k] = 1
            elif cl[k] < op[k] and sma5[k] < slow[k]:
                imb[k] = -1
    sigs = []
    for k in range(300, n):
        if not imb[k] or imb[k - 1] == imb[k] or not d["open_ok"][k]:
            continue
        bull = imb[k] == 1
        if sess:
            m = d["utc_min"][k]
            if not (7 * 60 <= m < 10 * 60 + 30 or 12 * 60 + 30 <= m < 15 * 60):
                continue
        if htf and hv[k] != (1 if bull else -1):
            continue
        if div:
            if bull and not (lo[k] < lo[k - L] and rs[k] > rs[k - L]):
                continue
            if not bull and not (hi[k] > hi[k - L] and rs[k] < rs[k - L]):
                continue
        if liq:
            a, b = max(k - 3 - L + 1, 0), k - 3 + 1
            if bull:
                ref = min(lo[a:b])
                if not (min(lo[k], lo[k - 1], lo[k - 2]) < ref and cl[k] > ref):
                    continue
            else:
                ref = max(hi[a:b])
                if not (max(hi[k], hi[k - 1], hi[k - 2]) > ref and cl[k] < ref):
                    continue
        sigs.append((k, "long" if bull else "short", atr[k]))
    return sigs


def simulate(d, sigs, side, sl_mult, tp_r, slip):
    hi, lo, cl, n = d["hi"], d["lo"], d["cl"], d["n"]
    out, busy = [], -1
    for k, dr, a in sigs:
        if k < busy or (side != "both" and dr != side):
            continue
        sl = sl_mult * a
        if sl <= 0:
            continue
        tp = tp_r * sl
        e = cl[k]
        long_ = dr == "long"
        stop, tgt = (e - sl, e + tp) if long_ else (e + sl, e - tp)
        for j in range(k + 1, n):
            hs = lo[j] <= stop if long_ else hi[j] >= stop
            ht = hi[j] >= tgt if long_ else lo[j] <= tgt
            if hs:
                out.append((k, -(sl + slip)))
                busy = j
                break
            if ht:
                out.append((k, tp - slip))
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
    first = max(next((i for i, t in enumerate(d["t"]) if t >= start_ms), 300), 300)
    split = first + 2 * (d["n"] - first) // 3
    print(f"[data] {d['n']} candles | train {split - first} bars, test {d['n'] - split} bars")

    cache, rows = {}, []
    sides = ("both", "long", "short")
    for L, thr, vm, liq, htf, sess, div in itertools.product(
            (7, 10), (0.55, 0.7, 0.8), (1.0, 1.2), (False, True), (False, True), (False, True), (False, True)):
        sigs = [s for s in build_signals(d, L, thr, vm, liq, htf, sess, div, cache) if s[0] >= first]
        for side, slm, tpr in itertools.product(sides, (1.5, 1.9, 2.5), (1.0, 1.5, 2.0, 3.0)):
            trs = simulate(d, sigs, side, slm, tpr, a.slip)
            tr = [x for x in trs if x[0] < split]
            te = [x for x in trs if x[0] >= split]
            lab = (f"L{L} body{thr:g} vol{vm:g}{' SWEEP' if liq else ''}{' HTF' if htf else ''}"
                   f"{' LDN+NY' if sess else ''}{' RSIdiv' if div else ''} "
                   f"{ {'both': 'BOTH', 'long': 'BUY', 'short': 'SELL'}[side] } SL{slm:g}xATR TP{tpr:g}R")
            rows.append((lab, stats(tr), stats(te), trs, (L, thr, vm, liq, htf, sess, div, side, slm, tpr)))
    print(f"[done] {len(rows)} variants")

    def fmt(r):
        lab, tr, te = r[0], r[1], r[2]
        return (f"{lab}\n  TRAIN {tr[0]}t {tr[1]:.0f}% {tr[2]:+.0f} ({tr[3]:+.2f}/t) | "
                f"TEST {te[0]}t {te[1]:.0f}% {te[2]:+.0f} ({te[3]:+.2f}/t)")

    base = [r for r in rows if r[4][:4] == (7, 0.7, 1.0, False) and r[4][4:] [:3] == (False, False, False)
            and r[4][7] == "both" and r[4][8] == 1.9 and r[4][9] in (1.0, 1.5, 2.0)]
    elig = [r for r in rows if r[1][0] >= MIN_TRAIN and r[2][0] >= MIN_TEST]
    both_pos = [r for r in elig if r[1][2] > 0 and r[2][2] > 0]
    robust = sorted(both_pos, key=lambda r: min(r[1][3], r[2][3]), reverse=True)
    by_train = sorted(elig, key=lambda r: r[1][3], reverse=True)

    msg = [f"QIT exact-rules test | {d['n']} candles | slippage {a.slip:g} | {len(rows)} variants | "
           f"TRAIN = first 2/3, TEST = last 1/3 (not used for ranking). Volume spike uses candle range as a stand-in.",
           "YOUR SETTINGS (no filters, both sides, SL 1.9 x ATR):\n" + "\n".join(fmt(r) for r in base),
           f"Variants positive in BOTH train and test: {len(both_pos)} of {len(elig)} "
           f"({len(both_pos) / max(len(elig), 1) * 100:.1f}%). On random prices this swings from about 1% to 11%, "
           f"so it is NOT proof. Look for winners that share the same settings.",
           "TOP 8 by robustness:\n" + ("\n".join(f"{i + 1}. " + fmt(r) for i, r in enumerate(robust[:8]))
                                       or "none profitable in both periods"),
           "TOP 5 by TRAIN only (watch TEST):\n" + "\n".join(f"{i + 1}. " + fmt(r) for i, r in enumerate(by_train[:5]))]
    text = "\n\n".join(msg)
    print("\n" + text)
    bt.send_tg(text)

    pick = robust[0] if robust else base[0]
    s, w, dl = report(f"WEEKLY/DAILY for: {pick[0]}", pick[3], d)
    for m in (s, w, dl):
        print("\n" + m)
        bt.send_tg(m)


if __name__ == "__main__":
    main()
