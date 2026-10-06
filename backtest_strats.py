"""Backtest SMA6 and EMA+RSI (and optionally others) from main.py on XAUUSD M5.

  python backtest_strats.py                      # SMA6 + EMA+RSI, 3 months
  STRATS="SMA6,EMA+RSI,MA Pullback" python backtest_strats.py
  SPREAD=0.3 MONTHS=3 GAP_BARS=12 python backtest_strats.py   # GAP_BARS=12 ~ 1 signal/hour

Each strategy's real check function from main.py is called on a rolling 800-bar
window, exactly like the live scan. Entry = close of the signal candle.
SL/TP checked on later candles; SL and TP in the same candle = LOSS.
A random-entry benchmark (same number of trades) shows the cost floor.
"""
import os, random
from datetime import datetime, timezone
import main as bot
import backtest_dg as bt

WINDOW = 800
GAP_BARS = int(os.environ.get("GAP_BARS", "1"))   # min bars between signals (live uses 1+)
AVAILABLE = {
    "SMA6": bot.check_sma6,
    "EMA+RSI": bot.check_ema_rsi,
    "MA Pullback": bot.check_ma_pullback,
    "SMA13/EMA18": bot.check_sma13_ema18,
    "Liquidity Sweep": bot.check_liquidity_sweep,
}


def signals(c, fn):
    out = []
    last = -10**9
    for i in range(60, len(c)):
        if i - last < GAP_BARS:
            continue
        d, _ = fn(c[max(0, i - WINDOW + 1): i + 1])
        if d:
            out.append((i, d))
            last = i
    return out


def random_baseline(c, n, sl, tp, runs=15):
    random.seed(7)
    tot = 0.0
    cnt = 0
    for _ in range(runs):
        res = []
        for _ in range(n):
            i = random.randrange(60, len(c) - 300)
            res.append(bt.simulate(c, i, random.choice(("long", "short")), sl, tp))
        res = [r for r in res if r is not None]
        tot += sum(res); cnt += len(res)
    return tot / cnt if cnt else 0.0


def month_of(c, i):
    t = c[i].get("open_time")
    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m") if t else None


def main():
    c = bt.load()
    print(f"{len(c)} candles, spread {bt.SPREAD} pt\n")
    names = [s.strip() for s in os.environ.get("STRATS", "SMA6,EMA+RSI").split(",")]
    for name in names:
        fn = AVAILABLE.get(name)
        if not fn:
            print(f"unknown strategy {name}\n"); continue
        sig = signals(c, fn)
        print(f"=== {name}: {len(sig)} signals ===")
        for label, (sl, tp) in {"SL7/TP6": (7, 6), "SL7/TP7": (7, 7)}.items():
            print(f"  {label} all     {bt.stats([bt.simulate(c, i, s, sl, tp) for i, s in sig])}")
        rb = random_baseline(c, max(len(sig), 30), 7, 6)
        print(f"  random entries SL7/TP6 exp={rb:+.2f}/trade  (benchmark to beat)")
        half = len(c) // 2
        for label, sub in (("1st half", [x for x in sig if x[0] < half]),
                           ("2nd half", [x for x in sig if x[0] >= half])):
            print(f"  SL7/TP6 {label} {bt.stats([bt.simulate(c, i, s, 7, 6) for i, s in sub])}")
        months = {}
        for i, s in sig:
            m = month_of(c, i)
            if m:
                months.setdefault(m, []).append(bt.simulate(c, i, s, 7, 6))
        for m in sorted(months):
            print(f"  SL7/TP6 {m}    {bt.stats(months[m])}")
        print()


if __name__ == "__main__":
    main()
