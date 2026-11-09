"""Adopt coins the wallet holds that no position record covers.

A buy can land with nothing left on disk to monitor it: the process was
killed or the machine was snapshotted/restored between the buy and the
position record, or the position file was lost. The coins then sit in the
wallet with no stop loss watching them. At each start the trader reads the
wallet's token accounts, and for every non-empty one that is not in the
positions file and is a pump.fun / PumpSwap coin, rebuilds the position from
the wallet's own buy transactions of it (cost, amount, time) and hands it to
the position manager like a resumed one: the usual take profit, stop loss and
hold-time rules then apply, and the sale goes through the usual sell path.

A coin worth less than `min_value_sol` is dust and left alone. A coin whose buy cannot be found in the wallet's history (an airdrop, a
transfer) is left alone and logged: its cost is unknown and it may be junk.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime

from solders.pubkey import Pubkey

from core.trade_deltas import TradeDeltas
from interfaces.core import Platform, TokenInfo
from trading.base import TradeResult
from trading.momentum import MomentumConfig, MomentumScanner
from utils.logger import get_logger

logger = get_logger(__name__)

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


@dataclass
class Orphan:
    """A held coin and the position rebuilt for it."""

    token_info: TokenInfo
    buy_result: TradeResult
    entry_time: datetime  # naive UTC, like Position.entry_time
    token_account: str


def parse_token_account(data: bytes) -> tuple[Pubkey, int] | None:
    """(mint, raw amount) of a token account's data, None if too short."""
    if len(data) < 72:
        return None
    return Pubkey.from_bytes(data[0:32]), struct.unpack_from("<Q", data, 64)[0]


def combine_buys(buys: list[TradeDeltas]) -> TradeDeltas:
    """One `TradeDeltas` for several buys of the same coin, summed."""
    quotes = [b.quote_raw for b in buys if b.quote_raw is not None]
    venue = [b.venue_quote_raw for b in buys if b.venue_quote_raw is not None]
    return TradeDeltas(
        tokens_raw=sum(b.tokens_raw for b in buys),
        quote_raw=sum(quotes) if quotes else None,
        network_fee_lamports=sum(b.network_fee_lamports for b in buys),
        rent_deposited_lamports=sum(b.rent_deposited_lamports for b in buys),
        rent_refunded_lamports=sum(b.rent_refunded_lamports for b in buys),
        other_lamports=sum(b.other_lamports for b in buys),
        venue_quote_raw=sum(venue) if venue else None,
    )


async def _held_coins(client, wallet: Pubkey, known: set[str]) -> list[tuple[Pubkey, int, str]]:
    """Non-empty token accounts of the wallet that the store does not cover."""
    held = []
    for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        for address, data in await client.get_token_accounts(wallet, program):
            parsed = parse_token_account(data)
            if not parsed:
                continue
            mint, amount = parsed
            if amount > 0 and str(mint) not in known and str(mint) not in IGNORED_MINTS:
                held.append((mint, amount, str(address)))
    return held


async def _buys_from_history(client, wallet: Pubkey, account: str, token_info: TokenInfo,
                             history: int) -> tuple[list[TradeDeltas], list[str], float | None]:
    """The wallet's buys of the coin: deltas, signatures, first block time."""
    response = await client.post_rpc({
        "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
        "params": [account, {"limit": history, "commitment": "confirmed"}],
    })
    sigs = [s for s in ((response or {}).get("result") or []) if not s.get("err")]
    buys, signatures, first_time = [], [], None
    for entry in reversed(sigs):  # oldest first
        deltas = await client.get_trade_deltas(
            entry["signature"], wallet, token_info.mint, token_info.quote_mint
        )
        if deltas is not None and deltas.tokens_raw > 0:
            buys.append(deltas)
            signatures.append(entry["signature"])
            if first_time is None:
                first_time = entry.get("blockTime")
    return buys, signatures, first_time


async def find_orphans(client, wallet: Pubkey, platform: Platform, known: set[str], *,
                       history: int = 25, max_coins: int = 5,
                       min_value_sol: float = 0.003) -> list[Orphan]:
    """Rebuild a position for each held pump.fun coin that is not in `known`.

    Never raises for one coin: a coin that cannot be read or priced is logged
    and left alone. The wallet's token accounts failing to read raises.
    """
    resolver = MomentumScanner(client, MomentumConfig(min_curve_real_sol=0.0), platform)
    found: list[Orphan] = []
    try:
        for mint, held_raw, account in (await _held_coins(client, wallet, known))[:max_coins * 4]:
            if len(found) >= max_coins:
                break
            try:
                token_info = await resolver.resolve(str(mint))
            except Exception as e:  # noqa: BLE001 - not a pump.fun coin, or unreadable
                logger.debug(f"ORPHAN scan: {mint} is not a tradable pump.fun coin: {e!s}")
                continue
            now_price = (token_info.additional_data or {}).get("onchain_price_sol")
            if now_price is not None and held_raw / 1e6 * now_price < min_value_sol:
                logger.info(
                    f"ORPHAN scan: {token_info.symbol} ({mint}) is worth "
                    f"{held_raw / 1e6 * now_price:.6f} SOL, under {min_value_sol}; left alone"
                )
                continue
            try:
                buys, sigs, first_time = await _buys_from_history(
                    client, wallet, account, token_info, history
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"ORPHAN scan: could not read the history of {mint}: {e!s}")
                continue
            if not buys:
                logger.warning(
                    f"ORPHAN scan: the wallet holds {held_raw} raw of {mint} "
                    f"({token_info.symbol}) but no buy of it is in its history; left alone"
                )
                continue
            deltas = combine_buys(buys)
            spent = (-deltas.quote_raw + deltas.network_fee_lamports) if (
                deltas.quote_raw is not None and deltas.quote_raw < 0) else None
            bought = deltas.tokens_raw / 1e6
            price = spent / 1e9 / bought if spent else None
            held = held_raw
            buy = TradeResult(
                success=True, platform=platform, tx_signature=sigs[0],
                amount=held / 1e6, amount_raw=held, price=price, deltas=deltas,
            )
            entry = datetime.utcfromtimestamp(first_time) if first_time else datetime.utcnow()
            found.append(Orphan(token_info, buy, entry, account))
            logger.warning(
                f"ORPHAN found: {token_info.symbol} {mint} holds {held / 1e6:.2f} tokens, "
                f"bought {len(buys)}x from {entry.isoformat()} UTC at "
                f"{price if price is None else format(price, '.4g')} SOL/token "
                f"(buy {sigs[0][:16]})"
            )
    finally:
        await resolver.close()
    return found
