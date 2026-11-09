"""Two more entry modes beside momentum, with the same `wait_for_token` interface.

copytrade
    Follows a list of wallets. One websocket (`logsSubscribe` with `mentions`
    per wallet) reports their transactions; only those whose logs show a
    pump.fun or PumpSwap `Buy` are fetched. A buy is copied when the wallet
    signed it, spent at least `min_wallet_buy_sol`, it landed at most
    `max_delay_s` ago, and the coin's on-chain price is at most
    `max_chase_pct` above the wallet's own fill price (SOL spent / tokens
    received, fees included). If the websocket fails the scanner polls
    `getSignaturesForAddress` per wallet instead.

xsignal
    X (Twitter) data is not reachable from this box (official API needs a key,
    nitter instances are down, x.com needs a login), so another process writes
    signals to a JSONL queue, one per line:

        {"ts": 1794250000, "mint": "<pump.fun mint>", "symbol": "ABC",
         "mentions_now": 42, "mentions_baseline": 6, "window_min": 15,
         "source": "x_search", "query": "$ABC OR <mint>"}

    `ts` is epoch seconds or ISO-8601; `mint` is required (tickers are
    ambiguous, copycats share them). The scanner takes each new line once,
    checks its age, the mention spike when given, and DexScreener sanity
    (not dumping, not a blow-off, buys >= sells, liquidity), then resolves the
    coin on chain with the momentum chase guard.

Both reuse `MomentumScanner.resolve` (on-chain TokenInfo, curve SOL floor,
canonical PumpSwap pool, chase guard) and its traded-today memory, and send
nothing themselves: the trader's risk guard, dry-run switch, buyer and exits
are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import websockets

from interfaces.core import Platform, TokenInfo
from trading.momentum import (
    MomentumConfig,
    MomentumScanner,
    TradedToday,
    pair_summary,
)
from utils.logger import get_logger

logger = get_logger(__name__)

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}


def _load(cls, raw: dict | None, section: str):
    raw = dict(raw or {})
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - known)
    if unknown:
        logger.warning(f"filters.{section}: ignoring unknown keys {unknown}")
    return cls(**{k: v for k, v in raw.items() if k in known})


def parse_wallet_swap(wallet: str, tx: dict | None) -> dict | None:
    """The wallet's SOL<->token swap in a jsonParsed transaction, or None.

    Only transactions the wallet signed, that touch pump.fun or PumpSwap and
    move exactly one non-quote token for the wallet, count.
    """
    if not tx or not tx.get("meta") or tx["meta"].get("err"):
        return None
    msg = tx["transaction"]["message"]
    raw = msg["accountKeys"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in raw]
    loaded = tx["meta"].get("loadedAddresses") or {}
    all_keys = set(keys) | set(loaded.get("writable", [])) | set(loaded.get("readonly", []))
    signers = {k["pubkey"] for k in raw if isinstance(k, dict) and k.get("signer")}
    if wallet not in signers:
        return None
    venue = "pumpfun" if PUMP in all_keys else ("pumpswap" if PUMPSWAP in all_keys else None)
    if venue is None:
        return None
    i = keys.index(wallet)
    sol = (tx["meta"]["postBalances"][i] - tx["meta"]["preBalances"][i]) / 1e9
    tok: dict[str, float] = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in tx["meta"].get(side) or []:
            if b.get("owner") != wallet:
                continue
            amt = float(b["uiTokenAmount"].get("uiAmount") or 0)
            tok[b["mint"]] = tok.get(b["mint"], 0) + sign * amt
    sol += tok.pop(WSOL, 0)
    tok = {m: v for m, v in tok.items() if abs(v) > 0 and m not in STABLES}
    if len(tok) != 1 or abs(sol) < 1e-4:
        return None
    mint, d = next(iter(tok.items()))
    if (d > 0) == (sol > 0):
        return None
    return {
        "mint": mint,
        "side": "buy" if d > 0 else "sell",
        "sol": abs(sol),
        "tokens": abs(d),
        "price_sol": abs(sol) / abs(d) if d else 0.0,
        "ts": tx.get("blockTime"),
        "venue": venue,
    }


# --------------------------------------------------------------------------- copytrade


@dataclass
class CopytradeConfig:
    """`filters.copytrade`."""

    wallets: list = field(default_factory=list)  # [{"address": ..., "label": ...}] or strings
    wss_endpoint: str = "wss://api.mainnet-beta.solana.com"  # publicnode times out mention subs
    rpc_endpoint: str = "https://solana-rpc.publicnode.com"
    max_delay_s: float = 30.0          # their buy must have landed at most this long ago
    max_chase_pct: float = 15.0        # skip if price already this far above their fill
    min_wallet_buy_sol: float = 0.2    # ignore their dust/test buys
    min_curve_real_sol: float = 0.0    # curve coins: SOL already in the curve
    min_wallets: int = 1               # distinct followed wallets buying within window
    consensus_window_s: float = 120.0
    # "poll": getSignaturesForAddress per wallet every poll_seconds (default;
    # deterministic). "ws": logsSubscribe mentions, falling back to polling.
    detect: str = "poll"
    poll_seconds: float = 3.0
    fetch_retries: int = 6
    fetch_workers: int = 4
    flood_limit_per_10s: int = 60      # unsubscribe a wallet spammed beyond this             # getTransaction tries (tx lands before it is served)
    skip_traded_today: bool = True
    traded_file: str = "positions/copytrade-traded.json"


def load_copytrade_config(raw: dict | None) -> CopytradeConfig:
    return _load(CopytradeConfig, raw, "copytrade")


class CopytradeScanner:
    def __init__(self, client, cfg: CopytradeConfig, platform: Platform = Platform.PUMP_FUN) -> None:
        self.cfg = cfg
        self.wallets = {
            (w["address"] if isinstance(w, dict) else str(w)): (
                w.get("label", "") if isinstance(w, dict) else ""
            )
            for w in cfg.wallets
        }
        self.resolver = MomentumScanner(
            client,
            MomentumConfig(
                min_curve_real_sol=cfg.min_curve_real_sol,
                max_onchain_premium_pct=cfg.max_chase_pct,
                traded_file=cfg.traded_file,
            ),
            platform,
        )
        self.traded = TradedToday(cfg.traded_file)
        self._session: aiohttp.ClientSession | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._seen: set[str] = set()
        self.flooded: set[str] = set()
        self.stats = {"poll_rounds": 0, "queued": 0, "fetched": 0, "swaps": 0, "stale": 0}
        self._swaps: asyncio.Queue = asyncio.Queue()
        self._beat = time.monotonic()
        self._polled: set[str] = set()
        self._poll_seen: set[str] = set()
        self._recent_buys: dict[str, dict[str, float]] = {}  # mint -> wallet -> ts
        self.events: list[dict] = []  # every copy decision, for reports

    async def _rpc(self, method: str, params: list) -> Any:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        for attempt in range(4):
            try:
                async with self._session.post(
                    self.cfg.rpc_endpoint,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                ) as r:
                    if r.status == 429:
                        logger.warning("COPYTRADE rpc 429; backing off")
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    data = await r.json(content_type=None)
                    if "error" in data:
                        await asyncio.sleep(1)
                        continue
                    return data.get("result")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"COPYTRADE rpc {method} failed: {e!s}")
                await asyncio.sleep(1)
        return None

    async def _ws_loop(self) -> None:
        """Feed (wallet, signature) for every logged pump.fun/PumpSwap buy."""
        while True:
            try:
                async with websockets.connect(
                    self.cfg.wss_endpoint, ping_interval=20, max_size=2**23
                ) as ws:
                    subs: dict[int, str] = {}
                    rate: dict[str, tuple[float, int]] = {}
                    watched = [w for w in self.wallets if w not in self.flooded]
                    for n, wallet in enumerate(watched, start=1):
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": n, "method": "logsSubscribe",
                            "params": [{"mentions": [wallet]}, {"commitment": "confirmed"}],
                        }))
                    pending = {n: w for n, w in enumerate(watched, start=1)}
                    logger.info(f"COPYTRADE websocket: subscribing to {len(pending)} wallets")
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "id" in msg and msg.get("id") in pending:
                            if "result" in msg:
                                subs[msg["result"]] = pending.pop(msg["id"])
                            else:
                                logger.warning(f"COPYTRADE subscribe failed: {msg.get('error')}")
                                pending.pop(msg["id"])
                            continue
                        params = msg.get("params") or {}
                        if params.get("error"):
                            raise ConnectionError(f"subscription error: {params['error']}")
                        value = (params.get("result") or {}).get("value") or {}
                        sub_id = params.get("subscription")
                        wallet = subs.get(sub_id)
                        if not wallet:
                            continue
                        # A wallet other programs spam with failed transactions
                        # floods the socket until the node drops it: unsubscribe.
                        now = time.monotonic()
                        start, n = rate.get(wallet, (now, 0))
                        if now - start > 10:
                            start, n = now, 0
                        rate[wallet] = (start, n + 1)
                        if n + 1 > self.cfg.flood_limit_per_10s:
                            self.flooded.add(wallet)
                            subs.pop(sub_id, None)
                            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 10_000 + len(self.flooded),
                                                      "method": "logsUnsubscribe", "params": [sub_id]}))
                            logger.warning(f"COPYTRADE {self.wallets[wallet] or wallet[:8]} floods the socket "
                                           f"(>{self.cfg.flood_limit_per_10s}/10s); unsubscribed")
                            continue
                        if value.get("err"):
                            continue
                        logs = " ".join(value.get("logs") or [])
                        if (PUMP in logs or PUMPSWAP in logs) and "Instruction: Buy" in logs:
                            await self._queue.put((wallet, value.get("signature"), time.time()))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"COPYTRADE websocket dropped ({e!s}); polling for 60s then reconnecting")
                await self._poll_for(60)

    async def detector(self) -> None:
        """Watch the wallets and fetch their new transactions concurrently."""
        workers = [asyncio.create_task(self._fetch_worker()) for _ in range(self.cfg.fetch_workers)]
        try:
            await self._watch()
        finally:
            for w in workers:
                w.cancel()

    async def _fetch_worker(self) -> None:
        while True:
            wallet, sig, seen_at = await self._queue.get()
            if sig in self._seen:
                continue
            self._seen.add(sig)
            if time.time() - seen_at > self.cfg.max_delay_s:
                self.stats["stale"] += 1  # backlog: too late to copy anyway
                continue
            try:
                swap = await self._fetch_swap(wallet, sig)
            except Exception:  # noqa: BLE001
                logger.exception("COPYTRADE fetch failed")
                continue
            self.stats["fetched"] += 1
            if swap:
                self.stats["swaps"] += 1
                await self._swaps.put((wallet, sig, swap))

    async def _watch(self) -> None:
        """Feed the queue with the followed wallets' new transactions."""
        while True:
            try:
                if self.cfg.detect == "ws":
                    await self._ws_loop()
                else:
                    await self._poll_for(3600)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a dead detector would silently stop copying
                logger.exception("COPYTRADE detector failed; restarting in 5s")
                await asyncio.sleep(5)

    async def _poll_for(self, seconds: float) -> None:
        """Fallback: getSignaturesForAddress per wallet; new recent ones are queued."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for wallet in self.wallets:
                sigs = await self._rpc(
                    "getSignaturesForAddress", [wallet, {"limit": 15, "commitment": "confirmed"}]
                ) or []
                first = wallet not in self._polled
                self._polled.add(wallet)
                for s in sigs:
                    sig = s["signature"]
                    if s.get("err") or sig in self._poll_seen:
                        continue
                    self._poll_seen.add(sig)
                    if first:
                        continue
                    bt = s.get("blockTime")
                    if bt is None or time.time() - bt <= self.cfg.max_delay_s:
                        self.stats["queued"] += 1
                        await self._queue.put((wallet, sig, time.time()))
            self.stats["poll_rounds"] += 1
            if time.monotonic() - self._beat >= 60:
                self._beat = time.monotonic()
                logger.info(f"COPYTRADE heartbeat {self.stats} queue={self._queue.qsize()}")
            await asyncio.sleep(self.cfg.poll_seconds)

    async def _fetch_swap(self, wallet: str, sig: str) -> dict | None:
        for _ in range(self.cfg.fetch_retries):
            tx = await self._rpc(
                "getTransaction",
                [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                       "commitment": "confirmed"}],
            )
            if tx:
                return parse_wallet_swap(wallet, tx)
            await asyncio.sleep(1)
        return None

    def _log(self, kind: str, **kw) -> None:
        rec = {"t": time.strftime("%H:%M:%S"), "kind": kind, **kw}
        self.events.append(rec)
        logger.info(f"COPYTRADE {kind} " + " ".join(f"{k}={v}" for k, v in kw.items()))

    async def next_signal(self, deadline: float) -> tuple[TokenInfo, dict] | None:
        while time.monotonic() < deadline:
            try:
                wallet, sig, swap = await asyncio.wait_for(
                    self._swaps.get(), timeout=max(0.1, deadline - time.monotonic())
                )
            except TimeoutError:
                return None
            if swap["side"] != "buy":
                continue
            label = self.wallets.get(wallet, "")
            delay = time.time() - (swap["ts"] or 0)
            base = {"wallet": wallet[:8], "label": label, "mint": swap["mint"],
                    "their_sol": round(swap["sol"], 4), "their_price": swap["price_sol"],
                    "delay_s": round(delay, 1), "venue": swap["venue"], "sig": sig[:16]}
            if swap["sol"] < self.cfg.min_wallet_buy_sol:
                self._log("skip", reason="buy below min_wallet_buy_sol", **base)
                continue
            if delay > self.cfg.max_delay_s:
                self._log("skip", reason=f"delay {delay:.0f}s > {self.cfg.max_delay_s}s", **base)
                continue
            if self.cfg.skip_traded_today and self.traded.contains(swap["mint"]):
                self._log("skip", reason="already traded today", **base)
                continue
            buyers = self._recent_buys.setdefault(swap["mint"], {})
            buyers[wallet] = time.time()
            live = {w for w, t in buyers.items() if time.time() - t <= self.cfg.consensus_window_s}
            if len(live) < self.cfg.min_wallets:
                self._log("wait", reason=f"{len(live)}/{self.cfg.min_wallets} wallets", **base)
                continue
            try:
                info = await self.resolver.resolve(
                    swap["mint"], {"price_sol": swap["price_sol"], "symbol": None}
                )
            except Exception as e:  # noqa: BLE001
                self._log("skip", reason=str(e)[:140], **base)
                continue
            onchain = info.additional_data.get("onchain_price_sol")
            info.additional_data["entry"] = "copytrade"
            info.additional_data["copytrade"] = base
            self._log("PICK", symbol=info.symbol, onchain_price=onchain,
                      chase_pct=round((onchain / swap["price_sol"] - 1) * 100, 1)
                      if swap["price_sol"] else None, **base)
            return info, {**base, "onchain_price": onchain}
        return None

    async def wait_for_token(self, timeout: float) -> TokenInfo | None:
        if not self.wallets:
            logger.warning("COPYTRADE: no wallets configured")
            await asyncio.sleep(timeout)
            return None
        task = asyncio.create_task(self.detector())
        try:
            got = await self.next_signal(time.monotonic() + timeout)
            return got[0] if got else None
        finally:
            task.cancel()
            await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        await self.resolver.close()

    def mark_traded(self, mint: str) -> None:
        self.traded.add(mint)


# --------------------------------------------------------------------------- xsignal


@dataclass
class XSignalConfig:
    """`filters.xsignal`."""

    queue_file: str = "/workspace/xsignal-queue.jsonl"
    state_file: str = "positions/xsignal-state.json"
    poll_seconds: float = 5.0
    max_signal_age_s: float = 600.0     # ignore signals older than this
    min_spike_ratio: float = 3.0        # mentions_now / mentions_baseline, when both given
    min_mentions_now: int = 5           # when given
    min_m5_change_pct: float = -10.0    # not dumping right now
    max_m5_change_pct: float = 150.0    # not a blow-off
    min_buy_sell_ratio: float = 1.0     # 5m buys >= sells
    min_liquidity_usd: float = 10000.0  # PumpSwap pools
    min_curve_real_sol: float = 5.0     # curve coins
    max_onchain_premium_pct: float = 15.0
    skip_traded_today: bool = True
    traded_file: str = "positions/xsignal-traded.json"


def load_xsignal_config(raw: dict | None) -> XSignalConfig:
    return _load(XSignalConfig, raw, "xsignal")


def _ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


class XSignalScanner:
    def __init__(self, client, cfg: XSignalConfig, platform: Platform = Platform.PUMP_FUN) -> None:
        self.cfg = cfg
        self.resolver = MomentumScanner(
            client,
            MomentumConfig(
                min_curve_real_sol=cfg.min_curve_real_sol,
                max_onchain_premium_pct=cfg.max_onchain_premium_pct,
                min_liquidity_usd=cfg.min_liquidity_usd,
                traded_file=cfg.traded_file,
            ),
            platform,
        )
        self.traded = TradedToday(cfg.traded_file)
        self.events: list[dict] = []

    def _offset(self) -> int:
        try:
            return int(json.loads(Path(self.cfg.state_file).read_text()).get("offset", 0))
        except (OSError, ValueError):
            return 0

    def _save_offset(self, offset: int) -> None:
        p = Path(self.cfg.state_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"offset": offset}))

    def read_new(self) -> list[dict]:
        """New complete lines since the saved byte offset."""
        path = Path(self.cfg.queue_file)
        if not path.exists():
            return []
        offset = self._offset()
        size = path.stat().st_size
        if size < offset:  # truncated/rotated
            offset = 0
        with path.open("rb") as f:
            f.seek(offset)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        self._save_offset(offset + end + 1)
        out = []
        for line in chunk[: end + 1].splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                logger.warning(f"XSIGNAL bad line: {line[:120]!r}")
        return out

    def _log(self, kind: str, **kw) -> None:
        rec = {"t": time.strftime("%H:%M:%S"), "kind": kind, **kw}
        self.events.append(rec)
        logger.info(f"XSIGNAL {kind} " + " ".join(f"{k}={v}" for k, v in kw.items()))

    def check_signal(self, sig: dict) -> str | None:
        if not sig.get("mint"):
            return "no mint"
        ts = _ts(sig.get("ts"))
        if ts is None:
            return "no/invalid ts"
        if time.time() - ts > self.cfg.max_signal_age_s:
            return f"signal {time.time() - ts:.0f}s old > {self.cfg.max_signal_age_s}s"
        now, base = sig.get("mentions_now"), sig.get("mentions_baseline")
        if now is not None and float(now) < self.cfg.min_mentions_now:
            return f"mentions {now} < {self.cfg.min_mentions_now}"
        if now is not None and base is not None:
            ratio = float(now) / max(float(base), 1.0)
            if ratio < self.cfg.min_spike_ratio:
                return f"spike {ratio:.1f}x < {self.cfg.min_spike_ratio}x"
        if self.cfg.skip_traded_today and self.traded.contains(sig["mint"]):
            return "already traded today"
        return None

    def check_pair(self, pair: dict | None) -> str | None:
        if pair is None:
            return "no pump.fun/PumpSwap SOL pair on DexScreener"
        s = pair_summary(pair)
        if s["m5"] < self.cfg.min_m5_change_pct:
            return f"m5 {s['m5']:+.1f}% (dumping)"
        if s["m5"] > self.cfg.max_m5_change_pct:
            return f"m5 {s['m5']:+.1f}% (blow-off)"
        if s["buys_m5"] < self.cfg.min_buy_sell_ratio * max(s["sells_m5"], 1):
            return f"buys/sells {s['buys_m5']}/{s['sells_m5']}"
        if s["dex"] != "pumpfun" and s["liq_usd"] < self.cfg.min_liquidity_usd:
            return f"liquidity ${s['liq_usd']:,.0f}"
        return None

    async def next_signal(self, deadline: float) -> tuple[TokenInfo, dict] | None:
        while time.monotonic() < deadline:
            for sig in self.read_new():
                base = {"mint": sig.get("mint"), "symbol": sig.get("symbol"),
                        "mentions_now": sig.get("mentions_now"),
                        "mentions_baseline": sig.get("mentions_baseline"),
                        "source": sig.get("source")}
                reason = self.check_signal(sig)
                if reason:
                    self._log("skip", reason=reason, **base)
                    continue
                pairs = await self.resolver.fetch_pairs([sig["mint"]])
                pair = pairs.get(sig["mint"])
                reason = self.check_pair(pair)
                if reason:
                    self._log("skip", reason=reason, **base)
                    continue
                summary = pair_summary(pair)
                try:
                    info = await self.resolver.resolve(sig["mint"], summary)
                except Exception as e:  # noqa: BLE001
                    self._log("skip", reason=str(e)[:140], **base)
                    continue
                info.additional_data["entry"] = "xsignal"
                info.additional_data["xsignal"] = sig
                rec = {**base, "symbol": info.symbol, "dex": summary["dex"],
                       "m5": summary["m5"], "h1": summary["h1"],
                       "ds_price": summary["price_sol"],
                       "onchain_price": info.additional_data.get("onchain_price_sol")}
                self._log("PICK", **rec)
                return info, rec
            await asyncio.sleep(self.cfg.poll_seconds)
        return None

    async def wait_for_token(self, timeout: float) -> TokenInfo | None:
        try:
            got = await self.next_signal(time.monotonic() + timeout)
            return got[0] if got else None
        finally:
            await self.resolver.close()

    async def close(self) -> None:
        await self.resolver.close()

    def mark_traded(self, mint: str) -> None:
        self.traded.add(mint)
