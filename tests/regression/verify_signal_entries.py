"""entry_mode copytrade / xsignal -- swap parsing, queue reading, signal checks (offline)."""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from bot_runner import build_trader_settings  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from trading.signal_entries import (  # noqa: E402
    PUMP,
    WSOL,
    XSignalScanner,
    load_copytrade_config,
    load_xsignal_config,
    parse_wallet_swap,
)

failures = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        failures.append(name)


W, MINT = "Wa11et1111111111111111111111111111111111111", "Mint1111111111111111111111111111111111pump"


def tx(signer=True, sol=-1.0, tok=1000.0, err=None, program=PUMP):
    return {
        "blockTime": int(time.time()),
        "transaction": {"message": {"accountKeys": [
            {"pubkey": W, "signer": signer}, {"pubkey": program, "signer": False}]}},
        "meta": {"err": err, "preBalances": [5_000_000_000, 0],
                 "postBalances": [int(5_000_000_000 + sol * 1e9), 0],
                 "preTokenBalances": [],
                 "postTokenBalances": [{"owner": W, "mint": MINT, "uiTokenAmount": {"uiAmount": tok}}]},
    }


b = parse_wallet_swap(W, tx())
check("a signed pump.fun buy parses with SOL spent and fill price", b and b["side"] == "buy" and abs(b["sol"] - 1) < 1e-9 and abs(b["price_sol"] - 0.001) < 1e-12)
check("not signed by the wallet is ignored (dust/spam mentions)", parse_wallet_swap(W, tx(signer=False)) is None)
check("failed tx is ignored", parse_wallet_swap(W, tx(err={"x": 1})) is None)
check("no pump.fun/PumpSwap program is ignored", parse_wallet_swap(W, tx(program="Other111111111111111111111111111111111111111")) is None)
check("token in with SOL in is not a swap", parse_wallet_swap(W, tx(sol=0.5)) is None)

c = load_copytrade_config({"wallets": [{"address": W, "label": "x"}], "max_chase_pct": 10})
check("copytrade config loads", c.max_chase_pct == 10 and len(c.wallets) == 1)

with tempfile.TemporaryDirectory() as d:
    q = Path(d) / "q.jsonl"
    cfg = load_xsignal_config({"queue_file": str(q), "state_file": f"{d}/s.json", "traded_file": f"{d}/t.json"})
    x = XSignalScanner(None, cfg)
    q.write_text(json.dumps({"ts": time.time(), "mint": MINT}) + "\n" + '{"ts": 1, "mint": "partial')
    got = x.read_new()
    check("queue: complete lines only, partial line left for later", len(got) == 1)
    with q.open("a") as f:
        f.write('"}\n')
    check("queue: the rest of the line is read once complete", len(x.read_new()) == 1)
    check("queue: nothing is read twice", x.read_new() == [])
    now = time.time()
    check("fresh signal with a spike passes", x.check_signal({"ts": now, "mint": MINT, "mentions_now": 30, "mentions_baseline": 5}) is None)
    check("old signal fails", x.check_signal({"ts": now - 3600, "mint": MINT}) is not None)
    check("weak spike fails", x.check_signal({"ts": now, "mint": MINT, "mentions_now": 12, "mentions_baseline": 10}) is not None)
    check("ISO timestamp accepted", x.check_signal({"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "mint": MINT}) is None)
    check("missing mint fails", x.check_signal({"ts": now}) is not None)

base = {"rpc_endpoint": "x", "wss_endpoint": "x", "private_key": "x",
        "trade": {"buy_amount": 0.01, "buy_slippage": 0.1, "sell_slippage": 0.1, "entry_mode": "xsignal"},
        "filters": {"listener_type": "logs", "xsignal": {"poll_seconds": 3}, "copytrade": {"max_delay_s": 9}}}
s = build_trader_settings(base, Platform.PUMP_FUN)
check("entry_mode, filters.xsignal and filters.copytrade reach the settings",
      s.entry_mode == "xsignal" and s.xsignal == {"poll_seconds": 3} and s.copytrade == {"max_delay_s": 9})

sys.exit(1 if failures else 0)
