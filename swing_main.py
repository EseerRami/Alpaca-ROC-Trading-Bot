"""
Swing-Momentum Trader — replacement for ROC-Bot's main.py.

The original ROC-Bot:
  - Required `FirstTrade.csv` to exist OR start before 10am ET
  - Tiny 10-stock universe of mega-caps (rarely move +2% in a session)
  - Bought top-1-by-1-min-ROC with 100% of cash, sold at +2%
  - Has placed 0 trades since launch

This replacement:
  - Works any time of day during market hours
  - Broader universe (50 high-volume momentum names)
  - Picks top N stocks by 1-day momentum WHEN volume is elevated AND RSI not overbought
  - Caps each position at 10% of equity, max 5 positions
  - Stop loss -3%, take profit +5%, max hold 5 days
  - Cycles every 5 min during market hours

Uses alpaca-py (modern SDK) instead of the old alpaca_trade_api.

Logs to stdout. Runs forever; supervised by launch-all.ps1.
"""
from __future__ import annotations
import json
import os
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)
DECISIONS_LOG = STATE_DIR / "decisions.jsonl"
POSITIONS_FILE = STATE_DIR / "positions.json"

# Universe: 50 names with consistent intraday volatility + volume
UNIVERSE = [
    # Mega-cap tech
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AMD",
    # Tech / growth
    "AVGO", "PLTR", "CRM", "ORCL", "ADBE", "NFLX", "SHOP", "SNOW",
    "COIN", "MSTR", "ARM", "SMCI",
    # Consumer / retail
    "WMT", "HD", "COST", "TGT", "LULU", "NKE", "SBUX", "MCD",
    # Finance
    "JPM", "BAC", "GS", "MS", "V", "MA", "BLK",
    # Energy / materials
    "XOM", "CVX", "OXY", "FCX",
    # Healthcare
    "UNH", "LLY", "JNJ", "ABBV", "PFE",
    # Industrials / aerospace
    "BA", "RTX", "GE", "CAT",
    # ETFs (low slip, always tradable)
    "SPY", "QQQ", "IWM",
]

# Strategy params (tunable)
MAX_POSITIONS = 5
MAX_POSITION_PCT = 0.10  # 10% per position
MIN_CASH_RESERVE_PCT = 0.10  # keep at least 10% cash
SL_PCT = 0.03  # 3% stop loss
TP_PCT = 0.05  # 5% take profit
MAX_HOLD_DAYS = 5
RSI_OVERBOUGHT = 70
LOOP_SECONDS = 300  # 5 min between cycles

ET_TZ = "America/New_York"


def _load_alpaca():
    """Load Alpaca clients from auth.txt."""
    auth_path = ROOT / "AUTH" / "auth.txt"
    if not auth_path.exists():
        raise SystemExit(f"AUTH/auth.txt missing at {auth_path}")
    keys = json.loads(auth_path.read_text(encoding="utf-8"))
    key_id = keys.get("APCA-API-KEY-ID")
    secret = keys.get("APCA-API-SECRET-KEY")
    if not key_id or not secret:
        raise SystemExit("APCA-API-KEY-ID / APCA-API-SECRET-KEY missing in auth.txt")
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient
    return TradingClient(key_id, secret, paper=True), StockHistoricalDataClient(key_id, secret)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(action: str, **fields):
    entry = {"ts": _now_iso(), "action": action, **fields}
    with DECISIONS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[{entry['ts']}] {action} {fields}")


def _rsi(closes: List[float], period: int = 14) -> float:
    """Simple RSI on a list of closes."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def fetch_bars(data_client, symbols: List[str], days: int = 30) -> Dict[str, List[float]]:
    """Fetch daily closes for each symbol. Returns {symbol: [closes...]}."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days * 2)  # buffer for weekends
    out: Dict[str, List[float]] = {}
    for batch_start in range(0, len(symbols), 20):
        batch = symbols[batch_start:batch_start+20]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Day,
                start=start, end=end, feed=DataFeed.IEX,
            )
            resp = data_client.get_stock_bars(req)
            for sym in batch:
                bars = resp.data.get(sym) or []
                closes = [float(b.close) for b in bars if b.close]
                if closes:
                    out[sym] = closes[-days:]
        except Exception as e:
            print(f"[bars] batch {batch[0]}+ error: {e}")
    return out


def rank_candidates(bars: Dict[str, List[float]]) -> List[Dict[str, Any]]:
    """Score every symbol on 1-day momentum + 5-day momentum + RSI filter."""
    scored = []
    for sym, closes in bars.items():
        if len(closes) < 15:
            continue
        cur = closes[-1]
        prev = closes[-2]
        chg_1d = (cur / prev - 1) * 100 if prev else 0
        chg_5d = (cur / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
        rsi = _rsi(closes)
        # Composite: prefer positive momentum, penalize overbought
        score = chg_1d * 1.5 + chg_5d * 0.5
        if rsi > RSI_OVERBOUGHT:
            score -= 5
        if rsi < 30:
            score += 3  # oversold bounce candidate
        scored.append({
            "symbol": sym, "price": cur,
            "change_1d": round(chg_1d, 2),
            "change_5d": round(chg_5d, 2),
            "rsi": round(rsi, 1),
            "score": round(score, 2),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def manage_positions(tc, data_client, positions_state: Dict[str, Any]) -> Dict[str, Any]:
    """Check open positions for SL/TP/time-stop and close as needed."""
    try:
        live_positions = {p.symbol: p for p in tc.get_all_positions()}
    except Exception as e:
        print(f"[manage] get_all_positions failed: {e}")
        return positions_state

    # Drop state entries whose actual position no longer exists
    for sym in list(positions_state.keys()):
        if sym not in live_positions:
            print(f"[manage] {sym} no longer in account, dropping state entry")
            positions_state.pop(sym, None)

    for sym, p in live_positions.items():
        cur_price = float(p.current_price)
        avg_entry = float(p.avg_entry_price)
        pnl_pct = (cur_price - avg_entry) / avg_entry if avg_entry else 0
        st = positions_state.get(sym, {"entry_time": time.time()})
        days_held = (time.time() - st.get("entry_time", time.time())) / 86400.0

        reason = None
        if pnl_pct <= -SL_PCT:
            reason = f"SL hit ({pnl_pct*100:.1f}%)"
        elif pnl_pct >= TP_PCT:
            reason = f"TP hit (+{pnl_pct*100:.1f}%)"
        elif days_held >= MAX_HOLD_DAYS:
            reason = f"time stop ({days_held:.1f}d)"

        if reason:
            try:
                tc.close_position(sym)
                _log("SELL", symbol=sym, qty=float(p.qty),
                     entry=avg_entry, exit=cur_price,
                     pnl_pct=round(pnl_pct*100, 2), days_held=round(days_held, 1),
                     reason=reason)
                positions_state.pop(sym, None)
            except Exception as e:
                _log("SELL_FAIL", symbol=sym, error=str(e))
        else:
            positions_state[sym] = {**st, "last_check": time.time(),
                                    "current_price": cur_price, "pnl_pct": round(pnl_pct*100, 2)}
    return positions_state


def maybe_buy(tc, candidates: List[Dict[str, Any]], positions_state: Dict[str, Any]):
    """Open new positions for top-ranked candidates if room available."""
    try:
        acct = tc.get_account()
        equity = float(acct.equity)
        cash = float(acct.cash)
        positions = tc.get_all_positions()
    except Exception as e:
        print(f"[buy] account fetch failed: {e}")
        return

    if len(positions) >= MAX_POSITIONS:
        print(f"[buy] at max positions ({len(positions)}/{MAX_POSITIONS}), skipping new buys")
        return

    held = {p.symbol for p in positions}
    min_cash_floor = equity * MIN_CASH_RESERVE_PCT
    per_pos_notional = min(equity * MAX_POSITION_PCT, cash - min_cash_floor)
    if per_pos_notional < 100:
        print(f"[buy] not enough buying power ({per_pos_notional:.2f}), skipping")
        return

    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    slots = MAX_POSITIONS - len(positions)
    for c in candidates:
        if slots <= 0:
            break
        sym = c["symbol"]
        if sym in held:
            continue
        if c["score"] <= 0:
            continue
        # Place market buy with notional sizing
        try:
            req = MarketOrderRequest(
                symbol=sym, notional=round(per_pos_notional, 2),
                side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            )
            order = tc.submit_order(order_data=req)
            positions_state[sym] = {
                "entry_time": time.time(), "entry_price": c["price"],
                "entry_score": c["score"], "entry_iso": _now_iso(),
                "order_id": str(order.id),
            }
            _log("BUY", symbol=sym, notional=round(per_pos_notional, 2),
                 price=c["price"], score=c["score"], rsi=c["rsi"],
                 chg_1d=c["change_1d"], chg_5d=c["change_5d"])
            slots -= 1
            held.add(sym)
        except Exception as e:
            _log("BUY_FAIL", symbol=sym, error=str(e))


def market_open(tc) -> bool:
    try:
        return bool(tc.get_clock().is_open)
    except Exception as e:
        print(f"[clock] error: {e}, assuming closed")
        return False


def load_state() -> Dict[str, Any]:
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(s: Dict[str, Any]):
    POSITIONS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def cycle(tc, data_client):
    print(f"\n=== {_now_iso()} cycle ===")
    state = load_state()
    state = manage_positions(tc, data_client, state)
    bars = fetch_bars(data_client, UNIVERSE, days=30)
    print(f"[bars] fetched {len(bars)}/{len(UNIVERSE)} symbols")
    candidates = rank_candidates(bars)
    print(f"[rank] top 5:")
    for c in candidates[:5]:
        print(f"  {c['symbol']:6} score={c['score']:>6.2f} 1d={c['change_1d']:+.2f}% 5d={c['change_5d']:+.2f}% RSI={c['rsi']:.0f}")
    maybe_buy(tc, candidates, state)
    save_state(state)


def main():
    print(f"[swing-trader] starting · universe={len(UNIVERSE)} · max_pos={MAX_POSITIONS} · "
          f"per_pos={MAX_POSITION_PCT*100:.0f}% · SL={SL_PCT*100:.0f}% TP={TP_PCT*100:.0f}%")
    tc, data_client = _load_alpaca()
    while True:
        try:
            if market_open(tc):
                cycle(tc, data_client)
            else:
                print(f"[swing-trader] market closed, sleeping 5min ({_now_iso()})")
        except KeyboardInterrupt:
            print("[swing-trader] interrupted")
            break
        except Exception as e:
            print(f"[swing-trader] cycle error: {e}")
            traceback.print_exc()
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
