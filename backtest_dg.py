"""Backtest the Dynamic Grid strategy from main.py on XAUUSD M5.

Usage:
  python backtest_dg.py                 # fetches ~5000 M5 bars from TwelveData
  python backtest_dg.py data.csv        # CSV with columns: time,open,high,low,close
  SPREAD=0.3 python backtest_dg.py      # cost per trade in points (default 0.3)

Rules: entry = close of the signal candle. SL/TP checked on later candles.
If SL and TP are both inside one candle it counts as a LOSS (same as the bot).
"""
import csv, os, sys, time
from datetime import datetime, timezone, timedelta
import requests
import main as bot

SPREAD = float(os.environ.get("SPREAD", "0.3"))
MONTHS = float(os.environ.get("MONTHS", "3"))


def fetch_history(months=MONTHS):
    """Page back through TwelveData (5000 bars per request) until `months` of M5."""
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        raise RuntimeError("TWELVEDATA_API_KEY not set")
    cutoff = datetime.now(timezone.utc) - timedelta(days=30.4 * months)
    bucket = int(datetime.now(timezone.utc).timestamp() // 300) * 300
    seen, end = {}, None
    for page in range(12):
        params = {"symbol": "XAU/USD", "interval": "5min", "outputsize": 5000,
                  "apikey": key, "timezone": "UTC", "order": "DESC"}
        if end:
            params["end_date"] = end
        for attempt in range(3):
            data = requests.get("https://api.twelvedata.com/time_series",
                                params=params, timeout=30).json()
            if data.get("status") == "error" and "limit" in str(data.get("message", "")).lower():
                print("rate limit, waiting 65s"); time.sleep(65); continue
            break
        if data.get("status") == "error":
            if seen:
                print("stopping early:", data.get("message")); break
            raise RuntimeError(data.get("message"))
        vals = data.get("values") or []
        new = 0
        for v in vals:
            if v["datetime"] not in seen:
                seen[v["datetime"]] = v; new += 1
        if not vals or not new:
            break
        oldest = min(v["datetime"] for v in vals)
        print(f"page {page+1}: {len(vals)} bars back to {oldest}")
        if datetime.strptime(oldest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) <= cutoff:
            break
        end = oldest
        time.sleep(2)
    out = []
    for dt, v in sorted(seen.items()):
        t = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if t < cutoff or int(t.timestamp()) >= bucket:
            continue
        out.append({"open_time": int(t.timestamp() * 1000), "open": float(v["open"]),
                    "high": float(v["high"]), "low": float(v["low"]),
                    "close": float(v["close"]), "volume": float(v.get("volume") or 0)})
    return bot.sanitize_candles(market_open_only(out))


def load():
    if len(sys.argv) > 1:
        out = []
        with open(sys.argv[1]) as f:
            for r in csv.DictReader(f):
                out.append({k: float(r[k]) for k in ("open", "high", "low", "close")})
        return out
    return fetch_history()


def simulate_ex(c, i, side, sl, tp):
    """Returns (pnl, exit_index); (None, None) if still open at end of data."""
    e = c[i]["close"]
    for j in range(i + 1, len(c)):
        hi, lo = c[j]["high"], c[j]["low"]
        if side == "long":
            hit_sl, hit_tp = lo <= e - sl, hi >= e + tp
        else:
            hit_sl, hit_tp = hi >= e + sl, lo <= e - tp
        if hit_sl:
            return -sl - SPREAD, j       # SL first / both = loss
        if hit_tp:
            return tp - SPREAD, j
    return None, None


def simulate(c, i, side, sl, tp):
    return simulate_ex(c, i, side, sl, tp)[0]


def trades_for(c, sigs, sl, tp):
    """Live rule: only ONE open trade per strategy at a time (can_send in main.py).
    sigs = [(i, side, ...)]. Returns [(i, side, extra, pnl)] in order."""
    out, busy_until = [], -1
    for sg in sorted(sigs, key=lambda x: x[0]):
        i, side = sg[0], sg[1]
        if i <= busy_until:
            continue
        pnl, j = simulate_ex(c, i, side, sl, tp)
        if pnl is None:
            break
        out.append((i, side, sg, pnl))
        busy_until = j
    return out


def market_open_only(c):
    """Drop weekend / closed-market bars using the bot's own Octa hours rule."""
    if not c or "open_time" not in c[0]:
        return c
    keep = [x for x in c if bot.is_octa_xauusd_open(
        datetime.fromtimestamp(x["open_time"] / 1000, timezone.utc))]
    print(f"market-hours filter: kept {len(keep)} of {len(c)} candles")
    return keep


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
    t0 = c[0].get("open_time"); t1 = c[-1].get("open_time")
    rng = ""
    if t0 and t1:
        rng = f" ({datetime.fromtimestamp(t0/1000, timezone.utc):%Y-%m-%d} to {datetime.fromtimestamp(t1/1000, timezone.utc):%Y-%m-%d})"
    print(f"{len(c)} candles{rng}, spread {SPREAD} pt, one open trade at a time\n")
    half = len(c) // 2
    for mode in ("trend", "reversion", "both"):
        bot.DG_MODE = mode
        g = bot.dynamic_grid_compute(c)
        sigs = [(i, "long" if g["signal"][i] == "long" else "short", g["level"][i])
                for i in range(len(c)) if g["signal"][i]]
        print(f"=== mode={mode}: {len(sigs)} raw signals ===")
        tr = trades_for(c, sigs, 7, 6)
        pn = lambda rows: [r[3] for r in rows]
        print(f"  SL7/TP6  all     {stats(pn(tr))}")
        print(f"  SL7/TP7  all     {stats(pn(trades_for(c, sigs, 7, 7)))}")
        for lv in range(1, bot.DG_NUM_LEVELS + 1):
            sub = [r for r in tr if r[2][2] == lv]
            if sub:
                print(f"  SL7/TP6  deg {lv}   {stats(pn(sub))}")
        print(f"  SL7/TP6  1st half {stats(pn([r for r in tr if r[0] < half]))}")
        print(f"  SL7/TP6  2nd half {stats(pn([r for r in tr if r[0] >= half]))}")
        months = {}
        for r in tr:
            t = c[r[0]].get("open_time")
            if t:
                months.setdefault(datetime.fromtimestamp(t/1000, timezone.utc).strftime("%Y-%m"), []).append(r[3])
        for m in sorted(months):
            print(f"  SL7/TP6  {m}    {stats(months[m])}")
        base = []
        for i, side, _ in sigs:
            d = abs(g["hma"][i] - c[i]["close"])
            base.append((i, side, None))
        trb, busy = [], -1
        for i, side, _ in base:
            if i <= busy:
                continue
            d = abs(g["hma"][i] - c[i]["close"])
            pnl, j = simulate_ex(c, i, side, 7, max(3.0, min(12.0, d)))
            if pnl is None:
                break
            trb.append(pnl); busy = j
        print(f"  SL7/baseline-TP {stats(trb)}\n")


if __name__ == "__main__":
    main()
