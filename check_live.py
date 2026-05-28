"""
check_live.py — Live strategy scanner with full terminal breakdown
==================================================================
Run standalone:
    python check_live.py

Or import from main.py — add these 2 lines anywhere in run_once():
    import check_live
    check_live.scan_once()
"""

import sys
import time
from datetime import datetime, timezone

# Windows UTF-8 fix
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from config import ALL_INSTRUMENTS, GOLD_PAIR, PIP_SIZE, TP1_RR, TP2_RR
    from data_fetcher import fetch_mtf_data, fetch_account_summary, fetch_current_price
    from strategies import analyze_all_strategies
    from logger import init_db
except ImportError as e:
    print(f"\n[ERROR] Import failed: {e}")
    print("Make sure you are running from the gold_testing folder with venv active.\n")
    sys.exit(1)

init_db()

# Keys must match the sid strings in strategies.py exactly
STRATEGY_LABELS = {
    "S1_ASIAN_RANGE_SWEEP":       "S1  Asian Range Sweep  ",
    "S2_NY_OPEN_KILLSHOT":        "S2  NY Open Killshot   ",
    "S3_OB_PSYCHOLOGICAL_LEVELS": "S3  OB + Psych Levels  ",
    "S4_WEEKLY_PROFILE":          "S4  Weekly Profile     ",
    "S5_FVG_RETRACEMENT":         "S5  FVG Retracement    ",
    "S6_POWER_OF_3":              "S6  Power of 3         ",
    "S7_SILVER_BULLET":           "S7  Silver Bullet      ",
    "S_FOREX_LQ_SWEEP":           "LQ  Forex LQ Sweep     ",
}


def get_label(strategy_id: str) -> str:
    sid = strategy_id or ""
    if sid in STRATEGY_LABELS:
        return STRATEGY_LABELS[sid]
    # fuzzy fallback
    sid_lower = sid.lower()
    for key, label in STRATEGY_LABELS.items():
        if key.lower() in sid_lower or sid_lower in key.lower():
            return label
    return f"{sid:<23}"


def _sep(char="-", width=68):
    print(char * width, flush=True)


def scan_once():
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%d  %H:%M:%S UTC")

    _sep("=")
    print(f"  ICT/SMC LIVE STRATEGY SCAN   {now_str}", flush=True)
    _sep("=")

    try:
        acct = fetch_account_summary()
        if acct:
            print(
                f"  Balance: ${acct['balance']:,.2f}  |  "
                f"NAV: ${acct['nav']:,.2f}  |  "
                f"Open trades: {acct['open_trade_count']}",
                flush=True,
            )
    except Exception:
        print("  (Account data unavailable)", flush=True)
    print(flush=True)

    context = {}
    any_signal = False

    for instrument in ALL_INSTRUMENTS:
        _sep()
        print(f"  {instrument}", flush=True)
        _sep()

        try:
            price = fetch_current_price(instrument)
            if price:
                print(
                    f"  Bid: {price['bid']}  Ask: {price['ask']}  "
                    f"Spread: {price['spread_pips']} pips",
                    flush=True,
                )
        except Exception:
            pass

        try:
            mtf_data = fetch_mtf_data(instrument)
        except Exception as e:
            print(f"  [ERROR] MTF fetch failed: {e}", flush=True)
            continue

        if not mtf_data.get("Daily") or not mtf_data.get("1H"):
            print("  [SKIP] Insufficient MTF data", flush=True)
            continue

        last_1h = mtf_data.get("1H", [{}])[-1]
        print(
            f"  1H Candle -- O:{last_1h.get('open')}  "
            f"H:{last_1h.get('high')}  "
            f"L:{last_1h.get('low')}  "
            f"C:{last_1h.get('close')}",
            flush=True,
        )
        print(flush=True)

        try:
            signal = analyze_all_strategies(instrument, mtf_data, context)
        except Exception as e:
            print(f"  [ERROR] Strategy analysis failed: {e}", flush=True)
            continue

        individual = signal.get("_all_results", [])

        if not individual:
            # strategies.py missing _all_results — show master result only
            sig = signal.get("signal", "NO_TRADE")
            if sig != "NO_TRADE":
                print(
                    f"  [{sig}]  score={signal.get('score')}/10  "
                    f"strategy={signal.get('strategy_id')}",
                    flush=True,
                )
                print(
                    f"    entry={signal.get('entry')}  "
                    f"SL={signal.get('stop_loss')}  "
                    f"TP1={signal.get('tp1')}  TP2={signal.get('tp2')}",
                    flush=True,
                )
            else:
                print(f"  [NO SIGNAL] {signal.get('reason','')[:80]}", flush=True)
        else:
            for r in individual:
                sid     = r.get("strategy_id", "")
                label   = get_label(sid)
                sig_val = r.get("signal", "NO_TRADE")
                score   = r.get("score", 0)
                reason  = (r.get("reason") or "")[:52]

                if sig_val == "LONG":
                    tag    = "[LONG] "
                    detail = (
                        f"score={score}/10  "
                        f"entry={r.get('entry')}  "
                        f"SL={r.get('stop_loss')}  "
                        f"TP1={r.get('tp1')}"
                    )
                elif sig_val == "SHORT":
                    tag    = "[SHORT]"
                    detail = (
                        f"score={score}/10  "
                        f"entry={r.get('entry')}  "
                        f"SL={r.get('stop_loss')}  "
                        f"TP1={r.get('tp1')}"
                    )
                else:
                    tag    = "[ --- ]"
                    detail = reason

                print(f"  {label} | {tag} | {detail}", flush=True)

            print(flush=True)
            master_sig = signal.get("signal", "NO_TRADE")
            if master_sig != "NO_TRADE":
                any_signal = True
                arrow = "^ LONG " if master_sig == "LONG" else "v SHORT"
                print(
                    f"  ** BEST >> {arrow}  score={signal['score']}/10  "
                    f"strategy={signal.get('strategy_id')}",
                    flush=True,
                )
                print(
                    f"             entry={signal.get('entry')}  "
                    f"SL={signal.get('stop_loss')}  "
                    f"TP1={signal.get('tp1')}  TP2={signal.get('tp2')}",
                    flush=True,
                )
            else:
                top = (signal.get("reason") or "")[:80]
                top_lines = top.split(";")[:3]
                print(f"  X NO SIGNAL", flush=True)
                for ln in top_lines:
                    print(f"    {ln.strip()}", flush=True)

        print(flush=True)

    _sep("=")
    if any_signal:
        print("  ** Signals detected above — live bot will evaluate for execution", flush=True)
    else:
        print("  No signals this scan -- normal outside London/NY kill zones", flush=True)
    print("  Next scan in 60s  |  Press Ctrl+C to stop", flush=True)
    _sep("=")
    print(flush=True)


if __name__ == "__main__":
    print("\nStarting ICT/SMC Live Strategy Scanner...", flush=True)
    print("Press Ctrl+C to stop\n", flush=True)

    cycle = 0
    while True:
        cycle += 1
        print(f"\n  [ Cycle {cycle} ]", flush=True)
        try:
            scan_once()
        except KeyboardInterrupt:
            print("\nScanner stopped.")
            break
        except Exception as e:
            print(f"\n[ERROR] Cycle failed: {e}", flush=True)

        try:
            time.sleep(60)
        except KeyboardInterrupt:
            print("\nScanner stopped.")
            break
