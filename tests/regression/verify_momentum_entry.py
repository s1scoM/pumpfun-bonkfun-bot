"""entry_mode: momentum -- thresholds, config wiring, traded-today memory (offline)."""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from bot_runner import build_trader_settings  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from trading.momentum import (  # noqa: E402
    WSOL,
    MomentumConfig,
    TradedToday,
    evaluate_pair,
    load_momentum_config,
)

now = time.time() * 1000
good = {
    "dexId": "pumpswap",
    "quoteToken": {"address": WSOL},
    "priceChange": {"m5": 25, "h1": 60},
    "txns": {"m5": {"buys": 120, "sells": 60}},
    "volume": {"m5": 9000},
    "liquidity": {"usd": 30000},
    "pairCreatedAt": now - 30 * 60000,
}
cfg = MomentumConfig()
failures = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        failures.append(name)


def variant(**kw):
    p = {**good, **kw}
    return evaluate_pair(p, cfg, now)


check("a strong, liquid, aged PumpSwap pair passes", variant() is None)
check("curve pair passes without DexScreener liquidity", variant(dexId="pumpfun", liquidity=None) is None)
check("m5 below +15% fails", variant(priceChange={"m5": 10, "h1": 60}) is not None)
check("m5 blow-off above +150% fails", variant(priceChange={"m5": 400, "h1": 600}) is not None)
check("negative h1 fails", variant(priceChange={"m5": 20, "h1": -5}) is not None)
check("sell-heavy flow fails", variant(txns={"m5": {"buys": 100, "sells": 90}}) is not None)
check("wash-like tiny trades fail", variant(txns={"m5": {"buys": 2000, "sells": 900}}, volume={"m5": 4000}) is not None)
check("thin pool fails", variant(liquidity={"usd": 5000}) is not None)
check("too young fails", variant(pairCreatedAt=now - 3 * 60000) is not None)
check("raydium is not tradable here", variant(dexId="raydium") is not None)
check("non-SOL quote fails", variant(quoteToken={"address": "USDC"}) is not None)

c = load_momentum_config({"min_m5_change_pct": 20, "bogus": 1})
check("config keys load, unknown ignored", c.min_m5_change_pct == 20)

with tempfile.TemporaryDirectory() as d:
    t = TradedToday(f"{d}/t.json")
    t.add("MintA")
    check("traded today is remembered across instances", TradedToday(f"{d}/t.json").contains("MintA"))
    check("other mints are not", not t.contains("MintB"))

base = {
    "rpc_endpoint": "x", "wss_endpoint": "x", "private_key": "x",
    "trade": {"buy_amount": 0.01, "buy_slippage": 0.1, "sell_slippage": 0.1},
    "filters": {"listener_type": "logs"},
}
check("entry_mode defaults to sniper", build_trader_settings(base, Platform.PUMP_FUN).entry_mode == "sniper")
m = {**base, "trade": {**base["trade"], "entry_mode": "momentum"},
     "filters": {"listener_type": "logs", "momentum": {"poll_seconds": 30}}}
s = build_trader_settings(m, Platform.PUMP_FUN)
check("entry_mode and filters.momentum reach the settings", s.entry_mode == "momentum" and s.momentum == {"poll_seconds": 30})

sys.exit(1 if failures else 0)
