"""
main.py — ICT/SMC Gold Trading Bot — Main loop
================================================
Production-grade automated trading bot combining all 7 ICT/SMC strategies.

Flow per cycle:
1. Validate prerequisites (credentials, circuit breaker)
2. Fetch account state + open trades
3. Manage active trade lifecycle (TP1/TP2/BE)
4. For each instrument: fetch MTF data → run all strategies → rank signals
5. Select best signal (highest score, respect gold priority)
6. Risk validate
7. Check spread
8. Optional LLM confirmation
9. Execute trade
10. Log + notify
11. Sleep until next candle close
"""

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
import check_live

# Force UTF-8 output on Windows (fixes Unicode crash in PowerShell)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config import (
    OANDA_API_KEY, OANDA_ACCOUNT_ID, ALL_INSTRUMENTS, GOLD_PAIR,
    CHECK_INTERVAL_SECONDS, KILL_ZONES, REQUIRE_KILL_ZONE,
    LOG_DIR, TP1_RR, TP2_RR,
)
from data_fetcher import (
    fetch_account_summary, fetch_open_trades,
    fetch_mtf_data, is_spread_acceptable,
)
from trade_executor import calculate_position_size, execute_market_order
from risk_manager import RiskManager
from trade_manager import TradeManager
from strategies import analyze_all_strategies
from llm_confirmation import confirm_with_llm
from notifier import (
    notify_startup, notify_shutdown, notify_new_signal,
    notify_trade_entry, notify_error, notify_daily_summary,
)
from logger import setup_logging, init_db, log_trade_open, log_signal, log_event

logger = logging.getLogger(__name__)

# ─── Globals ──────────────────────────────────────────────────────────────
_RUNNING   = True
_VERSION   = "1.0.0"


def _signal_handler(signum, frame):
    global _RUNNING
    logger.info("Shutdown signal received — stopping bot gracefully")
    _RUNNING = False


def _is_in_kill_zone() -> bool:
    """Returns True if current UTC hour is within any configured kill zone."""
    now_utc = datetime.now(timezone.utc)
    h, m    = now_utc.hour, now_utc.minute
    curr    = h * 60 + m
    for kz in KILL_ZONES:
        if kz["start"] * 60 <= curr < kz["end"] * 60:
            return True
    return False


def _get_session_name() -> str:
    h = datetime.now(timezone.utc).hour
    if 0  <= h < 7:  return "Asian"
    if 7  <= h < 12: return "London"
    if 12 <= h < 20: return "NY"
    return "Off-session"


def _rank_signals(signals: list) -> list:
    """
    Sort signals by score descending, gold first on tie.
    Returns sorted list.
    """
    def key(s):
        is_gold = 1 if s["pair"] == GOLD_PAIR else 0
        return (s["score"], is_gold)
    return sorted(signals, key=key, reverse=True)


def _validate_credentials() -> bool:
    if not OANDA_API_KEY:
        logger.critical("OANDA_API_KEY not set. Check your .env file.")
        return False
    if not OANDA_ACCOUNT_ID:
        logger.critical("OANDA_ACCOUNT_ID not set. Check your .env file.")
        return False
    return True


def run_once(risk_mgr: RiskManager, trade_mgr: TradeManager) -> int:
    """
    Single bot cycle. Returns number of trades opened.
    """
    # ── 1. Kill zone filter ──────────────────────────────────────
    if REQUIRE_KILL_ZONE and not _is_in_kill_zone():
        logger.debug("Outside kill zones — skipping cycle")
        return 0

    # ── 2. Circuit breaker ───────────────────────────────────────
    if risk_mgr.is_circuit_breaker_active():
        logger.warning("Circuit breaker active — no trades today")
        return 0

    # ── 3. Account state ─────────────────────────────────────────
    account = fetch_account_summary()
    if not account:
        logger.error("Failed to fetch account summary")
        return 0

    balance     = account["balance"]
    open_trades = fetch_open_trades()

    logger.info(
        "Cycle | Balance=%.2f | Open=%d | Session=%s",
        balance, len(open_trades), _get_session_name()
    )

    # ── 4. Manage active trades ───────────────────────────────────
    trade_mgr.manage_all()

    # ── 5. Collect signals from all instruments ───────────────────
    context     = risk_mgr.get_sb_context()
    all_signals = []

    _STRATEGY_LABELS = {
        "analyze_s1_asian_range_sweep": "S1  Asian Range Sweep  ",
        "analyze_s2_ny_open_killshot":  "S2  NY Open Killshot   ",
        "analyze_s3_ob_psychological":  "S3  OB + Psych Levels  ",
        "analyze_s4_weekly_profile":    "S4  Weekly Profile     ",
        "analyze_s5_fvg_retracement":   "S5  FVG Retracement    ",
        "analyze_s6_power_of_3":        "S6  Power of 3         ",
        "analyze_s7_silver_bullet":     "S7  Silver Bullet      ",
        "analyze_forex_lq_sweep":       "LQ  Forex LQ Sweep     ",
    }

    for instrument in ALL_INSTRUMENTS:
        try:
            mtf_data = fetch_mtf_data(instrument)
            if not mtf_data.get("Daily") or not mtf_data.get("1H"):
                logger.warning("Skipping %s — no MTF data", instrument)
                continue

            signal = analyze_all_strategies(instrument, mtf_data, context)
            signal["pair"]     = instrument
            signal["mtf_data"] = mtf_data

            # ── Per-instrument strategy breakdown ─────────────────
            now_str  = datetime.now(timezone.utc).strftime("%H:%M UTC")
            session  = _get_session_name()
            price_1h = mtf_data.get("1H", [{}])[-1]
            price_str = f"H:{price_1h.get('high', '?')}  L:{price_1h.get('low', '?')}  C:{price_1h.get('close', '?')}"

            print(f"\n" + "-"*68, flush=True)
            print(f"  {instrument:<10}  {now_str}  [{session}]  {price_str}", flush=True)
            print("-"*68, flush=True)

            individual = signal.get("_all_results", [])
            for r in individual:
                sid     = r.get("strategy_id", "")
                label   = sid
                for fn_name, lbl in _STRATEGY_LABELS.items():
                    key = fn_name.replace("analyze_", "").replace("_", " ").upper()
                    if key in sid.upper() or sid.upper() in lbl.upper():
                        label = lbl
                        break

                sig_val = r.get("signal", "NO_TRADE")
                score   = r.get("score", 0)
                reason  = (r.get("reason") or "")[:52]

                if sig_val == "LONG":
                    tag    = "[LONG] "
                    detail = f"score={score}/10  entry={r.get('entry')}  SL={r.get('stop_loss')}  TP1={r.get('tp1')}"
                elif sig_val == "SHORT":
                    tag    = "[SHORT]"
                    detail = f"score={score}/10  entry={r.get('entry')}  SL={r.get('stop_loss')}  TP1={r.get('tp1')}"
                else:
                    tag    = "[ --- ]"
                    detail = reason

                print(f"  {label} | {tag} | {detail}", flush=True)

            print("-"*68, flush=True)
            if signal.get("signal") != "NO_TRADE":
                sig_val = signal["signal"]
                arrow   = "^ LONG " if sig_val == "LONG" else "v SHORT"
                print(
                    f"  ** BEST | {arrow} score={signal['score']}/10"
                    f" | {signal.get('strategy_id','')}"
                    f"  entry={signal.get('entry')}"
                    f"  SL={signal.get('stop_loss')}"
                    f"  TP1={signal.get('tp1')}  TP2={signal.get('tp2')}",
                    flush=True
                )
                all_signals.append(signal)
            else:
                top_reason = (signal.get("reason") or "")[:70]
                print(f"  X NONE  | {top_reason}", flush=True)
            print(flush=True)

        except Exception as exc:
            logger.error("Strategy error for %s: %s", instrument, exc, exc_info=True)
            notify_error(f"Strategy loop {instrument}", str(exc))

    if not all_signals:
        print("\n  No actionable signals this cycle.\n", flush=True)
        return 0

    # ── 6. Rank & select best signal ──────────────────────────────
    ranked = _rank_signals(all_signals)
    best   = ranked[0]
    pair   = best["pair"]

    logger.info(
        "Best signal: %s %s score=%d strategy=%s",
        pair, best["signal"], best["score"], best.get("strategy_id")
    )

    # Log signal (pre-validation)
    log_signal(best, pair, taken=False, skip_reason="pending validation")

    # Notify signal detected
    notify_new_signal(best, pair)

    # ── 7. Spread check ───────────────────────────────────────────
    spread_ok, spread_pips = is_spread_acceptable(pair)
    if not spread_ok:
        skip = f"Spread too wide: {spread_pips} pips"
        logger.warning(skip)
        log_signal(best, pair, taken=False, skip_reason=skip)
        return 0

    # ── 8. Risk validation ────────────────────────────────────────
    allowed, risk_reason = risk_mgr.validate_trade(
        best, open_trades, balance, spread_pips
    )
    if not allowed:
        logger.warning("Trade blocked by risk manager: %s", risk_reason)
        log_signal(best, pair, taken=False, skip_reason=risk_reason)
        return 0

    # ── 9. Optional LLM confirmation ──────────────────────────────
    mtf_data = best.pop("mtf_data", {})
    approved, llm_confidence, llm_reason = confirm_with_llm(best, pair, mtf_data)
    if not approved:
        skip = f"LLM rejected (confidence={llm_confidence}/10): {llm_reason}"
        logger.warning(skip)
        log_signal(best, pair, taken=False, skip_reason=skip)
        return 0

    # ── 10. Calculate position size ───────────────────────────────
    units = calculate_position_size(pair, best["risk_pips"], balance)
    if units <= 0:
        logger.error("Position size 0 — skipping")
        return 0

    risk_dollars = risk_mgr.calculate_risk_dollars(balance)

    # ── 11. Execute ───────────────────────────────────────────────
    try:
        trade = execute_market_order(
            pair      = pair,
            signal    = best["signal"],
            units     = units,
            stop_loss = best["stop_loss"],
            take_profit = best["tp2"],   # TP2 set as main TP; TP1 managed manually
            comment   = f"{best.get('strategy_id', '')} s={best['score']}",
        )
    except Exception as exc:
        logger.error("Order execution error: %s", exc, exc_info=True)
        notify_error("Order execution", str(exc))
        return 0

    if not trade:
        print("\n  [FAILED] ORDER NOT OPENED\n", flush=True)
        logger.error("Order execution returned None — trade not opened")
        return 0

    trade_id = trade["trade_id"]
    direction_arrow = "^" if best["signal"] == "LONG" else "v"
    print("\n" + "="*68, flush=True)
    print(f"  [TRADE EXECUTED]  {direction_arrow} {best['signal']} {pair}", flush=True)
    print("="*68, flush=True)
    print(f"  Trade ID   : {trade_id}", flush=True)
    print(f"  Strategy   : {best.get('strategy_id')}", flush=True)
    print(f"  Score      : {best.get('score')}/10", flush=True)
    print(f"  Fill Price : {trade['fill_price']}", flush=True)
    print(f"  Units      : {units:,}", flush=True)
    print(f"  Stop Loss  : {best['stop_loss']}  ({best['risk_pips']} pips)", flush=True)
    print(f"  TP1        : {best['tp1']}  (1:{TP1_RR} RR - 50% close)", flush=True)
    print(f"  TP2        : {best['tp2']}  (1:{TP2_RR} RR - runner)", flush=True)
    print(f"  Risk $     : ${risk_dollars:,.2f}", flush=True)
    print("="*68 + "\n", flush=True)

    # ── 12. Post-execution bookkeeping ────────────────────────────
    open_time = datetime.now(timezone.utc).isoformat()
    session   = _get_session_name()

    risk_mgr.record_trade_open(pair)
    if "Silver Bullet" in best.get("strategy_id", ""):
        # find which SB window
        h = datetime.now(timezone.utc).hour
        if 3 <= h < 4:   risk_mgr.update_sb_window("Asian SB")
        elif 10 <= h < 11: risk_mgr.update_sb_window("London SB")
        elif 14 <= h < 15: risk_mgr.update_sb_window("NY SB")

    trade_mgr.register_trade(trade_id, pair, best, trade["fill_price"], units)

    log_trade_open(trade_id, pair, best, units, open_time, session, risk_dollars)
    log_signal(best, pair, taken=True)
    log_event("TRADE_OPENED", {
        "trade_id": trade_id, "pair": pair,
        "signal": best["signal"], "score": best["score"],
        "fill_price": trade["fill_price"],
        "llm_confidence": llm_confidence,
    })

    notify_trade_entry(trade, best, pair, units)

    logger.info(
        "Trade OPENED: %s %s %d units at %.5f | SL=%.5f TP2=%.5f",
        pair, best["signal"], units, trade["fill_price"],
        best["stop_loss"], best["tp2"]
    )

    return 1


def run_bot():
    """Main bot loop — runs until interrupted."""
    global _RUNNING

    # Setup
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    setup_logging("INFO")
    init_db()

    logger.info("=" * 60)
    logger.info("ICT/SMC Gold Bot v%s starting", _VERSION)
    logger.info("Instruments: %s", ", ".join(ALL_INSTRUMENTS))
    logger.info("=" * 60)

    if not _validate_credentials():
        logger.critical("Missing credentials — exiting")
        sys.exit(1)

    risk_mgr  = RiskManager()
    trade_mgr = TradeManager(risk_mgr)

    notify_startup(_VERSION)
    log_event("STARTUP", {"version": _VERSION, "instruments": ALL_INSTRUMENTS})

    cycle = 0
    while _RUNNING:
        cycle += 1
        logger.info("─── Cycle %d | %s UTC ───",
                    cycle, datetime.now(timezone.utc).strftime("%H:%M:%S"))
        try:
            run_once(risk_mgr, trade_mgr)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.error("Unhandled cycle error: %s", exc, exc_info=True)
            notify_error("Main loop", str(exc))

        if _RUNNING:
            check_live.scan_once()
            logger.debug("Sleeping %ds", CHECK_INTERVAL_SECONDS)
            time.sleep(CHECK_INTERVAL_SECONDS)

    # Shutdown
    logger.info("Bot shutting down cleanly")
    notify_shutdown("Clean shutdown")

    # Final daily summary
    summary = risk_mgr.get_daily_summary()
    notify_daily_summary(summary)
    log_event("SHUTDOWN", summary)


def run_backtest_cli(args):
    """Entry point for backtest mode."""
    from backtester import run_backtest
    setup_logging("INFO")
    init_db()

    instrument  = args.instrument or GOLD_PAIR
    from_year   = args.from_year or 2022
    to_year     = args.to_year   or 2024

    logger.info("Running backtest: %s %d→%d", instrument, from_year, to_year)
    results = run_backtest(instrument, from_year, to_year)

    if "error" in results:
        logger.error("Backtest failed: %s", results["error"])
        return

    print("\n" + "=" * 50)
    print(f"BACKTEST RESULTS: {instrument} {from_year}–{to_year}")
    print("=" * 50)
    print(f"Trades:        {results['total_trades']}")
    print(f"Win Rate:      {results['win_rate']}%")
    print(f"Net PnL:       ${results['net_pnl']:,.2f}")
    print(f"Profit Factor: {results['profit_factor']}")
    print(f"Sharpe Ratio:  {results['sharpe_ratio']}")
    print(f"Max Drawdown:  ${results['max_drawdown']:,.2f}")
    print(f"Final Balance: ${results['final_balance']:,.2f}")
    print(f"Return:        {results['return_pct']}%")

    mc = results.get("monte_carlo", {})
    if mc:
        print(f"\nMonte Carlo ({mc['simulations']} sims):")
        print(f"  P10 equity: ${mc['p10_final_equity']:,.2f}")
        print(f"  P50 equity: ${mc['p50_final_equity']:,.2f}")
        print(f"  P90 equity: ${mc['p90_final_equity']:,.2f}")
        print(f"  Worst DD (P90): ${mc['p90_max_drawdown']:,.2f}")

    if results.get("csv_path"):
        print(f"\nCSV exported: {results['csv_path']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ICT/SMC Gold Trading Bot")
    parser.add_argument("--mode",       choices=["live", "backtest"], default="live")
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--from-year",  type=int, dest="from_year", default=2022)
    parser.add_argument("--to-year",    type=int, dest="to_year",   default=2024)
    args = parser.parse_args()

    if args.mode == "backtest":
        run_backtest_cli(args)
    else:
        run_bot()
