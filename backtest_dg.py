"""Backtest the Dynamic Grid strategy from main.py on XAUUSD M5.

Usage:
  python backtest_dg.py                 # fetches ~5000 M5 bars from TwelveData
  python backtest_dg.py data.csv        # CSV with columns: time,open,high,low,close
  SPREAD=0.3 python backtest_dg.py      # cost per trade in points (default 0.3)

Rules: entry = close of the signal candle. SL/TP checked on later candles.
If SL and TP are both inside one candle it counts as a LOSS (same as the bot).
"""
import csv, os, sys
import main as bot

SPREAD = float(os.environ.get("SPREAD", "0.3"))


def load():
    if len(sys.argv) > 1:
        out = []
        with open(sys.argv[1]) as f:
            for r in csv.DictReader(f):
                out.append({k: float(r[k]) for k in ("open", "high", "low", "close")})
        return out
    return bot._get_xauusd_m5_twelvedata(5000)


def simulate(c, i, side, sl, tp):
    e = c[i]["close"]
    for j in range(i + 1, len(c)):
        hi, lo = c[j]["high"], c[j]["low"]
        if side == "long":
            hit_sl, hit_tp = lo <= e - sl, hi >= e + tp
        else:
            hit_sl, hit_tp = hi >= e + sl, lo <= e - tp
        if hit_sl:
            return -sl - SPREAD          # SL first / both = loss
        if hit_tp:
            return tp - SPREAD
    return None                          # still open at end of data


def stats(res):
    res = [r for r in res if r is not None]
    if not res:
        return "no trades"
    w = sum(1 for r in res if r > 0)
    streak = best = 0
    for r in res:
        streak = streak + 1 if r <= 0 else 0
        best = max(best, streak)
    net = sum(res)
    return (f"n={len(res):3d} win={w/len(res)*100:4.1f}% net={net:+7.1f}pt "
            f"exp={net/len(res):+5.2f}/trade maxLossStreak={best}")


def main():
    c = load()
    print(f"{len(c)} candles, spread {SPREAD} pt\n")
    variants = {"SL7/TP6": (7, 6), "SL7/TP7": (7, 7)}
    for mode in ("trend", "reversion", "both"):
        bot.DG_MODE = mode
        g = bot.dynamic_grid_compute(c)
        sigs = [(i, g["signal"][i], g["level"][i], g["kind"][i])
                for i in range(len(c)) if g["signal"][i]]
        print(f"=== mode={mode}: {len(sigs)} signals ===")
        for name, (sl, tp) in variants.items():
            print(f"  {name:8s} all     {stats([simulate(c, i, s, sl, tp) for i, s, _, _ in sigs])}")
        for lv in range(1, bot.DG_NUM_LEVELS + 1):
            sub = [x for x in sigs if x[2] == lv]
            if sub:
                print(f"  SL7/TP6  deg {lv}   {stats([simulate(c, i, s, 7, 6) for i, s, _, _ in sub])}")
        # baseline-target exit (min 3 pts, max 12 pts), SL 7
        res = []
        for i, s, _, _ in sigs:
            d = abs(g["hma"][i] - c[i]["close"])
            res.append(simulate(c, i, s, 7, max(3.0, min(12.0, d))))
        print(f"  SL7/baseline-TP {stats(res)}\n")


if __name__ == "__main__":
    main()
