"""Momentum entry: buy pump.fun coins that are already moving up, not new launches.

Instead of the launch listener, `MomentumScanner` polls free public market data
and hands the trader one coin that passes every check:

1. Discovery (which coins to look at): GeckoTerminal's Solana trending pools
   (5-minute window, pages 1-2) plus DexScreener's latest token boosts. Only
   coins that trade on the pump.fun curve (`pump-fun`) or in their PumpSwap
   pool (`pumpswap`) are kept, since those are the venues this bot can buy and
   sell through.
2. Verification (is it really moving now): DexScreener `tokens/v1` is read for
   every kept mint, up to 30 per call, and the coin's pump.fun/PumpSwap SOL
   pair is scored against the thresholds in `MomentumConfig`.
3. Confirmation: a coin has to pass on `confirm_polls` consecutive polls, and
   with `require_rising_on_confirm` its price must not have fallen since the
   previous pass, so a single spike does not trigger a buy.
4. Resolution: the coin's curve, mint and metadata are read on chain and turned
   into a `TokenInfo`. The buyer then trades it on the curve, or in the PumpSwap
   pool when the curve has graduated, as it does for any pump.fun coin. A
   curve coin must also hold `min_curve_real_sol` SOL in its curve.

Coins bought in momentum mode are written to `traded_file` with the day, and are
skipped for the rest of that (UTC) day, across restarts.

Nothing here signs or sends: the trader's existing risk guard, dry-run switch,
buyer and exit logic are used unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
from solders.pubkey import Pubkey

from core.pubkeys import WSOL_MINT, cached_quote_token_program, normalize_quote_mint
from core.token_metadata import decode_token_metadata, metadata_address
from interfaces.core import Platform, TokenInfo
from platforms import get_platform_implementations
from platforms.pumpfun.pumpswap_market import canonical_pool_address
from utils.logger import get_logger

logger = get_logger(__name__)

GT_TRENDING = (
    "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
    "?duration=5m&page={page}"
)
DS_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
DS_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
WSOL = str(WSOL_MINT)
_GT_DEXES = {"pump-fun", "pumpswap"}


@dataclass
class MomentumConfig:
    """Thresholds for `entry_mode: momentum`, read from `filters.momentum`.

    Percentages are in percent (15 is +15%), money in USD from DexScreener.
    """

    poll_seconds: float = 20.0
    # Price change over the last 5 minutes: at least this much ...
    min_m5_change_pct: float = 15.0
    # ... but not more: a coin already up this much in 5 min is usually a top.
    max_m5_change_pct: float = 150.0
    # The last hour must be up too (strictly above this).
    min_h1_change_pct: float = 0.0
    # Real buying in the last 5 minutes.
    min_m5_buys: int = 40
    min_buy_sell_ratio: float = 1.3
    min_m5_volume_usd: float = 3000.0
    # Volume per trade: filters wash bots spamming tiny trades.
    min_avg_trade_usd: float = 5.0
    # PumpSwap pool liquidity (DexScreener does not report it for curve pairs).
    min_liquidity_usd: float = 10000.0
    # SOL a curve coin must hold in its curve (on-chain check at resolution).
    min_curve_real_sol: float = 10.0
    # Pair age from DexScreener pairCreatedAt (for PumpSwap: since migration).
    min_pair_age_minutes: float = 10.0
    max_pair_age_hours: float | None = None
    # Skip when the on-chain price is this much above DexScreener's snapshot:
    # the coin ran further since, and buying it is chasing.
    max_onchain_premium_pct: float = 15.0
    confirm_polls: int = 2
    require_rising_on_confirm: bool = True
    allowed_dex_ids: list[str] = field(default_factory=lambda: ["pumpfun", "pumpswap"])
    use_boosts: bool = True
    gt_pages: int = 2
    skip_traded_today: bool = True
    traded_file: str = "positions/momentum-traded.json"
    http_timeout: float = 10.0


def load_momentum_config(raw: dict | None) -> MomentumConfig:
    """Build a `MomentumConfig` from `filters.momentum`; unknown keys are ignored."""
    raw = dict(raw or {})
    known = {f.name for f in fields(MomentumConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        logger.warning(f"filters.momentum: ignoring unknown keys {unknown}")
    return MomentumConfig(**{k: v for k, v in raw.items() if k in known})


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def evaluate_pair(pair: dict, cfg: MomentumConfig, now_ms: float | None = None) -> str | None:
    """Why a DexScreener pair fails the momentum thresholds, or None if it passes."""
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    dex = pair.get("dexId")
    if dex not in cfg.allowed_dex_ids:
        return f"dex {dex} not tradable"
    if (pair.get("quoteToken") or {}).get("address") != WSOL:
        return "not SOL-quoted"
    change = pair.get("priceChange") or {}
    m5, h1 = _num(change.get("m5")), _num(change.get("h1"))
    if m5 < cfg.min_m5_change_pct:
        return f"m5 {m5:+.1f}% < {cfg.min_m5_change_pct}%"
    if m5 > cfg.max_m5_change_pct:
        return f"m5 {m5:+.1f}% > {cfg.max_m5_change_pct}% (blow-off)"
    if h1 <= cfg.min_h1_change_pct:
        return f"h1 {h1:+.1f}% not above {cfg.min_h1_change_pct}%"
    tx = (pair.get("txns") or {}).get("m5") or {}
    buys, sells = int(_num(tx.get("buys"))), int(_num(tx.get("sells")))
    if buys < cfg.min_m5_buys:
        return f"m5 buys {buys} < {cfg.min_m5_buys}"
    if buys < cfg.min_buy_sell_ratio * max(sells, 1):
        return f"buys/sells {buys}/{sells} below {cfg.min_buy_sell_ratio}x"
    vol = _num((pair.get("volume") or {}).get("m5"))
    if vol < cfg.min_m5_volume_usd:
        return f"m5 volume ${vol:,.0f} < ${cfg.min_m5_volume_usd:,.0f}"
    avg = vol / max(buys + sells, 1)
    if avg < cfg.min_avg_trade_usd:
        return f"avg trade ${avg:.2f} < ${cfg.min_avg_trade_usd} (wash-like)"
    if dex != "pumpfun":
        liq = _num((pair.get("liquidity") or {}).get("usd"))
        if liq < cfg.min_liquidity_usd:
            return f"liquidity ${liq:,.0f} < ${cfg.min_liquidity_usd:,.0f}"
    created = pair.get("pairCreatedAt")
    if not created:
        return "pair age unknown"
    age_min = (now_ms - float(created)) / 60000
    if age_min < cfg.min_pair_age_minutes:
        return f"age {age_min:.1f}m < {cfg.min_pair_age_minutes}m"
    if cfg.max_pair_age_hours is not None and age_min > cfg.max_pair_age_hours * 60:
        return f"age {age_min / 60:.1f}h > {cfg.max_pair_age_hours}h"
    return None


def pair_summary(pair: dict) -> dict:
    """The numbers a momentum decision was made on, for logs and reports."""
    change = pair.get("priceChange") or {}
    tx = (pair.get("txns") or {}).get("m5") or {}
    return {
        "symbol": (pair.get("baseToken") or {}).get("symbol"),
        "mint": (pair.get("baseToken") or {}).get("address"),
        "dex": pair.get("dexId"),
        "pair": pair.get("pairAddress"),
        "price_sol": _num(pair.get("priceNative")),
        "price_usd": _num(pair.get("priceUsd")),
        "m5": _num(change.get("m5")),
        "h1": _num(change.get("h1")),
        "buys_m5": int(_num(tx.get("buys"))),
        "sells_m5": int(_num(tx.get("sells"))),
        "vol_m5": _num((pair.get("volume") or {}).get("m5")),
        "liq_usd": _num((pair.get("liquidity") or {}).get("usd")),
        "mcap": _num(pair.get("marketCap")),
        "age_min": round((time.time() * 1000 - _num(pair.get("pairCreatedAt"))) / 60000, 1)
        if pair.get("pairCreatedAt")
        else None,
    }


class TradedToday:
    """Mints bought in momentum mode, per UTC day, kept on disk across restarts."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"day": _today(), "mints": []}
        if data.get("day") != _today():
            return {"day": _today(), "mints": []}
        return data

    def contains(self, mint: str) -> bool:
        return mint in self._load().get("mints", [])

    def add(self, mint: str) -> None:
        data = self._load()
        if mint not in data["mints"]:
            data["mints"].append(mint)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.path)


class MomentumScanner:
    """Polls public market data and returns one coin worth buying on momentum."""

    def __init__(self, client, cfg: MomentumConfig, platform: Platform = Platform.PUMP_FUN) -> None:
        self.client = client
        self.cfg = cfg
        self.platform = platform
        self.traded = TradedToday(cfg.traded_file)
        # mint -> (consecutive passes, price_sol at last pass)
        self._streak: dict[str, tuple[int, float]] = {}
        self._rejected: set[str] = set()  # failed on-chain resolution this run
        self._backoff_until: dict[str, float] = {}
        self._boosts_at = 0.0
        self._boost_mints: list[str] = []
        self._session: aiohttp.ClientSession | None = None
        self.last_poll: list[dict] = []

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, source: str, url: str) -> Any:
        if time.monotonic() < self._backoff_until.get(source, 0):
            return None
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.cfg.http_timeout),
                headers={"Accept": "application/json", "User-Agent": "momo-momentum/1.0"},
            )
        try:
            async with self._session.get(url) as resp:
                if resp.status == 429:
                    self._backoff_until[source] = time.monotonic() + 60
                    logger.warning(f"MOMENTUM {source} rate-limited (429); backing off 60s")
                    return None
                if resp.status != 200:
                    logger.warning(f"MOMENTUM {source} HTTP {resp.status}")
                    return None
                return await resp.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MOMENTUM {source} request failed: {e!s}")
            return None

    async def discover(self) -> list[str]:
        """Candidate pump.fun mints from trending pools and recent boosts."""
        mints: list[str] = []
        for page in range(1, self.cfg.gt_pages + 1):
            data = await self._get("geckoterminal", GT_TRENDING.format(page=page))
            for pool in (data or {}).get("data", []) or []:
                rel = pool.get("relationships") or {}
                dex = ((rel.get("dex") or {}).get("data") or {}).get("id")
                base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
                if dex in _GT_DEXES and base.startswith("solana_"):
                    mints.append(base.split("_", 1)[1])
        if self.cfg.use_boosts:
            if time.monotonic() - self._boosts_at >= 60:
                data = await self._get("dexscreener-boosts", DS_BOOSTS)
                if isinstance(data, list):
                    self._boost_mints = [
                        b.get("tokenAddress")
                        for b in data
                        if b.get("chainId") == "solana" and b.get("tokenAddress")
                    ]
                    self._boosts_at = time.monotonic()
            mints.extend(self._boost_mints)
        seen: set[str] = set()
        return [m for m in mints if not (m in seen or seen.add(m))]

    async def fetch_pairs(self, mints: list[str]) -> dict[str, dict]:
        """Best tradable SOL pair per mint from DexScreener (highest liquidity)."""
        best: dict[str, dict] = {}
        for i in range(0, len(mints), 30):
            chunk = mints[i : i + 30]
            data = await self._get("dexscreener", DS_TOKENS.format(mints=",".join(chunk)))
            for pair in data if isinstance(data, list) else []:
                mint = (pair.get("baseToken") or {}).get("address")
                if mint not in chunk or pair.get("chainId") != "solana":
                    continue
                if pair.get("dexId") not in self.cfg.allowed_dex_ids:
                    continue
                if (pair.get("quoteToken") or {}).get("address") != WSOL:
                    continue
                cur = best.get(mint)
                liq = _num((pair.get("liquidity") or {}).get("usd"))
                if cur is None or liq > _num((cur.get("liquidity") or {}).get("usd")):
                    best[mint] = pair
        return best

    async def poll_once(self) -> list[dict]:
        """One poll: score every candidate, return the confirmed picks (best first)."""
        mints = [m for m in await self.discover() if m not in self._rejected]
        if self.cfg.skip_traded_today:
            mints = [m for m in mints if not self.traded.contains(m)]
        pairs = await self.fetch_pairs(mints)
        now_ms = time.time() * 1000
        confirmed: list[dict] = []
        report: list[dict] = []
        passed_now: set[str] = set()
        for mint, pair in pairs.items():
            reason = evaluate_pair(pair, self.cfg, now_ms)
            summary = pair_summary(pair)
            summary["reason"] = reason
            report.append(summary)
            if reason:
                continue
            passed_now.add(mint)
            price = summary["price_sol"]
            streak, last_price = self._streak.get(mint, (0, 0.0))
            if (
                streak
                and self.cfg.require_rising_on_confirm
                and price < last_price
            ):
                streak = 0  # fell since the last pass: start over
            streak += 1
            self._streak[mint] = (streak, price)
            summary["streak"] = streak
            if streak >= self.cfg.confirm_polls:
                confirmed.append(summary)
        for mint in list(self._streak):
            if mint not in passed_now:
                del self._streak[mint]
        self.last_poll = report
        logger.info(
            f"MOMENTUM poll: {len(mints)} candidates, {len(pairs)} with pump.fun/PumpSwap "
            f"SOL pairs, {len(passed_now)} pass, {len(confirmed)} confirmed"
        )
        for s in report:
            if s["reason"] is None:
                logger.info(
                    f"MOMENTUM pass {s['symbol']} {s['mint']} dex={s['dex']} "
                    f"m5={s['m5']:+.1f}% h1={s['h1']:+.1f}% buys/sells={s['buys_m5']}/"
                    f"{s['sells_m5']} vol5m=${s['vol_m5']:,.0f} liq=${s['liq_usd']:,.0f} "
                    f"age={s['age_min']}m streak={s.get('streak')}"
                )
        confirmed.sort(key=lambda s: (s["m5"] * min(s["buys_m5"] / max(s["sells_m5"], 1), 3)), reverse=True)
        return confirmed

    async def resolve(self, mint_str: str, summary: dict | None = None) -> TokenInfo:
        """Read the coin on chain and build the `TokenInfo` the buyer trades.

        Raises:
            ValueError: If the mint or curve cannot be read, the quote asset is
                not SOL, or a curve coin holds less than `min_curve_real_sol`
        """
        mint = Pubkey.from_string(mint_str)
        impl = get_platform_implementations(self.platform, self.client)
        provider = impl.address_provider
        curve_address = provider.derive_pool_address(mint)
        mint_account, metaplex_account, curve_account = (
            await self.client.get_multiple_accounts(
                [mint, metadata_address(mint), curve_address]
            )
        )
        if mint_account is None:
            raise ValueError("mint account not found")  # noqa: TRY003
        if curve_account is None:
            raise ValueError("no pump.fun bonding curve (not a pump.fun coin)")  # noqa: TRY003
        state = await impl.curve_manager.get_pool_state(curve_address)
        quote_mint = normalize_quote_mint(state.get("quote_mint"))
        if quote_mint != WSOL_MINT:
            raise ValueError(f"curve quote {quote_mint} is not SOL")  # noqa: TRY003
        graduated = bool(state.get("graduated"))
        if not graduated:
            real_sol = _num(state.get("real_quote_reserves")) / 1e9
            if real_sol < self.cfg.min_curve_real_sol:
                raise ValueError(  # noqa: TRY003
                    f"curve holds {real_sol:.2f} SOL < {self.cfg.min_curve_real_sol}"
                )
        if graduated and summary and summary.get("pair"):
            canonical = str(canonical_pool_address(mint, WSOL_MINT))
            if summary["pair"] != canonical:
                raise ValueError(  # noqa: TRY003
                    f"DexScreener pair {summary['pair']} is not the canonical "
                    f"PumpSwap pool {canonical}"
                )
        metadata = decode_token_metadata(
            mint_account.data, metaplex_account.data if metaplex_account else None
        )
        name, symbol, uri = metadata or ("", "", "")
        creator_raw = state.get("creator")
        creator = (
            creator_raw
            if isinstance(creator_raw, Pubkey)
            else (Pubkey.from_string(str(creator_raw)) if creator_raw else None)
        )
        token_program = mint_account.owner
        info = TokenInfo(
            name=name,
            symbol=symbol or (summary or {}).get("symbol") or mint_str[:8],
            uri=uri,
            mint=mint,
            platform=self.platform,
            bonding_curve=curve_address,
            associated_bonding_curve=provider.derive_associated_bonding_curve(
                mint, curve_address, token_program
            ),
            creator=creator,
            creator_vault=provider.derive_creator_vault(creator) if creator else None,
            token_program_id=token_program,
            is_mayhem_mode=state.get("is_mayhem_mode", False),
            is_cashback_coin=state.get("is_cashback_coin", False),
            is_holder_reward=state.get("is_holder_reward", False),
            creator_fee_bps=state.get("creator_fee_bps", 0),
            quote_mint=WSOL_MINT,
            quote_token_program_id=cached_quote_token_program(WSOL_MINT),
            creation_timestamp=time.time(),
            additional_data={
                "entry": "momentum",
                "graduated": graduated,
                "momentum": summary or {},
            },
        )
        # Price where the buyer will buy, against the snapshot the pick used.
        market = impl.graduated_market if graduated else impl.curve_market
        onchain = await market.get_price(info)
        info.additional_data["onchain_price_sol"] = onchain
        ds_price = (summary or {}).get("price_sol") or 0
        if ds_price and onchain > ds_price * (1 + self.cfg.max_onchain_premium_pct / 100):
            raise ValueError(  # noqa: TRY003
                f"on-chain price {onchain:.3e} is "
                f"{(onchain / ds_price - 1) * 100:.0f}% above the snapshot {ds_price:.3e} (chasing)"
            )
        return info

    async def wait_for_token(self, timeout: float) -> TokenInfo | None:
        """Poll until a confirmed coin resolves on chain, or `timeout` seconds pass."""
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                started = time.monotonic()
                for pick in await self.poll_once():
                    try:
                        info = await self.resolve(pick["mint"], pick)
                    except Exception as e:  # noqa: BLE001
                        logger.info(f"MOMENTUM skip {pick['symbol']} {pick['mint']}: {e!s}")
                        self._rejected.add(pick["mint"])
                        continue
                    venue = "PumpSwap" if info.additional_data.get("graduated") else "curve"
                    logger.info(
                        f"MOMENTUM PICK {info.symbol} {info.mint} venue={venue} "
                        f"price_sol={pick['price_sol']:.10f} m5={pick['m5']:+.1f}% "
                        f"h1={pick['h1']:+.1f}% buys/sells={pick['buys_m5']}/{pick['sells_m5']}"
                    )
                    return info
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(1.0, self.cfg.poll_seconds - elapsed))
            return None
        finally:
            await self.close()

    def mark_traded(self, mint: str) -> None:
        self.traded.add(mint)
