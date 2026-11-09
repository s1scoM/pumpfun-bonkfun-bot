"""A spot preview expires, reconciles a simulation, and does not resubmit a late sell.

Usage:
    uv run tests/regression/verify_spot_preview.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import _check  # noqa: E402
from solders.hash import Hash  # noqa: E402

from trading.phantom_executor import (  # noqa: E402
    FIXTURE_MINT,
    BalanceSnapshot,
    ExecutorLimits,
    FxQuote,
    PolicyError,
)
from trading.spot_preview import (  # noqa: E402
    SpotQuote,
    build_planned_sell,
    build_spot_preview,
    reconcile_preview,
)

_NOW = datetime.fromisoformat("2026-11-09T12:00:00+01:00")


def _limits() -> ExecutorLimits:
    return ExecutorLimits(
        fee_payer="8rRjKEhJYoYJmG3AVz64xq1cj9LUpqr77j9My5T26BDj",
        stop=False,
        live=False,
        submit_enabled=False,
    )


def _quote(expires_at: datetime) -> SpotQuote:
    return SpotQuote(
        mint=FIXTURE_MINT,
        side="buy",
        input_lamports=1_000,
        minimum_out=1,
        source="caller_fixture",
        observed_at=_NOW,
        expires_at=expires_at,
    )


def _result(document: dict) -> dict:
    return {
        "schema": "phantom-executor-result/1",
        "request_id": document["request_id"],
        "transaction": document["transaction"],
        "confirmed": False,
        "submitted": False,
        "simulated": True,
        "blockhash": document["blockhash"],
        "err": None,
        "observed_at": document["built_at"],
        "attempt": 1,
    }


def main() -> None:
    """Exit non-zero when the preview plan submits or ignores an expiry."""
    print("SPOT PREVIEW (fixture quote, not submitted)")
    problems: list[str] = []
    state = PROJECT_ROOT / "phantom-requests" / "spot-preview" / "state.json"
    if state.exists():
        state.unlink()
    limits = _limits()
    fx = FxQuote(820.0, _NOW, "fixture")
    balance = BalanceSnapshot(223_224_000, _NOW, "fixture")
    try:
        build_spot_preview(
            limits,
            quote=_quote(_NOW - timedelta(seconds=1)),
            blockhash=Hash.default(),
            blockhash_source="fixture",
            signal_id="fixture-signal",
            fx=fx,
            balance=balance,
            now=_NOW,
            state_path=state,
            sell_within=timedelta(seconds=120),
        )
        stale = False
    except PolicyError:
        stale = True
    if not _check("expired quote is refused", stale, "fetch a new quote"):
        problems.append("stale quote")

    buy = build_spot_preview(
        limits,
        quote=_quote(_NOW + timedelta(seconds=60)),
        blockhash=Hash.default(),
        blockhash_source="fixture",
        signal_id="fixture-signal",
        fx=fx,
        balance=balance,
        now=_NOW,
        state_path=state,
        sell_within=timedelta(seconds=120),
    )
    if not _check(
        "preview stays unsubmitted and names a sell deadline",
        buy["confirmed"] is False
        and buy["submit"] is False
        and buy["on_sell_failure"] == "keep_open_no_resubmit"
        and "sell_by" in buy,
        buy["label"],
    ):
        problems.append("preview")
    decision = reconcile_preview(
        buy, _result(buy), now=_NOW, state_path=state, max_age_seconds=60
    )
    if not _check(
        "simulation receipt is reconciled without broadcast",
        decision["accepted"] is True and decision["broadcast_by_this_tool"] is False,
        decision["request_id"][:12],
    ):
        problems.append("reconcile")
    try:
        build_planned_sell(
            limits,
            buy=buy,
            token_amount_in=1,
            blockhash=Hash.default(),
            blockhash_source="fixture",
            signal_id="fixture-signal",
            fx=fx,
            balance=balance,
            now=_NOW + timedelta(seconds=121),
            state_path=state,
            minimum_quote_out=1,
        )
        late = False
    except PolicyError:
        late = True
    if not _check("expired sell is not rebuilt", late, "sell window"):
        problems.append("late sell")
    if problems:
        print(f"FAILED {len(problems)} check(s)")
        sys.exit(1)
    print("HARNESS PASS")
    print("live_submit: False")


if __name__ == "__main__":
    main()
