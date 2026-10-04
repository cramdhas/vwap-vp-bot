#!/usr/bin/env python3
"""
Backtest + parameter finder for the SMA16/WMA15 strategy.

It imports your real main.py and calls the SAME check_sma16_wma15() the live
bot uses, so there is no second copy of the logic to drift out of sync.

Rules simulated (same as the bot):
  * signal on a COMPLETED M5 candle, entry = that candle's close
  * fixed SL 7 / TP 6, single target, no breakeven
  * one trade at a time (a new signal is ignored while a trade is open)
  * the bot's 1H bias filter (counter-trend signals blocked)
  * if SL and TP are both touched inside one candle -> counted as a LOSS
    (conservative, because the real order inside the candle is unknown)
  * spread/commission cost is subtracted from every trade (default 0.30)

To avoid fooling yourself, the data is split by time. Settings are RANKED on
the first part (train) and then CHECKED on the later, unseen part (test).
Only trust a setting that is profitable on BOTH.

Usage (run in the same folder as main.py):
  python backtest.py                    # last 30 days via TwelveData
  python backtest.py --days 60
  python backtest.py --csv gold_m5.csv  # your own MT5 / TradingView export
  python backtest.py --no-grid          # only test the current settings

Needs: pip install requests   (already used by the bot)
TwelveData needs the TWELVEDATA_API_KEY environment variable.
"""

import argparse
import csv
import importlib.util
import itertools
import os
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FAST_WINDOW = 80          # bars handed to the strategy (it needs ~54)
BIAS_WINDOW = 800         # bars handed to the 1H bias (needs >= 240)


# --------------------------------------------------------------------------
# load the real bot
# --------------------------------------------------------------------------
def load_bot(path):
    spec = importlib.util.spec_from_file_location("bot_main", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------
TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%m/%d/%Y %H:%M",
)


def _parse_time(text, tz_offset_h):
    text = text.strip()
    if text.replace(".", "", 1).isdigit():
        v = float(text)
        if v > 1e11:
            v /= 1000.0
        dt = datetime.fromtimestamp(v, tz=timezone.utc)
    else:
        dt = None
        for fmt in TIME_FORMATS:
            try:
                dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if dt is None:
            raise ValueError(f"unrecognised time format: {text!r}")
    return dt - timedelta(hours=tz_offset_h)


def load_csv(path, tz_offset_h=0.0):
    """Accepts MT5 exports (tab/semicolon/comma, <DATE> <TIME> headers) and
    generic CSVs with time/datetime + open/high/low/close[/volume]."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(f, dialect))

    if not rows:
        raise SystemExit("CSV is empty")

    head = [h.strip().strip("<>").lower() for h in rows[0]]

    def col(*names):
        for n in names:
            if n in head:
                return head.index(n)
        return None

    i_date = col("date")
    i_time = col("time", "datetime", "timestamp", "date_time", "open_time")
    i_o, i_h, i_l, i_c = col("open"), col("high"), col("low"), col("close")
    i_v = col("volume", "tick_volume", "tickvol", "vol")
    if None in (i_o, i_h, i_l, i_c) or (i_time is None and i_date is None):
        raise SystemExit(f"Cannot find columns in CSV header: {head}")

    candles = {}
    for r in rows[1:]:
        if len(r) < len(head):
            continue
        try:
            if i_date is not None and i_time is not None and i_date != i_time:
                stamp = f"{r[i_date].strip()} {r[i_time].strip()}"
            else:
                stamp = r[i_time if i_time is not None else i_date]
            dt = _parse_time(stamp, tz_offset_h)
            c = {
                "open_time": int(dt.timestamp() * 1000),
                "open": float(r[i_o]), "high": float(r[i_h]),
                "low": float(r[i_l]), "close": float(r[i_c]),
                "volume": float(r[i_v]) if i_v is not None and r[i_v] else 0.0,
            }
        except (ValueError, IndexError):
            continue
        if min(c["open"], c["high"], c["low"], c["close"]) > 0:
            candles[c["open_time"]] = c
    return [candles[k] for k in sorted(candles)]


def save_cache(path, candles):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for c in candles:
            t = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc)
            w.writerow([t.strftime("%Y-%m-%d %H:%M:%S"), c["open"], c["high"],
                        c["low"], c["close"], c["volume"]])


def fetch_twelvedata(days, api_key, requests_mod=None, sleep=8.0):
    """Pages backwards 5000 candles at a time (free tier: 8 calls/min)."""
    import requests as rq
    rq = requests_mod or rq
    target = int(days * 288 * 0.75)           # ~weekday share of 288 bars/day
    collected = {}
    end_date = None
    for call in range(1, 40):
        params = {"symbol": "XAU/USD", "interval": "5min", "outputsize": 5000,
                  "apikey": api_key, "timezone": "UTC", "order": "DESC"}
        if end_date:
            params["end_date"] = end_date
        r = rq.get("https://api.twelvedata.com/time_series", params=params, timeout=40)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "error":
            print(f"[TwelveData] stopped: {data.get('message')}")
            break
        vals = data.get("values") or []
        if not vals:
            break
        for v in vals:
            try:
                dt = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                collected[int(dt.timestamp() * 1000)] = {
                    "open_time": int(dt.timestamp() * 1000),
                    "open": float(v["open"]), "high": float(v["high"]),
                    "low": float(v["low"]), "close": float(v["close"]),
                    "volume": float(v.get("volume") or 0.0),
                }
            except (KeyError, ValueError):
                continue
        oldest = min(collected)
        print(f"[TwelveData] call {call}: {len(collected)} candles, "
              f"back to {datetime.fromtimestamp(oldest/1000, tz=timezone.utc):%Y-%m-%d %H:%M}")
        if len(collected) >= target or len(vals) < 5000:
            break
        end_date = (datetime.fromtimestamp(oldest / 1000, tz=timezone.utc)
                    - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(sleep)
    return [collected[k] for k in sorted(collected)]


def get_data(bot, args):
    if args.csv:
        candles = load_csv(args.csv, args.tz_offset)
        print(f"Loaded {len(candles)} candles from {args.csv}")
    else:
        candles = []
        if os.path.exists(args.cache) and not args.refresh:
            candles = load_csv(args.cache)
            age_h = (time.time() - os.path.getmtime(args.cache)) / 3600
            print(f"Loaded {len(candles)} cached candles ({age_h:.1f}h old) from {args.cache}")
        if not candles:
            key = os.environ.get("TWELVEDATA_API_KEY")
            if key:
                candles = fetch_twelvedata(args.days, key)
            if not candles:
                print("No TwelveData key/data - falling back to the bot's own feed "
                      "(only ~5 days, results will be weak).")
                candles, src = bot.get_klines(1400)
                candles = candles or []
            if candles:
                save_cache(args.cache, candles)
    if not candles:
        raise SystemExit("No candle data. Use --csv or set TWELVEDATA_API_KEY.")
    candles = bot.sanitize_candles(candles)
    # drop the newest candle (may be live/incomplete)
    return candles[:-1] if len(candles) > 1 else candles


# --------------------------------------------------------------------------
# simulation
# --------------------------------------------------------------------------
def simulate(bot, candles, sl, tp, use_bias=True, first_bar=None):
    """Walk forward bar by bar. Returns list of closed trades."""
    n = len(candles)
    start = first_bar if first_bar is not None else 60
    trades = []
    i = start
    while i < n - 1:
        window = candles[max(0, i - FAST_WINDOW + 1): i + 1]
        direction, _ = bot.check_sma16_wma15(window)
        if not direction:
            i += 1
            continue

        if use_bias:
            bias = bot.get_1h_bias(candles[max(0, i - BIAS_WINDOW + 1): i + 1])
            if (bias == "bullish" and direction == "short") or \
               (bias == "bearish" and direction == "long"):
                i += 1
                continue

        entry = candles[i]["close"]
        if direction == "long":
            sl_p, tp_p = entry - sl, entry + tp
        else:
            sl_p, tp_p = entry + sl, entry - tp

        result, exit_i = None, None
        for j in range(i + 1, n):
            c = candles[j]
            if direction == "long":
                hit_sl, hit_tp = c["low"] <= sl_p, c["high"] >= tp_p
            else:
                hit_sl, hit_tp = c["high"] >= sl_p, c["low"] <= tp_p
            if hit_sl:                      # SL wins ties (conservative)
                result, exit_i = "loss", j
                break
            if hit_tp:
                result, exit_i = "win", j
                break
        if result is None:                  # still open at end of data
            break

        trades.append({"i": i, "exit_i": exit_i, "dir": direction,
                       "entry": entry, "result": result,
                       "time": candles[i]["open_time"]})
        i = exit_i + 1                      # one trade at a time
    return trades


def metrics(trades, tp, sl, spread, days):
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "win%": 0.0, "net": 0.0, "pf": 0.0,
                "per_day": 0.0, "max_dd": 0.0, "max_loss_streak": 0}
    wins = sum(1 for t in trades if t["result"] == "win")
    pnl = [(tp - spread) if t["result"] == "win" else -(sl + spread) for t in trades]
    gross_win = sum(p for p in pnl if p > 0)
    gross_loss = -sum(p for p in pnl if p < 0)
    peak = run = dd = 0.0
    streak = best_streak = 0
    for t, p in zip(trades, pnl):
        run += p
        peak = max(peak, run)
        dd = max(dd, peak - run)
        streak = streak + 1 if t["result"] == "loss" else 0
        best_streak = max(best_streak, streak)
    return {"trades": n, "wins": wins, "win%": 100.0 * wins / n,
            "net": sum(pnl), "pf": (gross_win / gross_loss) if gross_loss else float("inf"),
            "per_day": n / max(days, 1e-9), "max_dd": dd, "max_loss_streak": best_streak}


def span_days(candles, lo, hi):
    return max((candles[hi]["open_time"] - candles[lo]["open_time"]) / 86400000.0, 1e-9)


def split_trades(trades, split_bar):
    return ([t for t in trades if t["i"] < split_bar],
            [t for t in trades if t["i"] >= split_bar])


def fmt(m):
    pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
    return (f"{m['trades']:>4} tr | win {m['win%']:5.1f}% | net {m['net']:+7.1f} pts | "
            f"PF {pf:>4} | maxDD {m['max_dd']:5.1f} | lossrun {m['max_loss_streak']}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def parse_list(text, cast=float):
    return [cast(x) for x in text.split(",") if x.strip()]


def main():
    ap = argparse.ArgumentParser(description="SMA16/WMA15 backtest + parameter finder")
    ap.add_argument("--bot", default=os.path.join(HERE, "main.py"))
    ap.add_argument("--csv", help="your own M5 candle export (MT5 / TradingView)")
    ap.add_argument("--tz-offset", type=float, default=0.0,
                    help="hours to SUBTRACT from CSV times to get UTC (e.g. 3 for MT5 GMT+3)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--cache", default=os.path.join(HERE, "gold_m5_cache.csv"))
    ap.add_argument("--refresh", action="store_true", help="re-download instead of using the cache")
    ap.add_argument("--sl", type=float, default=7.0)
    ap.add_argument("--tp", type=float, default=6.0)
    ap.add_argument("--spread", type=float, default=0.30, help="cost per trade in points")
    ap.add_argument("--no-bias", action="store_true", help="ignore the bot's 1H bias filter")
    ap.add_argument("--no-grid", action="store_true", help="only test the current settings")
    ap.add_argument("--gap", default="0.2,0.3,0.5,0.8")
    ap.add_argument("--slope", default="0.05,0.10,0.15,0.20")
    ap.add_argument("--chop", default="6,9,12,18")
    ap.add_argument("--split", type=float, default=0.70, help="share of data used for ranking")
    ap.add_argument("--min-trades", type=int, default=15, help="min TRAIN trades to rank a setting")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--out", default=os.path.join(HERE, "backtest_results.csv"))
    args = ap.parse_args()

    bot = load_bot(args.bot)
    candles = get_data(bot, args)
    n = len(candles)
    warm = 60
    if n < 400:
        raise SystemExit(f"Only {n} candles - too few for a meaningful test.")

    split_bar = warm + int((n - warm) * args.split)
    d_all = span_days(candles, warm, n - 1)
    d_train = span_days(candles, warm, split_bar)
    d_test = span_days(candles, split_bar, n - 1)
    t0 = datetime.fromtimestamp(candles[0]["open_time"] / 1000, tz=timezone.utc)
    t1 = datetime.fromtimestamp(candles[-1]["open_time"] / 1000, tz=timezone.utc)
    be = 100.0 * (args.sl + args.spread) / (args.sl + args.tp)
    print(f"\nData: {n} M5 candles, {t0:%Y-%m-%d} -> {t1:%Y-%m-%d} ({d_all:.1f} days)")
    print(f"SL {args.sl} / TP {args.tp} / spread {args.spread} | 1H bias filter: "
          f"{'off' if args.no_bias else 'on'}")
    print(f"Break-even win rate (after spread): {be:.1f}%   "
          f"train {d_train:.1f}d / test {d_test:.1f}d")

    use_bias = not args.no_bias
    cur = {"SMA16_MIN_GAP": bot.SMA16_MIN_GAP, "SMA16_MIN_SLOPE": bot.SMA16_MIN_SLOPE,
           "SMA16_CHOP_LOOKBACK": bot.SMA16_CHOP_LOOKBACK}

    def run(gap, slope, chop):
        bot.SMA16_MIN_GAP, bot.SMA16_MIN_SLOPE, bot.SMA16_CHOP_LOOKBACK = gap, slope, int(chop)
        tr = simulate(bot, candles, args.sl, args.tp, use_bias, first_bar=warm)
        a, b = split_trades(tr, split_bar)
        return (metrics(tr, args.tp, args.sl, args.spread, d_all),
                metrics(a, args.tp, args.sl, args.spread, d_train),
                metrics(b, args.tp, args.sl, args.spread, d_test), tr)

    print("\n=== CURRENT bot settings "
          f"(gap {cur['SMA16_MIN_GAP']}, slope {cur['SMA16_MIN_SLOPE']}, chop {cur['SMA16_CHOP_LOOKBACK']}) ===")
    all_m, tr_m, te_m, _ = run(cur["SMA16_MIN_GAP"], cur["SMA16_MIN_SLOPE"], cur["SMA16_CHOP_LOOKBACK"])
    print("all  :", fmt(all_m))
    print("train:", fmt(tr_m))
    print("test :", fmt(te_m))
    if all_m["trades"] < 30:
        print(f"NOTE: only {all_m['trades']} trades - far too few to judge. "
              "Use --days 60+ or a bigger CSV before trusting any number.")

    if args.no_grid:
        return

    combos = list(itertools.product(parse_list(args.gap), parse_list(args.slope),
                                    parse_list(args.chop, int)))
    print(f"\nTesting {len(combos)} combinations ...")
    results = []
    t_start = time.time()
    for k, (g, s, c) in enumerate(combos, 1):
        all_m, tr_m, te_m, tr = run(g, s, c)
        results.append({"gap": g, "slope": s, "chop": c, "all": all_m,
                        "train": tr_m, "test": te_m})
        if k % 8 == 0 or k == len(combos):
            el = time.time() - t_start
            print(f"  {k}/{len(combos)} done ({el:.0f}s)", flush=True)

    bot.SMA16_MIN_GAP, bot.SMA16_MIN_SLOPE, bot.SMA16_CHOP_LOOKBACK = (
        cur["SMA16_MIN_GAP"], cur["SMA16_MIN_SLOPE"], cur["SMA16_CHOP_LOOKBACK"])

    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["gap", "slope", "chop", "split", "trades", "win%", "net_pts",
                    "profit_factor", "max_dd", "max_loss_streak"])
        for r in results:
            for name in ("all", "train", "test"):
                m = r[name]
                w.writerow([r["gap"], r["slope"], r["chop"], name, m["trades"],
                            round(m["win%"], 1), round(m["net"], 1),
                            "inf" if m["pf"] == float("inf") else round(m["pf"], 2),
                            round(m["max_dd"], 1), m["max_loss_streak"]])

    ranked = sorted((r for r in results if r["train"]["trades"] >= args.min_trades),
                    key=lambda r: r["train"]["net"], reverse=True)
    print(f"\n=== TOP {args.top} by TRAIN net points (min {args.min_trades} train trades) ===")
    if not ranked:
        print("No setting reached the minimum trade count. Use more days or lower --min-trades.")
        return
    for r in ranked[:args.top]:
        enough = r["test"]["trades"] >= max(5, args.min_trades // 3)
        if r["train"]["net"] > 0 and r["test"]["net"] > 0 and enough:
            ok = "PASS"
        elif not enough:
            ok = "too few test trades"
        else:
            ok = "fail"
        print(f"gap {r['gap']:<4} slope {r['slope']:<4} chop {r['chop']:<3} [{ok}]")
        print("   train:", fmt(r["train"]))
        print("   test :", fmt(r["test"]))

    robust = [r for r in ranked if r["test"]["trades"] >= max(5, args.min_trades // 3)
              and r["test"]["net"] > 0 and r["train"]["net"] > 0]
    print("\n=== VERDICT ===")
    if robust:
        b = max(robust, key=lambda r: r["train"]["net"] + r["test"]["net"])
        print("Best setting that is profitable on BOTH train and test:")
        print(f"  SMA16_MIN_GAP = {b['gap']}")
        print(f"  SMA16_MIN_SLOPE = {b['slope']}")
        print(f"  SMA16_CHOP_LOOKBACK = {b['chop']}")
        print("  train:", fmt(b["train"]))
        print("  test :", fmt(b["test"]))
        if b["train"]["trades"] + b["test"]["trades"] < 40:
            print("  WARNING: fewer than 40 trades in total - treat this as a hint, not proof.")
    else:
        print("No setting was profitable on both train and test after spread.")
        print("Do not trade this strategy live with SL 7 / TP 6 on this data; try --days 60,")
        print("a different SL/TP (--sl / --tp) or a longer history before changing the bot.")
    print(f"\nFull table saved to {args.out}")


if __name__ == "__main__":
    main()
