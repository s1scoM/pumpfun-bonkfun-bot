"""Paper test for entry_mode: momentum. Reads only: no keys, no signing, no sends.

Runs the real MomentumScanner (discovery, thresholds, confirmation, on-chain
resolution), prices each pick on chain where the buyer would buy it (curve, or
PumpSwap once graduated), and follows it with the repo's own Position exit rules
(TP / SL / trailing / max hold) from the bot YAML. Every pick is tracked as its
own paper position; the first one is what single-slot live mode would buy.

Usage: PYTHONPATH=src python tools/momentum_paper.py bots/<cfg>.yaml MINUTES OUT.jsonl
"""

import asyncio
import json
import sys
import time

import yaml

from core.client import SolanaClient
from interfaces.core import CurveGraduatedError, Platform
from platforms import get_platform_implementations
from trading.momentum import MomentumScanner, load_momentum_config
from trading.position import Position

RPC = "https://solana-rpc.publicnode.com"


async def read_price(impl, info):
    try:
        return await impl.curve_market.get_price(info), "curve"
    except CurveGraduatedError:
        return await impl.graduated_market.get_price(info), "pumpswap"


async def main(cfg_path: str, minutes: float, out_path: str) -> None:
    cfg = yaml.safe_load(open(cfg_path))
    trade = cfg["trade"]
    mcfg = load_momentum_config((cfg.get("filters") or {}).get("momentum"))
    mcfg.traded_file = "/workspace/momentum-dev/paper-traded.json"
    client = SolanaClient(RPC, max_rps=4)
    impl = get_platform_implementations(Platform.PUMP_FUN, client)
    scanner = MomentumScanner(client, mcfg)
    out = open(out_path, "a")

    def emit(kind, **kw):
        rec = {"t": time.strftime("%H:%M:%S"), "kind": kind, **kw}
        out.write(json.dumps(rec, default=str) + "\n")
        out.flush()
        print(json.dumps(rec, default=str), flush=True)

    positions: dict[str, dict] = {}
    end = time.monotonic() + minutes * 60
    next_poll = 0.0
    polls = 0
    while time.monotonic() < end:
        now = time.monotonic()
        if now >= next_poll:
            next_poll = now + mcfg.poll_seconds
            polls += 1
            try:
                picks = await scanner.poll_once()
            except Exception as e:  # noqa: BLE001
                emit("poll_error", error=str(e))
                picks = []
            passing = [s for s in scanner.last_poll if s["reason"] is None]
            emit("poll", n=polls, scored=len(scanner.last_poll), passing=len(passing),
                 confirmed=[p["symbol"] for p in picks])
            for pick in picks:
                if pick["mint"] in positions:
                    continue
                try:
                    info = await scanner.resolve(pick["mint"], pick)
                    price = info.additional_data["onchain_price_sol"]
                    venue = "pumpswap" if info.additional_data["graduated"] else "curve"
                except Exception as e:  # noqa: BLE001
                    emit("resolve_skip", symbol=pick["symbol"], mint=pick["mint"], error=str(e))
                    scanner._rejected.add(pick["mint"])
                    continue
                qty = trade["buy_amount"] / price if price else 0
                pos = Position.create_from_buy_result(
                    info.mint, info.symbol, price, qty,
                    take_profit_percentage=trade.get("take_profit_percentage"),
                    stop_loss_percentage=trade.get("stop_loss_percentage"),
                    trailing_stop_percentage=trade.get("trailing_stop_percentage"),
                    max_hold_time=trade.get("max_hold_time"),
                )
                positions[pick["mint"]] = {"pos": pos, "info": info, "venue": venue,
                                           "entry": price, "last": price, "open": True,
                                           "order": len(positions) + 1}
                scanner.mark_traded(pick["mint"])
                emit("paper_buy", order=len(positions), symbol=info.symbol, mint=pick["mint"],
                     venue=venue, entry_price_sol=price, ds_price_sol=pick["price_sol"],
                     m5=pick["m5"], h1=pick["h1"], buys=pick["buys_m5"], sells=pick["sells_m5"],
                     vol5m=pick["vol_m5"], liq=pick["liq_usd"], age_min=pick["age_min"])
        for mint, p in positions.items():
            if not p["open"]:
                continue
            try:
                price, venue = await read_price(impl, p["info"])
            except Exception as e:  # noqa: BLE001
                continue
            p["last"] = price
            ok, reason = p["pos"].should_exit(price)
            if ok:
                p["open"] = False
                p["exit"], p["reason"] = price, reason.value if reason else None
                emit("paper_sell", symbol=p["info"].symbol, reason=p["reason"],
                     exit_price_sol=price, pnl_pct=round((price / p["entry"] - 1) * 100, 2))
        await asyncio.sleep(3)
    total = 0.0
    summary = []
    for mint, p in positions.items():
        last = p.get("exit", p["last"])
        pct = (last / p["entry"] - 1) * 100
        sol = 0.0 if not p["entry"] else (trade["buy_amount"] * pct / 100)
        total += sol
        summary.append({"order": p["order"], "symbol": p["info"].symbol, "venue": p["venue"],
                        "entry": p["entry"], "last_or_exit": last, "pnl_pct": round(pct, 2),
                        "pnl_sol": round(sol, 6), "status": p.get("reason") or "open (marked to market)"})
    emit("summary", polls=polls, picks=len(positions), total_pnl_sol_ex_fees=round(total, 6),
         positions=summary)
    await scanner.close()
    await client.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], float(sys.argv[2]), sys.argv[3]))
