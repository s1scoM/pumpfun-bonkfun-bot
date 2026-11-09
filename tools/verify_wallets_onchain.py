"""Verify candidate wallets' recent pump.fun/PumpSwap trading from chain (public RPC).
For each wallet: last N signatures -> getTransaction -> per-mint SOL in/out for the wallet.
Read-only."""
import asyncio, json, sys, time, statistics
import aiohttp

RPC = "https://solana-rpc.publicnode.com"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"
QUOTES = {WSOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}
sem = asyncio.Semaphore(4)

async def rpc(s, method, params):
    for attempt in range(6):
        async with sem:
            try:
                async with s.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 429:
                        await asyncio.sleep(1.5 * (attempt + 1)); continue
                    d = await r.json(content_type=None)
                    if "error" in d:
                        await asyncio.sleep(1); continue
                    return d.get("result")
            except Exception:
                await asyncio.sleep(1)
    return None

def analyze(wallet, tx):
    if not tx or tx["meta"] is None or tx["meta"].get("err"):
        return None
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    keys += (tx["meta"].get("loadedAddresses") or {}).get("writable", []) + (tx["meta"].get("loadedAddresses") or {}).get("readonly", [])
    progs = set(keys)
    venue = "pumpfun" if PUMP in progs else ("pumpswap" if PUMPSWAP in progs else "other")
    raw = tx["transaction"]["message"]["accountKeys"]
    signers = {k["pubkey"] for k in raw if isinstance(k, dict) and k.get("signer")}
    if wallet not in signers:
        return None
    i = keys.index(wallet)
    sol = (tx["meta"]["postBalances"][i] - tx["meta"]["preBalances"][i]) / 1e9
    tok = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in tx["meta"].get(side) or []:
            if b.get("owner") != wallet:
                continue
            amt = float(b["uiTokenAmount"]["uiAmount"] or 0)
            tok[b["mint"]] = tok.get(b["mint"], 0) + sign * amt
    sol += tok.pop(WSOL, 0)
    tok = {m: v for m, v in tok.items() if abs(v) > 0 and m not in QUOTES}
    if len(tok) != 1 or abs(sol) < 0.001:
        return None
    mint, d = next(iter(tok.items()))
    if (d > 0) == (sol > 0):
        return None  # not a SOL<->token swap
    return {"mint": mint, "side": "buy" if d > 0 else "sell", "sol": sol, "ts": tx.get("blockTime"), "venue": venue}

async def wallet_report(s, w, n):
    allsigs = await rpc(s, "getSignaturesForAddress", [w["wallet"], {"limit": 1000}]) or []
    sigs = [x for x in allsigs if not x.get("err")][:n]
    txs = await asyncio.gather(*[rpc(s, "getTransaction", [x["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]) for x in sigs])
    trades = [t for t in (analyze(w["wallet"], tx) for tx in txs) if t]
    per = {}
    for t in sorted(trades, key=lambda t: t["ts"] or 0):
        m = per.setdefault(t["mint"], {"in": 0.0, "out": 0.0, "buy_ts": None, "sell_ts": None, "buys": 0, "sells": 0})
        if t["side"] == "buy":
            m["in"] += -t["sol"]; m["buys"] += 1; m["buy_ts"] = m["buy_ts"] or t["ts"]
        else:
            m["out"] += t["sol"]; m["sells"] += 1; m["sell_ts"] = m["sell_ts"] or t["ts"]
    closed = {k: v for k, v in per.items() if v["buys"] and v["sells"]}
    pnl = [v["out"] - v["in"] for v in closed.values()]
    holds = [v["sell_ts"] - v["buy_ts"] for v in closed.values() if v["sell_ts"] and v["buy_ts"] and v["sell_ts"] >= v["buy_ts"]]
    buys = [t for t in trades if t["side"] == "buy"]
    span_h = ((max(t["ts"] for t in trades) - min(t["ts"] for t in trades)) / 3600) if len(trades) > 1 else 0
    return {**w, "sigs": len(sigs), "pump_trades": len(trades), "buys": len(buys),
            "span_h": round(span_h, 2), "last_trade_min_ago": round((time.time() - max(t["ts"] for t in trades)) / 60, 1) if trades else None,
            "closed_mints": len(closed), "closed_wins": sum(p > 0 for p in pnl),
            "closed_net_sol": round(sum(pnl), 3), "median_buy_sol": round(statistics.median([-t["sol"] for t in buys]), 3) if buys else None,
            "median_hold_s": round(statistics.median(holds)) if holds else None,
            "venues": {v: sum(t["venue"] == v for t in trades) for v in ("pumpfun", "pumpswap", "other")},
            "window_h": round((time.time() - min(x["blockTime"] for x in sigs)) / 3600, 2) if sigs else None,
            "trades_per_h": round(len(trades) / span_h, 1) if span_h else None}

async def main():
    cands = json.load(open(sys.argv[1]))
    n = int(sys.argv[2])
    async with aiohttp.ClientSession() as s:
        out = []
        for w in cands:
            r = await wallet_report(s, w, n)
            out.append(r); print(json.dumps(r), flush=True)
    json.dump(out, open(sys.argv[3], "w"), indent=1)

if __name__ == "__main__":
    asyncio.run(main())
