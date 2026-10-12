"""
Why did (or didn't) MSB-OB fire around a given time?  Uses the bot's own candle feed and the bot's own
msbob_compute(), replaying what the bot saw at each candle close.

    python diagnose_msb.py                       (default: 2026-10-12 00:10 UTC = 05:40 IST)
    python diagnose_msb.py --at "2026-10-12 00:10" --before 4 --after 4

The bot's feed only keeps about 5 days of 5m candles, so run it soon after the event.
For every candle it prints the swing lines the bot used and which condition failed, then lists the MSB-OB
trades and the dedup / cooldown state that could have blocked a signal.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import backtest_ha as bt
import main as bot

IST = timedelta(hours=5, minutes=30)


def fmt_t(ms):
    t = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f"{t:%H:%M} UTC / {(t + IST):%H:%M} IST"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="2026-10-12 00:10", help="candle OPEN time, UTC")
    ap.add_argument("--before", type=int, default=4)
    ap.add_argument("--after", type=int, default=4)
    a = ap.parse_args()

    candles, source = bot.get_klines()
    if not candles:
        sys.exit("Could not load candles from the bot's feed.")
    target = int(datetime.strptime(a.at, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)
    idx = next((i for i, c in enumerate(candles) if c["open_time"] >= target), None)
    if idx is None:
        sys.exit(f"Target time not in feed (feed has {fmt_t(candles[0]['open_time'])} to {fmt_t(candles[-1]['open_time'])}).")

    out = [f"MSB-OB diagnosis | feed {source} | ZigZag {bot.MSBOB_ZIGZAG_LEN} | Fib {bot.MSBOB_FIB_FACTOR:g} | "
           f"feed covers {fmt_t(candles[0]['open_time'])} -> {fmt_t(candles[-1]['open_time'])}"]
    for k in range(max(idx - a.before, 0), min(idx + a.after + 1, len(candles))):
        pre = candles[:k + 1]
        dbg = {}
        sig = bot.msbob_compute(pre, debug=dbg)
        c = candles[k]
        head = (f"\n{fmt_t(c['open_time'])} | O {c['open']:.2f} H {c['high']:.2f} L {c['low']:.2f} C {c['close']:.2f}"
                + ("   <== target" if k == idx else ""))
        out.append(head)
        if not dbg:
            out.append("  no swing high AND swing low pivots yet")
            continue
        need_up = dbg["h_line"] + dbg["fib_dist"]
        need_dn = dbg["l_line"] - dbg["fib_dist"]
        out.append(f"  swing high line {dbg['h_line']:.2f} | swing low line {dbg['l_line']:.2f} | "
                   f"range {dbg['rng']:.2f} | fib distance {dbg['fib_dist']:.2f} | market state {'UP' if dbg['market'] == 1 else 'DOWN'}")
        out.append(f"  BUY needs: close>{dbg['h_line']:.2f} [{'yes' if dbg['close'] > dbg['h_line'] else 'NO'}], "
                   f"prev close<={dbg['h_line']:.2f} [{'yes' if dbg['prev_close'] <= dbg['h_line'] else 'NO'}], "
                   f"high>={need_up:.2f} [{'yes' if dbg['high'] >= need_up else 'NO'}], "
                   f"market not already UP [{'yes' if dbg['market'] != 1 else 'NO'}]")
        out.append(f"  SELL needs: close<{dbg['l_line']:.2f} [{'yes' if dbg['close'] < dbg['l_line'] else 'NO'}], "
                   f"prev close>={dbg['l_line']:.2f} [{'yes' if dbg['prev_close'] >= dbg['l_line'] else 'NO'}], "
                   f"low<={need_dn:.2f} [{'yes' if dbg['low'] <= need_dn else 'NO'}], "
                   f"market not already DOWN [{'yes' if dbg['market'] != -1 else 'NO'}]")
        if sig and sig["index"] == k:
            out.append(f"  => ENGINE SIGNAL on this candle: {sig['direction'].upper()} (level {sig['level']:.2f})")
        else:
            out.append("  => no engine signal on this candle")

    # things that can block a signal even when the engine sees one
    trades = [t for t in bot.load_trades() if t.get("strategy") == "MSB-OB"]
    near = [t for t in trades if t.get("status") == "open" or str(t.get("opened_at", t.get("time", "")))[:10] == a.at[:10]]
    out.append("\nMSB-OB trades that day or still open (an open MSB-OB trade blocks new MSB-OB signals):")
    out += [f"  {t.get('direction')} entry {t.get('entry')} status {t.get('status')} {t.get('result') or ''} "
            f"opened {t.get('opened_at', t.get('time', '?'))}" for t in near] or ["  none"]
    state = bot.load_state()
    out.append(f"Last MSB-OB signal time: {state.get('last_signal_time', {}).get('MSB-OB', 'never')} | "
               f"direction: {state.get('last_signal_direction', {}).get('MSB-OB', '-')} | cooldown {bot.COOLDOWN_SECONDS}s")
    out.append("Note: the bot only accepts a break on the candle that just closed or the one before (10-minute window).")

    text = "\n".join(out)
    print(text)
    for i in range(0, len(text), 3800):
        bt.send_tg(text[i:i + 3800])


if __name__ == "__main__":
    main()
