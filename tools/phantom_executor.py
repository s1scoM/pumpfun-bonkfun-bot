# ruff: noqa: C901, PLR0912, PLR0915
"""Build an unsigned Phantom simulation request. Does not start pump_bot.

The shipped config keeps stop set and submit off. Buy, sell and close then
refuse. `request self-transfer` spends 0 lamports and is not a token trade.
`--read-rpc` may call getLatestBlockhash or getBalance on the public mainnet
RPC. It never calls sendTransaction.

Usage:
    uv run tools/phantom_executor.py check
    uv run tools/phantom_executor.py request self-transfer
    uv run tools/phantom_executor.py request self-transfer --read-rpc
    uv run tools/phantom_executor.py request buy --fixture --fx 820
    uv run tools/phantom_executor.py accept-result request.json result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from solders.hash import Hash

from trading.gmgn_read import GmgnReadOnly
from trading.phantom_executor import (
    OSLO,
    BalanceSnapshot,
    FxQuote,
    PolicyError,
    accept_external_result,
    blockhash_from_rpc,
    build_buy_request,
    build_close_request,
    build_self_transfer_request,
    build_sell_request,
    load_executor_config,
    write_request,
)

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG = _ROOT / "bots" / "phantom-executor.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phantom_executor",
        description=(
            "Write an unsigned solana_send simulation request. "
            "This process does not sign or submit."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help="executor YAML. A file with private_key is refused.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="show the switches and exit")
    request = sub.add_parser("request", help="write one unsigned request")
    request.add_argument(
        "kind", choices=("buy", "sell", "close", "self-transfer")
    )
    request.add_argument("--fixture", action="store_true")
    request.add_argument("--fx", type=float, default=None)
    request.add_argument("--signal", default="fixture-signal")
    request.add_argument("--quote-lamports", type=int, default=1_000)
    request.add_argument("--min-tokens", type=int, default=1)
    request.add_argument("--token-amount", type=int, default=1)
    request.add_argument("--min-quote", type=int, default=1)
    request.add_argument("--read-rpc", action="store_true")
    request.add_argument(
        "--state",
        type=Path,
        default=_ROOT / "phantom-requests" / "state.json",
    )
    request.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output JSON. Default phantom-requests/<kind>.json",
    )
    accept = sub.add_parser(
        "accept-result", help="check a simulation result against a request"
    )
    accept.add_argument("request", type=Path)
    accept.add_argument("result", type=Path)
    accept.add_argument(
        "--state",
        type=Path,
        default=_ROOT / "phantom-requests" / "state.json",
    )
    return parser


def _die(message: str) -> None:
    print(f"phantom_executor: {message}", file=sys.stderr)
    raise SystemExit(1)


def _now() -> datetime:
    return datetime.now(OSLO)


def _fixture_inputs(
    fx_rate: float | None, now: datetime
) -> tuple[FxQuote, BalanceSnapshot, Hash, str]:
    if fx_rate is None:
        _die("fixture buy, sell and close require --fx NOK per SOL")
    fx = FxQuote(nok_per_sol=fx_rate, observed_at=now, source="fixture")
    balance = BalanceSnapshot(
        lamports=223_224_000, observed_at=now, source="fixture"
    )
    return fx, balance, Hash.default(), "fixture"


def _write(kind: str, document: dict, out: Path | None) -> None:
    target = out or (_ROOT / "phantom-requests" / f"{kind}.json")
    write_request(target, document)
    print(f"wrote {target}")
    print(
        f"kind={document['kind']} confirmed={document['confirmed']} "
        f"submit={document['submit']} blockhash_source={document['blockhash_source']}"
    )


def main(argv: list[str] | None = None) -> None:
    """Run one offline command. Exit 1 on a policy refusal."""
    args = _parser().parse_args(argv)
    try:
        limits = load_executor_config(args.config)
    except (OSError, PolicyError, ValueError) as exc:
        _die(str(exc))
    if args.command == "check":
        gmgn = GmgnReadOnly(enabled=limits.gmgn_read)
        print(f"config={args.config}")
        print(f"fee_payer={limits.fee_payer}")
        print(f"enabled_for_pump_bot=false submit={limits.submit_enabled}")
        print(f"live={limits.live} stop={limits.stop} gmgn_read={gmgn.enabled}")
        print("pump_bot is not started. solana_send is not called.")
        return
    now = _now()
    state = args.state
    if args.command == "request":
        if args.read_rpc and args.kind != "self-transfer":
            _die("--read-rpc is only implemented for self-transfer")
        if args.kind == "self-transfer":
            if args.read_rpc:
                try:
                    blockhash, source = blockhash_from_rpc()
                except (OSError, PolicyError, KeyError, ValueError) as exc:
                    _die(f"read-only RPC failed: {exc}")
            else:
                blockhash, source = Hash.default(), "fixture"
            document = build_self_transfer_request(
                limits, blockhash=blockhash, blockhash_source=source, now=now
            )
            _write("self-transfer", document, args.out)
            return
        if not args.fixture:
            _die("buy, sell and close require --fixture. Live quotes are not wired.")
        if limits.live or limits.submit_enabled:
            _die("fixture requests require live false and submit false")
        fx, balance, blockhash, source = _fixture_inputs(args.fx, now)
        trading = limits.__class__(
            **{**limits.__dict__, "stop": False, "live": False, "submit_enabled": False}
        )
        try:
            if args.kind == "buy":
                document = build_buy_request(
                    trading,
                    quote_lamports=args.quote_lamports,
                    minimum_tokens_out=args.min_tokens,
                    blockhash=blockhash,
                    blockhash_source=source,
                    signal_id=args.signal,
                    fx=fx,
                    balance=balance,
                    now=now,
                    state_path=state,
                )
            elif args.kind == "sell":
                document = build_sell_request(
                    trading,
                    token_amount_in=args.token_amount,
                    minimum_quote_out=args.min_quote,
                    blockhash=blockhash,
                    blockhash_source=source,
                    signal_id=args.signal,
                    fx=fx,
                    balance=balance,
                    now=now,
                    state_path=state,
                )
            else:
                document = build_close_request(
                    trading,
                    blockhash=blockhash,
                    blockhash_source=source,
                    signal_id=args.signal,
                    fx=fx,
                    balance=balance,
                    now=now,
                    state_path=state,
                )
        except PolicyError as exc:
            _die(str(exc))
        _write(args.kind, document, args.out)
        return
    request = json.loads(args.request.read_text())
    result = json.loads(args.result.read_text())
    try:
        decision = accept_external_result(
            request,
            result,
            now=now,
            state_path=args.state,
            max_age_seconds=int(request.get("max_age_seconds", 60)),
        )
    except (PolicyError, KeyError, ValueError) as exc:
        _die(str(exc))
    print(json.dumps(decision))


if __name__ == "__main__":
    main()
