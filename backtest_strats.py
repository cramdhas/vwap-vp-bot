"""Backtest the strategies in main.py on XAUUSD M5 (market-hours only, 3 months).

  python backtest_strats.py
  STRATS="SMA6,HalfTrend" VERBOSE=1 python backtest_strats.py

Env: STRATS (comma list), SPREAD (default 0.3), MONTHS (3), VERBOSE (0/1),
     DETAIL ("8x8,10x8,10x10" combos split by half/month when VERBOSE=1),
     SESSION ("7-20" = only signals 07:00-20:00 UTC), GAP_BARS, TIE ("sl"/"tp").

Each strategy's real check function from main.py is called on a rolling 800-bar
window like the live scan. Entry = close of the signal candle; one open trade per
strategy; SL and TP in the same candle = LOSS (TIE=sl). The VP strategies use their
OWN SL/TP from the bot (structure SL, 2R). OB Engine 5m is not supported.
"""
import contextlib, io, os, random, sys
from datetime import datetime, timezone
import main as bot
import backtest_dg as bt

WINDOW = 800
VERBOSE = os.environ.get("VERBOSE", "0") == "1"
DETAIL = [x for x in os.environ.get("DETAIL", "8x8,10x8,10x10").split(",") if x]
GRID = os.environ.get("GRID", "1") == "1"
SESSION = os.environ.get("SESSION", "")
GAP_BARS = int(os.environ.get("GAP_BARS", "1"))
COMBOS = ((7, 6), (8, 8), (10, 8), (10, 10))
ALL_NAMES = ("SMA6,EMA+RSI,MA Pullback,SMA13/EMA18,Liquidity Sweep,HalfTrend,"
             "MA3/25 Cross,SMA18 Touch,VWAP+VP,VP POC Bounce,VP Reversal,VP Breakout")

AVAILABLE = {
    "SMA6": bot.check_sma6,
    "EMA+RSI": bot.check_ema_rsi,
    "MA Pullback": bot.check_ma_pullback,
    "SMA13/EMA18": bot.check_sma13_ema18,
    "Liquidity Sweep": bot.check_liquidity_sweep,
    "HalfTrend": lambda cs: bot.check_halftrend(cs, {}),
    "MA3/25 Cross": lambda cs: bot.check_ma2_148_cross(cs, cs[-1]["close"], None),
    "SMA18 Touch": lambda cs: bot.check_sma18_touch(cs, cs[-1]["close"]),
    "VWAP+VP": lambda cs: bot.check_vwap_vp(cs, bot.session_vwap(cs), bot.volume_profile(cs[-200:])),
}
VP_FUNCS = {
    "VP POC Bounce": bot.check_vp_poc_bounce,
    "VP Reversal": bot.check_vp_reversal,
    "VP Breakout": bot.check_vp_breakout,
}
_errors = {}


def signals(c, fn, name):
    out, last = [], -10**9
    for i in range(60, len(c)):
        if i - last < GAP_BARS:
            continue
        try:
            d, _ = fn(c[max(0, i - WINDOW + 1): i + 1])
        except Exception as exc:
            _errors[name] = _errors.get(name, 0) + 1
            if _errors[name] == 1:
                print(f"[{name}] error on a bar: {type(exc).__name__}: {exc}")
            continue
        if d:
            out.append((i, d))
            last = i
    return out


def vp_all_signals(c):
    """Run the three VP strategies together (shared session profile per bar)."""
    gap = max(1, int(bot.VP_REFIRE_SECONDS // 300))
    out = {n: [] for n in VP_FUNCS}
    last = {}
    buf = io.StringIO()
    for i in range(60, len(c)):
        win = c[max(0, i - WINDOW + 1): i + 1]
        try:
            with contextlib.redirect_stdout(buf):
                ctx = bot.vp_context(win)
        except Exception:
            ctx = None
        buf.seek(0); buf.truncate(0)
        if not ctx:
            continue
        for n, fn in VP_FUNCS.items():
            bot.VP_PLAN.pop(n, None)
            try:
                with contextlib.redirect_stdout(buf):
                    d, _ = fn(win, ctx)
            except Exception:
                d = None
            buf.seek(0); buf.truncate(0)
            if not d or i - last.get((n, d), -10**9) < gap:
                continue
            plan = bot.VP_PLAN.get(n)
            if not plan:
                continue
            last[(n, d)] = i
            out[n].append((i, d, {"sl": plan[0], "tp": plan[1]}))
    return out


def random_baseline(c, n, sl, tp, runs=15):
    random.seed(7)
    tot, cnt = 0.0, 0
    for _ in range(runs):
        res = [bt.simulate(c, random.randrange(60, len(c) - 300),
                           random.choice(("long", "short")), sl, tp) for _ in range(n)]
        res = [r for r in res if r is not None]
        tot += sum(res); cnt += len(res)
    return tot / cnt if cnt else 0.0


def month_of(c, i):
    t = c[i].get("open_time")
    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m") if t else None


def pn(rows):
    return [r[3] for r in rows]


def analyze(name, sig, c, native):
    half = len(c) // 2
    row = {"name": name, "raw": len(sig), "native": native}
    if native:
        tr = bt.trades_for(c, sig, 0, 0)
        row["n"] = len(tr)
        row["net"] = sum(pn(tr))
        row["h1"] = sum(pn([r for r in tr if r[0] < half]))
        row["h2"] = sum(pn([r for r in tr if r[0] >= half]))
        if VERBOSE:
            print(f"=== {name}: {len(sig)} raw signals (own SL/TP) ===")
            print(f"  all      {bt.stats(pn(tr))}")
            print(f"  1st half {bt.stats(pn([r for r in tr if r[0] < half]))}")
            print(f"  2nd half {bt.stats(pn([r for r in tr if r[0] >= half]))}\n")
        return row
    for sl, tp in COMBOS:
        tr = bt.trades_for(c, sig, sl, tp)
        row[(sl, tp)] = (len(tr), sum(pn(tr)),
                         sum(pn([r for r in tr if r[0] < half])),
                         sum(pn([r for r in tr if r[0] >= half])))
    if VERBOSE:
        print(f"=== {name}: {len(sig)} raw signals ===")
        if GRID:
            for sl, tp in ((7, 6), (8, 8), (10, 8), (10, 10), (12, 12), (15, 12), (15, 15), (20, 20)):
                bt.TIE = "sl"; g = bt.trades_for(c, sig, sl, tp)
                bt.TIE = "tp"; g2 = bt.trades_for(c, sig, sl, tp)
                bt.TIE = "sl"
                print(f"  grid SL{sl}/TP{tp:<2} {bt.stats(pn(g))} | TP-first net={sum(pn(g2)):+.0f}")
        tr = bt.trades_for(c, sig, 7, 6)
        rb = random_baseline(c, max(len(tr), 30), 7, 6)
        print(f"  random entries SL7/TP6 exp={rb:+.2f}/trade  (benchmark to beat)")
        for combo in DETAIL:
            sl, tp = (float(x) for x in combo.split("x"))
            g = bt.trades_for(c, sig, sl, tp)
            print(f"  --- SL{sl:g}/TP{tp:g} split ---")
            print(f"  1st half {bt.stats(pn([r for r in g if r[0] < half]))}")
            print(f"  2nd half {bt.stats(pn([r for r in g if r[0] >= half]))}")
            months = {}
            for r in g:
                m = month_of(c, r[0])
                if m:
                    months.setdefault(m, []).append(r[3])
            for m in sorted(months):
                print(f"  {m}    {bt.stats(months[m])}")
        print()
    return row


def main():
    c = bt.load()
    print(f"{len(c)} candles, spread {bt.SPREAD} pt, one open trade per strategy\n")
    names = [s.strip() for s in os.environ.get("STRATS", ALL_NAMES).split(",") if s.strip()]
    rows, vp_cache = [], None
    for name in names:
        native = name in VP_FUNCS
        if native:
            if vp_cache is None:
                vp_cache = vp_all_signals(c)
            sig = vp_cache[name]
        elif name in AVAILABLE:
            sig = signals(c, AVAILABLE[name], name)
        else:
            print(f"unknown strategy {name} (OB Engine 5m is not supported)\n"); continue
        if SESSION:
            lo, hi = (int(x) for x in SESSION.split("-"))
            sig = [s for s in sig if c[s[0]].get("open_time") and
                   lo <= datetime.fromtimestamp(c[s[0]]["open_time"] / 1000, timezone.utc).hour < hi]
        rows.append(analyze(name, sig, c, native))

    print(f"=== SUMMARY: net points, spread {bt.SPREAD} (H1|H2 = 1st|2nd half of period) ===")
    print("strategy        n(7/6)   7/6   8/8  10/8 10/10  10/8 H1|H2")
    for r in rows:
        if r["native"]:
            continue
        a, b, d, e = r[(7, 6)], r[(8, 8)], r[(10, 8)], r[(10, 10)]
        print(f"{r['name'][:14]:14s} {a[0]:6d} {a[1]:+5.0f} {b[1]:+5.0f} {d[1]:+5.0f} {e[1]:+5.0f}  {d[2]:+5.0f}|{d[3]:+5.0f}")
    vp = [r for r in rows if r["native"]]
    if vp:
        print("\nVP strategies (own SL/TP):  n   net   H1|H2")
        for r in vp:
            print(f"{r['name'][:14]:14s} {r['n']:6d} {r['net']:+6.0f}  {r['h1']:+5.0f}|{r['h2']:+5.0f}")
    if _errors:
        print("\nerrors:", _errors)


if __name__ == "__main__":
    main()
