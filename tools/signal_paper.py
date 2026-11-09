"""Paper test for entry_mode copytrade / xsignal. Reads only: no keys, no signing, no sends.

Runs the real scanner (copytrade: websocket + getTransaction + chase guard;
xsignal: queue file + DexScreener sanity), prices each pick on chain where the
buyer would buy it, and follows it with the repo's Position exit rules from the
bot YAML. Every pick is its own paper position.

Usage: PYTHONPATH=src python tools/signal_paper.py MODE bots/<cfg>.yaml MINUTES OUT.jsonl
"""

import asyncio
import logging
import json
import sys
import time

import yaml

from core.client import SolanaClient
from interfaces.core import CurveGraduatedError, Platform
from platforms import get_platform_implementations
from trading.position import Position
from trading.signal_entries import (
    CopytradeScanner,
    XSignalScanner,
    load_copytrade_config,
    load_xsignal_config,
)

RPC = "https://solana-rpc.publicnode.com"


async def read_price(impl, info):
    try:
        return await impl.curve_market.get_price(info)
    except CurveGraduatedError:
        return await impl.graduated_market.get_price(info)


async def main(mode, cfg_path, minutes, out_path):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = yaml.safe_load(open(cfg_path))
    trade = cfg["trade"]
    client = SolanaClient(RPC, max_rps=4)
    impl = get_platform_implementations(Platform.PUMP_FUN, client)
    raw = dict((cfg.get("filters") or {}).get(mode) or {})
    raw["traded_file"] = f"/workspace/signals-dev/paper-{mode}-traded.json"
    if mode == "copytrade":
        scanner = CopytradeScanner(client, load_copytrade_config(raw))
        ws = asyncio.create_task(scanner.detector())
    else:
        raw["state_file"] = f"/workspace/signals-dev/paper-{mode}-state.json"
        scanner = XSignalScanner(client, load_xsignal_config(raw))
        ws = None
    out = open(out_path, "a")

    def emit(kind, **kw):
        rec = {"t": time.strftime("%H:%M:%S"), "kind": kind, **kw}
        out.write(json.dumps(rec, default=str) + "\n"); out.flush()
        print(json.dumps(rec, default=str), flush=True)

    positions = {}
    end = time.monotonic() + minutes * 60
    emitted = 0

    async def tracker():
        while True:
            for p in positions.values():
                if not p["open"]:
                    continue
                try:
                    price = await read_price(impl, p["info"])
                except Exception:  # noqa: BLE001
                    continue
                p["last"] = price
                ok, reason = p["pos"].should_exit(price)
                if ok:
                    p["open"] = False
                    p["exit"], p["reason"] = price, reason.value if reason else None
                    emit("paper_sell", symbol=p["info"].symbol, reason=p["reason"],
                         exit_price_sol=price, pnl_pct=round((price / p["entry"] - 1) * 100, 2))
            await asyncio.sleep(3)

    track = asyncio.create_task(tracker())
    while time.monotonic() < end:
        got = await scanner.next_signal(min(end, time.monotonic() + 10))
        for ev in scanner.events[emitted:]:
            emit("scanner", **{("event" if k == "kind" else k): v for k, v in ev.items()})
        emitted = len(scanner.events)
        if not got:
            continue
        info, rec = got
        price = info.additional_data.get("onchain_price_sol")
        if not price:
            continue
        scanner.mark_traded(str(info.mint))
        pos = Position.create_from_buy_result(
            info.mint, info.symbol, price, trade["buy_amount"] / price,
            take_profit_percentage=trade.get("take_profit_percentage"),
            stop_loss_percentage=trade.get("stop_loss_percentage"),
            trailing_stop_percentage=trade.get("trailing_stop_percentage"),
            max_hold_time=trade.get("max_hold_time"),
        )
        positions[str(info.mint)] = {"pos": pos, "info": info, "entry": price, "last": price,
                                     "open": True, "order": len(positions) + 1}
        emit("paper_buy", order=len(positions), symbol=info.symbol, mint=str(info.mint),
             venue="pumpswap" if info.additional_data.get("graduated") else "curve",
             entry_price_sol=price, **{k: v for k, v in rec.items() if k not in ("mint", "symbol", "venue")})
    track.cancel()
    if ws:
        ws.cancel()
    total, summary = 0.0, []
    for p in positions.values():
        last = p.get("exit", p["last"])
        pct = (last / p["entry"] - 1) * 100
        sol = trade["buy_amount"] * pct / 100
        total += sol
        summary.append({"order": p["order"], "symbol": p["info"].symbol, "entry": p["entry"],
                        "last_or_exit": last, "pnl_pct": round(pct, 2), "pnl_sol": round(sol, 6),
                        "status": p.get("reason") or "open (marked to market)"})
    emit("summary", mode=mode, picks=len(positions), total_pnl_sol_ex_fees=round(total, 6),
         positions=summary)
    await scanner.close()
    await client.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]))
