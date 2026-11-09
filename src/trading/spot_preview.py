# ruff: noqa: TRY003, PLR0913
"""Simulation-first spot plan. It never sets confirmed or submits.

A quote has its own expiry. A buy plan names when the sell must be rebuilt.
If that window has passed, the sell is not resent. A Phantom preview is
accepted only when it matches the unsigned request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from trading.phantom_executor import (
    FIXTURE_MINT,
    BalanceSnapshot,
    ExecutorLimits,
    FxQuote,
    PolicyError,
    accept_external_result,
    as_oslo,
    build_buy_request,
    build_sell_request,
)

if TYPE_CHECKING:
    from pathlib import Path

    from solders.hash import Hash


@dataclass(frozen=True)
class SpotQuote:
    """Caller-supplied spot quote. Not a perpetual price and not a market promise."""

    mint: str
    side: str
    input_lamports: int
    minimum_out: int
    source: str
    observed_at: datetime
    expires_at: datetime


def require_fresh_quote(quote: SpotQuote, now: datetime) -> None:
    """Refuse a quote that is not a fresh spot buy of the labeled fixture."""
    if quote.side != "buy":
        raise PolicyError("only a buy quote can open the one position")
    if quote.source != "caller_fixture" or quote.mint != FIXTURE_MINT:
        raise PolicyError("a live token quote is not wired; pass the labeled fixture")
    if quote.input_lamports <= 0 or quote.minimum_out <= 0:
        raise PolicyError("quote amounts must be positive")
    current = as_oslo(now)
    if as_oslo(quote.observed_at) > current:
        raise PolicyError("quote timestamp is in the future")
    if current >= as_oslo(quote.expires_at):
        raise PolicyError("quote expired; fetch a new quote before building")


def build_spot_preview(
    limits: ExecutorLimits,
    *,
    quote: SpotQuote,
    blockhash: Hash,
    blockhash_source: str,
    signal_id: str,
    fx: FxQuote,
    balance: BalanceSnapshot,
    now: datetime,
    state_path: Path,
    sell_within: timedelta,
) -> dict:
    """Unsigned buy plus the sell deadline. confirmed stays false."""
    if sell_within.total_seconds() <= 0:
        raise PolicyError("sell window must be positive")
    require_fresh_quote(quote, now)
    sell_by = as_oslo(now) + sell_within
    document = build_buy_request(
        limits,
        quote_lamports=quote.input_lamports,
        minimum_tokens_out=quote.minimum_out,
        blockhash=blockhash,
        blockhash_source=blockhash_source,
        signal_id=signal_id,
        fx=fx,
        balance=balance,
        now=now,
        state_path=state_path,
    )
    document["sell_by"] = sell_by.isoformat()
    document["on_sell_failure"] = "keep_open_no_resubmit"
    document["next_step"] = (
        "Parent runs solana_send with confirmed omitted. "
        "A later confirmed true is a separate user approval, not this file."
    )
    return document


def build_planned_sell(
    limits: ExecutorLimits,
    *,
    buy: dict,
    token_amount_in: int,
    blockhash: Hash,
    blockhash_source: str,
    signal_id: str,
    fx: FxQuote,
    balance: BalanceSnapshot,
    now: datetime,
    state_path: Path,
    minimum_quote_out: int,
) -> dict:
    """Unsigned sell for the open preview. Refuses an expired sell window."""
    sell_by = buy.get("sell_by")
    if not sell_by:
        raise PolicyError("buy plan has no sell deadline")
    if as_oslo(now) >= as_oslo(datetime.fromisoformat(str(sell_by))):
        raise PolicyError("sell window expired; do not resubmit")
    document = build_sell_request(
        limits,
        token_amount_in=token_amount_in,
        minimum_quote_out=minimum_quote_out,
        blockhash=blockhash,
        blockhash_source=blockhash_source,
        signal_id=signal_id,
        fx=fx,
        balance=balance,
        now=now,
        state_path=state_path,
    )
    document["sell_by"] = sell_by
    document["on_sell_failure"] = "keep_open_no_resubmit"
    return document


def reconcile_preview(
    request: dict,
    result: dict,
    *,
    now: datetime,
    state_path: Path,
    max_age_seconds: int,
) -> dict:
    """Accept a matching Phantom preview. A broadcast or a late sell is refused."""
    if request.get("kind") == "sell" and request.get("sell_by"):
        if as_oslo(now) >= as_oslo(datetime.fromisoformat(str(request["sell_by"]))):
            raise PolicyError("sell window expired; do not resubmit")
    decision = accept_external_result(
        request,
        result,
        now=now,
        state_path=state_path,
        max_age_seconds=max_age_seconds,
    )
    decision["broadcast_by_this_tool"] = False
    return decision
