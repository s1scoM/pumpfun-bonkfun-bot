"""Pre-buy entry filters for the Momo bot (YAML-configurable).

All checks are best-effort over public RPC. A filter that cannot be evaluated
fails closed (skip) when `strict` is true, otherwise logs and continues.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from solders.pubkey import Pubkey

from core.client import SolanaClient
from interfaces.core import TokenInfo
from utils.logger import get_logger

logger = get_logger(__name__)

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
# Anchor discriminators for pump.fun create / create_v2
_CREATE_DISCS = {
    bytes([24, 30, 200, 40, 5, 28, 7, 119]),  # create
    bytes([214, 144, 76, 236, 95, 139, 49, 180]),  # create_v2
}
_SERIAL_SIG_PAGE = 50
_SERIAL_MAX_SIGS_24H = 120
_SERIAL_MAX_TX_FETCHES = 30

# Token-2022 / SPL mint: COption pubkey layout helpers
_COPY_RE = re.compile(
    r"(trump|elon|pepe|doge|bonk|wif|bitcoin|btc|eth|solana|\bsol\b|obama|harris|biden)",
    re.I,
)


@dataclass
class EntryFilterConfig:
    """Mirrors `filters.entry:` in the bot YAML."""

    enabled: bool = True
    strict: bool = False
    require_authorities_revoked: bool = True
    observe_seconds: int = 45
    min_unique_buyers: int = 15
    min_net_buy_sol: float = 2.0
    require_curve_progress: bool = True
    creator_must_not_sell: bool = True
    max_creator_pct: float = 10.0
    max_top10_pct_excl_curve: float = 35.0
    max_creator_launches_24h: int = 3
    skip_copycat_names: bool = True
    # How many recent bonding-curve signatures to sample for buyer analysis
    signature_sample: int = 40


@dataclass
class FilterStats:
    """Counters for a dry-run session."""

    seen: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    passed: int = 0
    would_buys: list[dict[str, Any]] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _parse_coption_pubkey(data: bytes, offset: int) -> tuple[Pubkey | None, int]:
    """Parse a COption<Pubkey> (4-byte tag + optional 32-byte key)."""
    if len(data) < offset + 4:
        return None, offset
    tag = int.from_bytes(data[offset : offset + 4], "little")
    offset += 4
    if tag == 0:
        return None, offset
    if tag == 1 and len(data) >= offset + 32:
        return Pubkey.from_bytes(data[offset : offset + 32]), offset + 32
    return None, offset


def mint_authorities(mint_data: bytes) -> tuple[Pubkey | None, Pubkey | None]:
    """Return (mint_authority, freeze_authority) from a mint account blob."""
    # SPL / Token-2022 mint layout starts with COption mintAuthority, supply u64,
    # decimals u8, isInitialized u8, COption freezeAuthority.
    mint_auth, off = _parse_coption_pubkey(mint_data, 0)
    off += 8 + 1 + 1  # supply, decimals, isInitialized
    freeze_auth, _ = _parse_coption_pubkey(mint_data, off)
    return mint_auth, freeze_auth


class EntryFilterEngine:
    """Runs the configured pre-buy checks against one TokenInfo."""

    def __init__(self, client: SolanaClient, cfg: EntryFilterConfig) -> None:
        self.client = client
        self.cfg = cfg
        self.stats = FilterStats()
        # creator -> set of mints observed this process (free serial signal)
        self._session_creates: dict[str, set[str]] = {}
        # creator -> (launches_excl_current_at_cache_time, incomplete, cached_at, cached_for_mint)
        self._serial_cache: dict[str, tuple[int, bool, float]] = {}

    async def evaluate(self, token: TokenInfo) -> str | None:
        """Return skip reason or None if the token may be bought."""
        self.stats.seen += 1
        if not self.cfg.enabled:
            self.stats.passed += 1
            return None

        if self.cfg.skip_copycat_names:
            name = f"{token.name} {token.symbol}"
            if _COPY_RE.search(name) and not _looks_original(name):
                self.stats.skip("copycat_name")
                return f"copycat name/ticker: {token.symbol}"

        # Serial launcher: count real pump.fun CREATE/CREATE_V2 in last 24h
        if self.cfg.max_creator_launches_24h > 0 and token.creator:
            reason = await self._check_serial_launcher(token)
            if reason:
                self.stats.skip("serial_launcher")
                return reason

        # Wait-and-confirm (also gives the mint account time to appear on RPC)
        before = None
        if self.cfg.observe_seconds > 0:
            logger.info(
                f"Entry filter observing {token.symbol} for "
                f"{self.cfg.observe_seconds}s before buy checks..."
            )
            before = await self._curve_real_sol(token)
            await asyncio.sleep(self.cfg.observe_seconds)

        # Authorities AFTER observe, with retries for fresh creates
        if self.cfg.require_authorities_revoked:
            reason = await self._check_authorities(token, retries=4, delay_s=1.5)
            if reason:
                self.stats.skip("authority")
                return reason

        if self.cfg.observe_seconds > 0:
            reason = await self._check_after_observe(token, before)
            if reason:
                self.stats.skip(reason.split(":")[0].strip().replace(" ", "_")[:40])
                return reason

        # Holder concentration
        reason = await self._check_holders(token)
        if reason:
            self.stats.skip("holder_concentration")
            return reason

        self.stats.passed += 1
        self.stats.would_buys.append(
            {
                "symbol": token.symbol,
                "name": token.name,
                "mint": str(token.mint),
                "creator": str(token.creator) if token.creator else None,
                "bonding_curve": str(token.bonding_curve) if token.bonding_curve else None,
                "ts": time.time(),
            }
        )
        return None

    async def _check_authorities(
        self,
        token: TokenInfo,
        retries: int = 1,
        delay_s: float = 1.0,
    ) -> str | None:
        """Return skip reason if mint/freeze authority is still set.

        Fresh creates often 404 on public RPC for a few seconds. Callers should
        invoke this after the observe window and allow retries.
        """
        last_exc: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                acct = await self.client.get_account_info(
                    token.mint, commitment="confirmed"
                )
                mint_auth, freeze_auth = mint_authorities(bytes(acct.data))
                if mint_auth is not None:
                    return f"mint authority still set: {mint_auth}"
                if freeze_auth is not None:
                    return f"freeze authority still set: {freeze_auth}"
                return None
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                msg = str(exc).lower()
                retryable = "not found" in msg or "account" in msg
                if retryable and attempt + 1 < retries:
                    logger.info(
                        f"authority retry {attempt + 1}/{retries} for "
                        f"{token.symbol}: {exc}"
                    )
                    await asyncio.sleep(delay_s)
                    continue
                break
        if self.cfg.strict:
            return f"authority check failed: {last_exc}"
        # Soft: unknown (RPC lag / limits) — do not auto-skip
        logger.warning(
            f"authority check incomplete for {token.symbol} after "
            f"{retries} attempt(s): {last_exc} (treating as unknown)"
        )
        return None

    async def _curve_real_sol(self, token: TokenInfo) -> float | None:
        if not token.bonding_curve:
            return None
        try:
            acct = await self.client.get_account_info(
                token.bonding_curve, commitment="confirmed"
            )
            data = bytes(acct.data)
            # pump.fun BondingCurve: after 8-byte discriminator, fields include
            # virtual_token_reserves u64, virtual_sol_reserves u64,
            # real_token_reserves u64, real_sol_reserves u64 ...
            if len(data) < 8 + 32:
                return None
            real_sol = int.from_bytes(data[8 + 24 : 8 + 32], "little")
            return real_sol / 1e9
        except Exception:  # noqa: BLE001
            return None

    async def _check_after_observe(
        self, token: TokenInfo, before_sol: float | None
    ) -> str | None:
        after_sol = await self._curve_real_sol(token)
        if (
            self.cfg.require_curve_progress
            and before_sol is not None
            and after_sol is not None
            and after_sol + 1e-9 < before_sol
        ):
            return (
                f"curve_progress: real_sol fell {before_sol:.4f} -> {after_sol:.4f}"
            )

        buyers, net_buy, creator_sold = await self._sample_curve_flow(token)
        if buyers < self.cfg.min_unique_buyers:
            return (
                f"unique_buyers: {buyers} < min {self.cfg.min_unique_buyers}"
            )
        if net_buy < self.cfg.min_net_buy_sol:
            return f"net_buy_sol: {net_buy:.3f} < min {self.cfg.min_net_buy_sol}"
        if self.cfg.creator_must_not_sell and creator_sold:
            return "creator_sold: creator sold during observe window"
        if (
            self.cfg.require_curve_progress
            and before_sol is not None
            and after_sol is not None
            and (after_sol - before_sol) < min(0.1, self.cfg.min_net_buy_sol * 0.05)
        ):
            return (
                f"curve_progress: real_sol barely rose "
                f"{before_sol:.4f} -> {after_sol:.4f}"
            )
        return None

    async def _sample_curve_flow(
        self, token: TokenInfo
    ) -> tuple[int, float, bool]:
        """Best-effort unique buyer / net buy / creator-sold from recent txs."""
        if not token.bonding_curve:
            return 0, 0.0, False
        try:
            from solana.rpc.commitment import Confirmed

            rpc = await self.client.get_client()
            await self.client._rate_limiter.acquire()
            sigs = await rpc.get_signatures_for_address(
                token.bonding_curve,
                limit=self.cfg.signature_sample,
                commitment=Confirmed,
            )
            signatures = [s.signature for s in (sigs.value or [])]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"signature sample failed for {token.symbol}: {exc}")
            if self.cfg.strict:
                return 0, 0.0, False
            # Soft mode: treat as unknown → use signature count as weak proxy
            return 0, 0.0, False

        buyers: set[str] = set()
        net = 0.0
        creator_sold = False
        creator = str(token.creator) if token.creator else None

        for sig in signatures[: self.cfg.signature_sample]:
            try:
                await self.client._rate_limiter.acquire()
                tx = await rpc.get_transaction(
                    sig,
                    encoding="jsonParsed",
                    commitment=Confirmed,
                    max_supported_transaction_version=0,
                )
            except Exception:  # noqa: BLE001
                continue
            if not tx.value:
                continue
            meta = tx.value.transaction.meta
            if meta is None or meta.err is not None:
                continue
            # Fee payer as buyer proxy
            try:
                msg = tx.value.transaction.transaction.message
                keys = msg.account_keys
                fee_payer = str(keys[0].pubkey if hasattr(keys[0], "pubkey") else keys[0])
            except Exception:  # noqa: BLE001
                continue
            # SOL delta for fee payer
            try:
                pre = meta.pre_balances[0] / 1e9
                post = meta.post_balances[0] / 1e9
                delta = post - pre
            except Exception:  # noqa: BLE001
                delta = 0.0
            if delta < -0.0001:
                buyers.add(fee_payer)
                net += -delta
            elif delta > 0.0001 and creator and fee_payer == creator:
                creator_sold = True
            await asyncio.sleep(0.05)  # soft rate limit

        return len(buyers), net, creator_sold

    async def _check_holders(self, token: TokenInfo) -> str | None:
        try:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenLargestAccounts",
                "params": [str(token.mint), {"commitment": "confirmed"}],
            }
            resp = await self.client.post_rpc(body)
            value = (resp or {}).get("result", {}).get("value") or []
        except Exception as exc:  # noqa: BLE001
            if self.cfg.strict:
                return f"holder check failed: {exc}"
            logger.warning(f"holder check failed for {token.symbol}: {exc}")
            return None
        if not value:
            return None
        total = sum(int(v.get("amount") or 0) for v in value) or 1
        curve = str(token.associated_bonding_curve or token.bonding_curve or "")
        creator = str(token.creator) or ""
        # Largest accounts don't include owner — only addresses. For pump.fun the
        # bonding curve ATA is usually #1. Without owner mapping we approximate:
        # treat address[0] as curve if it matches known ATA, else skip creator %.
        amounts = []
        creator_amt = 0
        for v in value:
            addr = v.get("address") or ""
            amt = int(v.get("amount") or 0)
            if curve and addr == curve:
                continue
            amounts.append(amt)
            # Cannot map creator without getAccountInfo owner; skip creator exact
        top10 = sum(amounts[:10])
        top10_pct = 100.0 * top10 / total
        if top10_pct > self.cfg.max_top10_pct_excl_curve:
            return (
                f"top10_holders: {top10_pct:.1f}% > "
                f"{self.cfg.max_top10_pct_excl_curve}%"
            )
        # Creator hold: optional second pass via parsed token accounts is costly;
        # use additional_data if present.
        return None

    async def _check_serial_launcher(self, token: TokenInfo) -> str | None:
        """Skip if creator launched more than max_creator_launches_24h tokens.

        Counts actual pump.fun `create` / `create_v2` instructions in the last
        24h, excluding the current mint. Incomplete scans (RPC page/tx limits)
        are treated as unknown: log and do not auto-skip unless strict.
        """
        assert token.creator is not None
        creator = str(token.creator)
        mint = str(token.mint)
        seen = self._session_creates.setdefault(creator, set())
        seen.add(mint)
        session_other = len(seen) - 1
        if session_other > self.cfg.max_creator_launches_24h:
            return (
                f"serial_launcher: creator has {session_other} pump.fun creates "
                f"this session (excl current; max {self.cfg.max_creator_launches_24h})"
            )

        cached = self._serial_cache.get(creator)
        if cached is not None and time.time() - cached[2] < 600:
            launches, incomplete, _ = cached
            # cached count is other-creates from a prior mint; session may add more
            launches = max(launches, session_other)
        else:
            try:
                launches, incomplete = await self._count_creator_launches_24h(token)
            except Exception as exc:  # noqa: BLE001
                if self.cfg.strict:
                    return f"serial launcher check failed: {exc}"
                logger.warning(
                    f"serial launcher check failed for {token.symbol}: {exc} "
                    f"(treating as unknown)"
                )
                return None
            launches = max(launches, session_other)
            self._serial_cache[creator] = (launches, incomplete, time.time())

        if launches > self.cfg.max_creator_launches_24h:
            return (
                f"serial_launcher: creator has {launches} pump.fun creates "
                f"in 24h (excl current; max {self.cfg.max_creator_launches_24h})"
            )
        if incomplete:
            logger.warning(
                f"serial launcher incomplete for {token.symbol}: saw "
                f"{launches} creates before RPC/tx budget; treating as unknown"
            )
            if self.cfg.strict:
                return (
                    f"serial launcher check incomplete: {launches} creates "
                    f"seen before budget"
                )
        else:
            logger.info(
                f"serial launcher ok for {token.symbol}: {launches} other "
                f"creates in 24h"
            )
        return None

    async def _count_creator_launches_24h(
        self, token: TokenInfo
    ) -> tuple[int, bool]:
        """Return (other_create_count, incomplete).

        Uses raw JSON-RPC so we can request maxSupportedTransactionVersion=1
        (public RPCs reject v0 for many pump.fun creates).
        """
        assert token.creator is not None
        now = time.time()
        current_mint = str(token.mint)
        creator = str(token.creator)
        recent_sigs: list[dict[str, Any]] = []
        before: str | None = None
        incomplete = False

        while len(recent_sigs) < _SERIAL_MAX_SIGS_24H:
            opts: dict[str, Any] = {"limit": _SERIAL_SIG_PAGE}
            if before is not None:
                opts["before"] = before
            resp = await self.client.post_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [creator, opts],
                }
            )
            batch = (resp or {}).get("result") or []
            if not batch:
                break
            stop_paging = False
            for s in batch:
                bt = s.get("blockTime")
                if bt is None:
                    recent_sigs.append(s)
                    continue
                if now - float(bt) > 86400:
                    stop_paging = True
                    break
                recent_sigs.append(s)
                if len(recent_sigs) >= _SERIAL_MAX_SIGS_24H:
                    incomplete = True
                    stop_paging = True
                    break
            if stop_paging:
                break
            before = batch[-1]["signature"]
            if len(batch) < _SERIAL_SIG_PAGE:
                break
        else:
            incomplete = True

        launches = 0
        fetched = 0
        deadline = time.monotonic() + 12.0  # wall budget for public RPC
        rate_limit_stops = 0
        for s in recent_sigs:
            if launches > self.cfg.max_creator_launches_24h:
                break
            if fetched >= _SERIAL_MAX_TX_FETCHES or time.monotonic() >= deadline:
                incomplete = True
                break
            fetched += 1
            sig = s["signature"]
            try:
                resp = await self.client.post_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            sig,
                            {
                                "encoding": "json",
                                "commitment": "confirmed",
                                "maxSupportedTransactionVersion": 1,
                            },
                        ],
                    },
                    max_retries=2,
                    max_429_retries=2,
                    deadline_seconds=6.0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"serial tx fetch failed {sig}: {exc}")
                incomplete = True
                rate_limit_stops += 1
                if rate_limit_stops >= 2:
                    break
                continue
            if resp is None:
                incomplete = True
                rate_limit_stops += 1
                if rate_limit_stops >= 2:
                    break
                continue
            val = resp.get("result")
            if not val:
                # Missing/pruned — skip this sig, do not burn the whole check
                continue
            for mint in _pump_create_mints_from_rpc_tx(val):
                if mint == current_mint:
                    continue
                launches += 1
            await asyncio.sleep(0.05)

        return launches, incomplete


def _pump_create_mints_from_rpc_tx(tx_result: dict[str, Any]) -> list[str]:
    """Extract mint addresses from pump.fun create/create_v2 in a raw RPC tx."""
    import base58

    try:
        message = tx_result["transaction"]["message"]
        meta = tx_result.get("meta") or {}
    except Exception:
        return []

    def _key_str(k: Any) -> str:
        if isinstance(k, str):
            return k
        if isinstance(k, dict):
            return str(k.get("pubkey") or k)
        return str(k)

    keys = [_key_str(k) for k in (message.get("accountKeys") or [])]
    loaded = meta.get("loadedAddresses") or {}
    keys.extend(_key_str(x) for x in (loaded.get("writable") or []))
    keys.extend(_key_str(x) for x in (loaded.get("readonly") or []))

    pump = str(PUMP_PROGRAM)
    mints: list[str] = []

    def _consider(ix: dict[str, Any]) -> None:
        try:
            pid_index = int(ix["programIdIndex"])
            if pid_index >= len(keys) or keys[pid_index] != pump:
                return
            data_raw = ix.get("data")
            if not data_raw:
                return
            data = base58.b58decode(data_raw)
            if len(data) < 8 or data[:8] not in _CREATE_DISCS:
                return
            acct_idxs = ix.get("accounts") or []
            if not acct_idxs:
                return
            mint_idx = int(acct_idxs[0])
            if mint_idx < len(keys):
                mints.append(keys[mint_idx])
        except Exception:
            return

    for ix in message.get("instructions") or []:
        _consider(ix)
    for inner in meta.get("innerInstructions") or []:
        for ix in inner.get("instructions") or []:
            _consider(ix)
    return mints


def _looks_original(name: str) -> bool:
    """Allow names that only mention a keyword as part of a longer unique brand."""
    return len(name.strip()) >= 24


def load_entry_filter_config(raw: dict | None) -> EntryFilterConfig:
    """Build EntryFilterConfig from YAML `filters.entry` mapping."""
    raw = raw or {}
    return EntryFilterConfig(
        enabled=bool(raw.get("enabled", True)),
        strict=bool(raw.get("strict", False)),
        require_authorities_revoked=bool(raw.get("require_authorities_revoked", True)),
        observe_seconds=int(raw.get("observe_seconds", 45)),
        min_unique_buyers=int(raw.get("min_unique_buyers", 15)),
        min_net_buy_sol=float(raw.get("min_net_buy_sol", 2.0)),
        require_curve_progress=bool(raw.get("require_curve_progress", True)),
        creator_must_not_sell=bool(raw.get("creator_must_not_sell", True)),
        max_creator_pct=float(raw.get("max_creator_pct", 10.0)),
        max_top10_pct_excl_curve=float(raw.get("max_top10_pct_excl_curve", 35.0)),
        max_creator_launches_24h=int(raw.get("max_creator_launches_24h", 3)),
        skip_copycat_names=bool(raw.get("skip_copycat_names", True)),
        signature_sample=int(raw.get("signature_sample", 40)),
    )
