"""
dashboard.py — Streamlit live trading dashboard
================================================
Displays:
- Live account metrics
- Open positions
- Active signals
- Equity curve
- Win rate / strategy breakdown
- Session performance
- Recent trade log
- Bot event log
"""

import os
import json
import time
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ── Page config ──────────────────────────────────────────────
st.set_page_config(
    page_title="ICT/SMC Trading Bot",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Import bot modules (graceful fallback if not configured) ──
try:
    from config import ALL_INSTRUMENTS, GOLD_PAIR, LOG_DIR, DB_FILE, STATE_FILE
    from data_fetcher import fetch_account_summary, fetch_open_trades, fetch_current_price
    from logger import (
        init_db, get_recent_trades, get_all_trades,
        get_strategy_stats, get_open_db_trades,
    )
    from performance_tracker import (
        compute_metrics, compute_equity_curve,
        compute_session_stats, compute_instrument_stats,
    )
    BOT_CONFIGURED = True
except ImportError as e:
    BOT_CONFIGURED = False
    _import_error = str(e)

# ── Init DB ───────────────────────────────────────────────────
if BOT_CONFIGURED:
    try:
        init_db()
    except Exception:
        pass

# ── Helpers ───────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _get_account() -> dict:
    try:
        return fetch_account_summary() or {}
    except Exception:
        return {}


def _get_open_trades() -> list:
    try:
        return fetch_open_trades()
    except Exception:
        return []


# ── Sidebar ────────────────────────────────────────────────────
with st.sidebar:
    st.title("🤖 ICT/SMC Bot")
    st.caption("7-Strategy Gold & Forex System")
    st.divider()

    if not BOT_CONFIGURED:
        st.error(f"⚠️ Bot not configured\n\n{_import_error if 'import_error' in dir() else 'Check .env'}")
    else:
        state = _load_state()
        circuit_breaker = state.get("circuit_breaker", False)

        if circuit_breaker:
            st.error("🚨 Circuit Breaker ACTIVE")
        else:
            st.success("✅ Bot Ready")

        st.metric("Trades Today", state.get("trades_today", 0))
        st.metric("Daily PnL",
                  f"${state.get('daily_pnl', 0.0):+.2f}",
                  delta_color="normal")

    st.divider()
    auto_refresh = st.toggle("Auto-refresh (30s)", value=True)
    if st.button("🔄 Refresh Now"):
        st.rerun()

    st.divider()
    st.caption(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

# ── Auto refresh ─────────────────────────────────────────────
if auto_refresh:
    time.sleep(0.1)
    st.markdown(
        '<meta http-equiv="refresh" content="30">',
        unsafe_allow_html=True
    )

# ── Title ─────────────────────────────────────────────────────
st.title("📊 ICT/SMC Trading Dashboard")
st.caption("Live monitoring — XAU/USD + Major Forex | 7 Strategies")

if not BOT_CONFIGURED:
    st.warning("Configure your `.env` file with OANDA credentials to see live data.")
    st.stop()

# ── Row 1: Account KPIs ───────────────────────────────────────
st.subheader("Account Overview")
account = _get_account()
open_trades_live = _get_open_trades()

col1, col2, col3, col4, col5, col6 = st.columns(6)
with col1:
    st.metric("Balance", f"${account.get('balance', 0):,.2f}")
with col2:
    st.metric("NAV", f"${account.get('nav', 0):,.2f}")
with col3:
    upnl = account.get('unrealized_pnl', 0)
    st.metric("Unrealized P&L", f"${upnl:+.2f}",
              delta_color="normal" if upnl >= 0 else "inverse")
with col4:
    st.metric("Open Positions", account.get('open_trade_count', 0))
with col5:
    st.metric("Margin Used", f"${account.get('margin_used', 0):,.2f}")
with col6:
    st.metric("Margin Available", f"${account.get('margin_avail', 0):,.2f}")

st.divider()

# ── Row 2: Performance + Equity ──────────────────────────────
col_left, col_right = st.columns([1, 2])

with col_left:
    st.subheader("Performance Metrics")
    trades = get_all_trades()
    metrics = compute_metrics(trades)

    m1, m2 = st.columns(2)
    with m1:
        st.metric("Total Trades", metrics["total_trades"])
        st.metric("Win Rate",     f"{metrics['win_rate']}%")
        st.metric("Net PnL",      f"${metrics['net_pnl']:+,.2f}")
        st.metric("Expectancy",   f"${metrics['expectancy']:+.2f}")
    with m2:
        st.metric("Profit Factor", f"{metrics['profit_factor']:.2f}")
        st.metric("Sharpe Ratio",  f"{metrics['sharpe_ratio']:.2f}")
        st.metric("Max Drawdown",  f"${metrics['max_drawdown']:,.2f}")
        st.metric("Avg Win",       f"${metrics['avg_win']:+.2f}")

with col_right:
    st.subheader("Equity Curve")
    equity_data = compute_equity_curve(trades)
    if len(equity_data) > 1:
        df_equity = pd.DataFrame(equity_data)
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=list(range(len(df_equity))),
            y=df_equity["equity"],
            mode="lines",
            name="Equity",
            line=dict(color="#00CC88", width=2),
            fill="tozeroy",
            fillcolor="rgba(0,204,136,0.1)",
        ))
        fig.update_layout(
            height=280,
            margin=dict(l=0, r=0, t=10, b=0),
            xaxis_title="Trade #",
            yaxis_title="Equity ($)",
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(gridcolor="rgba(128,128,128,0.2)"),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No closed trades yet — equity curve will appear after first trades close")

st.divider()

# ── Row 3: Open positions + live prices ──────────────────────
col_pos, col_prices = st.columns([3, 2])

with col_pos:
    st.subheader("Open Positions")
    if open_trades_live:
        rows = []
        for t in open_trades_live:
            rows.append({
                "Trade ID":    t.get("id", ""),
                "Pair":        t.get("instrument", ""),
                "Units":       f"{t.get('units', 0):,.0f}",
                "Entry":       f"{t.get('open_price', 0):.5f}",
                "SL":          f"{t.get('stop_loss') or '—'}",
                "TP":          f"{t.get('take_profit') or '—'}",
                "Unrealized":  f"${t.get('unrealized', 0):+.2f}",
                "Open Time":   t.get("open_time", "")[:16],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, height=200)
    else:
        st.info("No open positions")

with col_prices:
    st.subheader("Live Prices")
    price_rows = []
    for pair in ALL_INSTRUMENTS[:5]:   # Show first 5 to keep it clean
        try:
            p = fetch_current_price(pair)
            if p:
                price_rows.append({
                    "Pair":   pair,
                    "Bid":    f"{p['bid']:.5f}",
                    "Ask":    f"{p['ask']:.5f}",
                    "Spread": f"{p['spread_pips']:.1f}",
                })
        except Exception:
            pass
    if price_rows:
        st.dataframe(pd.DataFrame(price_rows), use_container_width=True, height=200)
    else:
        st.info("Prices unavailable — check OANDA credentials")

st.divider()

# ── Row 4: Strategy + Session breakdown ──────────────────────
col_strat, col_sess = st.columns(2)

with col_strat:
    st.subheader("Strategy Performance")
    strategy_stats = get_strategy_stats()
    if strategy_stats:
        df_strat = pd.DataFrame(strategy_stats)
        df_strat = df_strat[["strategy_id", "total", "wins", "losses", "win_rate", "net_pnl"]]
        df_strat.columns = ["Strategy", "Trades", "Wins", "Losses", "Win Rate %", "Net PnL ($)"]
        st.dataframe(df_strat, use_container_width=True)

        # Bar chart
        fig2 = go.Figure(go.Bar(
            x=df_strat["Strategy"],
            y=df_strat["Net PnL ($)"],
            marker_color=["#00CC88" if v >= 0 else "#FF4B4B" for v in df_strat["Net PnL ($)"]],
        ))
        fig2.update_layout(
            height=200,
            margin=dict(l=0, r=0, t=5, b=0),
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("No closed trades yet")

with col_sess:
    st.subheader("Session Performance")
    sess_stats = compute_session_stats(trades)
    if sess_stats:
        rows = []
        for session, s in sess_stats.items():
            rows.append({
                "Session":  session,
                "Trades":   s["trades"],
                "Win Rate": f"{s['win_rate']}%",
                "Net PnL":  f"${s['net_pnl']:+.2f}",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

    st.subheader("Instrument Performance")
    instr_stats = compute_instrument_stats(trades)
    if instr_stats:
        rows = []
        for pair, s in instr_stats.items():
            rows.append({
                "Pair":     pair,
                "Trades":   s["trades"],
                "Win Rate": f"{s['win_rate']}%",
                "Net PnL":  f"${s['net_pnl']:+.2f}",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

st.divider()

# ── Row 5: Recent trade log ───────────────────────────────────
st.subheader("Recent Trades (Last 50)")
recent = get_recent_trades(50)
if recent:
    df_trades = pd.DataFrame(recent)
    cols = ["trade_id", "pair", "signal", "strategy_id", "score",
            "entry_price", "close_price", "pnl", "status",
            "open_time", "close_time"]
    available = [c for c in cols if c in df_trades.columns]
    df_display = df_trades[available].copy()

    # Colour PnL
    def _colour_pnl(val):
        if isinstance(val, (int, float)):
            color = "color: #00CC88" if val > 0 else ("color: #FF4B4B" if val < 0 else "")
            return color
        return ""

    if "pnl" in df_display.columns:
        st.dataframe(
            df_display.style.applymap(_colour_pnl, subset=["pnl"]),
            use_container_width=True,
            height=350,
        )
    else:
        st.dataframe(df_display, use_container_width=True, height=350)
else:
    st.info("No trades recorded yet")

st.divider()

# ── Row 6: State + Logs ───────────────────────────────────────
col_state, col_events = st.columns(2)

with col_state:
    st.subheader("Bot State")
    state = _load_state()
    if state:
        st.json(state)
    else:
        st.info("State file not found — is the bot running?")

with col_events:
    st.subheader("Recent Events")
    try:
        import sqlite3
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        events = conn.execute(
            "SELECT event_type, payload, created_at FROM bot_events ORDER BY id DESC LIMIT 20"
        ).fetchall()
        conn.close()
        if events:
            for ev in events:
                with st.expander(f"{ev['created_at'][:16]} — {ev['event_type']}"):
                    try:
                        st.json(json.loads(ev["payload"]))
                    except Exception:
                        st.text(ev["payload"])
        else:
            st.info("No events logged yet")
    except Exception as e:
        st.info(f"Events unavailable: {e}")

# ── Footer ────────────────────────────────────────────────────
st.caption("ICT/SMC Gold Bot v1.0 | 7 Strategies: Asian Sweep · NY Killshot · OB+Psych · Weekly Profile · FVG · PO3 · Silver Bullet")
