# ruff: noqa: TRY003, PLR0913, C901, PLR0912, PLR2004, S310
"""Build Phantom `solana_send` requests and check a simulation result.

Buy, sell and close use the pump.fun instruction builder and a public fee
payer. Nothing here signs, calls `solana_send`, or starts `pump_bot`.
`confirmed` is always false. A live submit switch is rejected at load time.

A NOK figure is quote lamports times an explicit FX rate. Token market value
is not an input. Missing FX, a stale blockhash, a second position or a kill
flag fails closed. The synthetic mint used for offline builds is a fixture,
not a signal to trade a newly seen coin.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import yaml
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from spl.token.instructions import close_account, get_associated_token_address
from spl.token.models import CloseAccountParams

from core.pubkeys import SystemAddresses
from core.unsigned_tx import (
    NETWORK_ID,
    PhantomSendEnvelope,
    build_phantom_send_envelope,
)
from interfaces.core import Platform, TokenInfo
from platforms.pumpfun.address_provider import (
    PumpFunAddresses,
    PumpFunAddressProvider,
)
from platforms.pumpfun.instruction_builder import PumpFunInstructionBuilder
from trading.paper_policy import (
    BASE_SIGNATURE_LAMPORTS,
    priority_fee_lamports,
    token_account_rent_lamports,
)
from utils.idl_parser import IDLParser

if TYPE_CHECKING:
    from collections.abc import Callable

    from solders.instruction import Instruction

OSLO = ZoneInfo("Europe/Oslo")
OFFICIAL_RPC = "https://api.mainnet-beta.solana.com"
READ_ONLY_RPC_METHODS = frozenset(
    {"getLatestBlockhash", "getBalance", "getFeeForMessage"}
)
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string(
    "ComputeBudget111111111111111111111111111111"
)
# Synthetic mint and creator already used by the offline layout verifier.
FIXTURE_MINT = "CU7nUQaJ4beyYjC3xAUrh5RiSjw14fhU6oWTwRBse8gj"
FIXTURE_CREATOR = "5wyFsNExysbXf2hTtcn8Tqd3urs9Nv85Zx1zNdAfTMmX"
FEE_PAYER = "8rRjKEhJYoYJmG3AVz64xq1cj9LUpqr77j9My5T26BDj"
RESERVE_FLOOR_LAMPORTS = 8_224_000
MAX_SPEND_LAMPORTS = 215_000_000
CAPITAL_CAP_NOK = 200.0
FRESH_SECONDS = 900
_SYSTEM_TRANSFER = 2
_TOKEN_APPROVE = 4
_TOKEN_TRANSFER = 3
_TOKEN_CLOSE = 9
_IDL = Path(__file__).resolve().parents[2] / "idl" / "pump_fun_idl.json"


class PolicyError(ValueError):
    """The request is outside the closed safety boundary."""


@dataclass(frozen=True)
class ExecutorLimits:
    """Caps for one unsigned request. `submit_enabled` stays false."""

    fee_payer: str
    capital_cap_nok: float = CAPITAL_CAP_NOK
    reserve_lamports: int = RESERVE_FLOOR_LAMPORTS
    max_spend_lamports: int = MAX_SPEND_LAMPORTS
    max_quote_lamports: int = 1_000_000
    buy_slippage: float = 0.05
    sell_slippage: float = 0.05
    priority_fee_microlamports: int = 0
    max_positions: int = 1
    max_retries: int = 1
    blockhash_max_age_seconds: int = 60
    submit_enabled: bool = False
    live: bool = False
    stop: bool = True
    gmgn_read: bool = False
    deadline_oslo: datetime | None = None
    buy_cu: int = 180_000
    sell_cu: int = 120_000
    close_cu: int = 40_000


@dataclass(frozen=True)
class FxQuote:
    """NOK per 1 SOL, supplied by the caller. Not a token price."""

    nok_per_sol: float
    observed_at: datetime
    source: str


@dataclass(frozen=True)
class BalanceSnapshot:
    """Lamports in the fee payer, from a fixture or a read-only RPC."""

    lamports: int
    observed_at: datetime
    source: str


def _as_oslo(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=OSLO)
    return moment.astimezone(OSLO)


def as_oslo(moment: datetime) -> datetime:
    """Return `moment` as Europe/Oslo. A naive value is read as Oslo local time."""
    return _as_oslo(moment)


def _sol_to_lamports(amount: float) -> int:
    return round(amount * 1_000_000_000)


def load_executor_config(path: Path) -> ExecutorLimits:
    """Load the dedicated executor file. A raw-key bot config is refused."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise PolicyError(f"{path} is not a mapping")
    if "private_key" in raw or "keystore" in raw:
        raise PolicyError(
            f"{path.name} names a raw key or keystore. "
            "This command does not start that bot."
        )
    for banned in ("leverage", "bridge", "topup"):
        if raw.get(banned):
            raise PolicyError(f"{banned} is not a permitted field")
    if raw.get("submit") is True:
        raise PolicyError("submit cannot be enabled")
    if int(raw.get("max_positions", 1)) != 1:
        raise PolicyError("max_positions must be 1")
    deadline = raw.get("deadline_oslo")
    parsed_deadline = None
    if deadline not in (None, ""):
        parsed_deadline = _as_oslo(datetime.fromisoformat(str(deadline)))
    reserve = _sol_to_lamports(float(raw.get("reserve_sol", 0.008224)))
    if reserve < RESERVE_FLOOR_LAMPORTS:
        raise PolicyError("reserve_sol is below 0.008224")
    max_spend = _sol_to_lamports(float(raw.get("max_spend_sol", 0.215)))
    if max_spend > MAX_SPEND_LAMPORTS:
        raise PolicyError("max_spend_sol is above 0.215")
    cap = float(raw.get("capital_cap_nok", CAPITAL_CAP_NOK))
    if cap > CAPITAL_CAP_NOK:
        raise PolicyError("capital_cap_nok is above 200")
    return ExecutorLimits(
        fee_payer=str(raw.get("fee_payer", FEE_PAYER)),
        capital_cap_nok=cap,
        reserve_lamports=reserve,
        max_spend_lamports=max_spend,
        max_quote_lamports=_sol_to_lamports(float(raw.get("max_quote_sol", 0.001))),
        buy_slippage=float(raw.get("buy_slippage", 0.05)),
        sell_slippage=float(raw.get("sell_slippage", 0.05)),
        priority_fee_microlamports=int(raw.get("priority_fee_microlamports", 0)),
        max_positions=1,
        max_retries=int(raw.get("max_retries", 1)),
        blockhash_max_age_seconds=int(raw.get("blockhash_max_age_seconds", 60)),
        submit_enabled=False,
        live=bool(raw.get("live", False)),
        stop=bool(raw.get("stop", True)),
        gmgn_read=bool(raw.get("gmgn_read", False)),
        deadline_oslo=parsed_deadline,
    )


def _pump() -> tuple[PumpFunInstructionBuilder, PumpFunAddressProvider]:
    builder = PumpFunInstructionBuilder(IDLParser(str(_IDL)))
    return builder, PumpFunAddressProvider()


def fixture_token() -> TokenInfo:
    """The layout verifier's synthetic coin. Not a live market listing."""
    provider = PumpFunAddressProvider()
    mint = Pubkey.from_string(FIXTURE_MINT)
    creator = Pubkey.from_string(FIXTURE_CREATOR)
    bonding_curve = provider.derive_pool_address(mint)
    return TokenInfo(
        name="fixture-not-a-signal",
        symbol="FIXTURE",
        uri="",
        mint=mint,
        platform=Platform.PUMP_FUN,
        bonding_curve=bonding_curve,
        associated_bonding_curve=provider.derive_associated_bonding_curve(
            mint, bonding_curve, SystemAddresses.TOKEN_2022_PROGRAM
        ),
        creator=creator,
        creator_vault=provider.derive_creator_vault(creator),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
        quote_mint=SystemAddresses.WSOL_MINT,
    )


def _fee_ceiling(limits: ExecutorLimits, cu_limit: int) -> int:
    return BASE_SIGNATURE_LAMPORTS + priority_fee_lamports(
        limits.priority_fee_microlamports, cu_limit
    )


def _require_signal(signal_id: str | None) -> str:
    signal = (signal_id or "").strip()
    if not signal:
        raise PolicyError("no signal, no trade")
    if len(signal) > 80:
        raise PolicyError("signal id is too long")
    return signal


def _require_fx(limits: ExecutorLimits, fx: FxQuote | None, now: datetime) -> FxQuote:
    if fx is None or fx.nok_per_sol <= 0:
        raise PolicyError("NOK per SOL is required and must be positive")
    if limits.live and fx.source == "fixture":
        raise PolicyError("live mode refuses a fixture FX rate")
    age = _as_oslo(now) - _as_oslo(fx.observed_at)
    if limits.live and age > timedelta(seconds=FRESH_SECONDS):
        raise PolicyError("FX rate is older than 15 minutes")
    return fx


def _require_balance(
    limits: ExecutorLimits, balance: BalanceSnapshot | None, now: datetime
) -> BalanceSnapshot:
    if balance is None or balance.lamports < 0:
        raise PolicyError("a balance snapshot is required")
    if limits.live and balance.source != "rpc_getBalance":
        raise PolicyError("live mode requires a fresh getBalance result")
    age = _as_oslo(now) - _as_oslo(balance.observed_at)
    if limits.live and age > timedelta(seconds=FRESH_SECONDS):
        raise PolicyError("balance snapshot is older than 15 minutes")
    return balance


def _require_deadline(limits: ExecutorLimits, now: datetime) -> None:
    if not limits.live:
        return
    if limits.deadline_oslo is None:
        raise PolicyError("live mode requires a finite Europe/Oslo deadline")
    if _as_oslo(now) >= _as_oslo(limits.deadline_oslo):
        raise PolicyError("the Europe/Oslo deadline has passed")


def _require_room(
    limits: ExecutorLimits,
    *,
    spend_lamports: int,
    fee_lamports: int,
    rent_lamports: int,
    balance: BalanceSnapshot,
    fx: FxQuote,
) -> float:
    if spend_lamports < 0:
        raise PolicyError("spend is negative")
    if spend_lamports > limits.max_quote_lamports:
        raise PolicyError("spend is above the configured quote cap")
    if spend_lamports > limits.max_spend_lamports:
        raise PolicyError("spend is above 0.215 SOL")
    spend_nok = spend_lamports / 1_000_000_000 * fx.nok_per_sol
    if spend_nok > limits.capital_cap_nok:
        raise PolicyError(
            f"spend is {spend_nok:.2f} NOK at the supplied FX, "
            f"above the {limits.capital_cap_nok:.0f} NOK cap"
        )
    need = spend_lamports + fee_lamports + rent_lamports + limits.reserve_lamports
    if balance.lamports < need:
        raise PolicyError(
            "balance does not cover spend, fees, rent and the "
            f"{limits.reserve_lamports} lamport reserve"
        )
    return spend_nok


def _token_opcode(instruction: Instruction) -> int | None:
    program = instruction.program_id
    token_programs = {
        SystemAddresses.TOKEN_PROGRAM,
        SystemAddresses.TOKEN_2022_PROGRAM,
    }
    if program not in token_programs or not instruction.data:
        return None
    return instruction.data[0]


def _system_transfer_lamports(instruction: Instruction) -> int | None:
    if instruction.program_id != SystemAddresses.SYSTEM_PROGRAM:
        return None
    data = bytes(instruction.data)
    if len(data) < 12 or int.from_bytes(data[:4], "little") != _SYSTEM_TRANSFER:
        raise PolicyError("unrecognized system instruction")
    return int.from_bytes(data[4:12], "little")


def assert_instructions_allowed(
    kind: str, instructions: list[Instruction], fee_payer: Pubkey
) -> None:
    """Reject transfers, approvals and programs this kind does not build."""
    if not instructions:
        raise PolicyError("transaction has no instructions")
    pump = PumpFunAddresses.PROGRAM
    allowed = {
        COMPUTE_BUDGET_PROGRAM,
        SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
    }
    if kind == "buy":
        allowed.add(pump)
    elif kind == "sell":
        allowed.add(pump)
    elif kind == "close":
        allowed.add(SystemAddresses.TOKEN_PROGRAM)
        allowed.add(SystemAddresses.TOKEN_2022_PROGRAM)
    elif kind == "self_transfer_simulation":
        allowed = {SystemAddresses.SYSTEM_PROGRAM}
    else:
        raise PolicyError(f"unknown request kind {kind}")
    for instruction in instructions:
        if instruction.program_id not in allowed:
            raise PolicyError(
                f"{kind} refuses program {instruction.program_id}"
            )
        opcode = _token_opcode(instruction)
        if opcode in {_TOKEN_APPROVE, _TOKEN_TRANSFER}:
            raise PolicyError("token approve and transfer are refused")
        if kind == "close" and opcode not in {None, _TOKEN_CLOSE}:
            raise PolicyError("close allows only CloseAccount")
        if kind != "self_transfer_simulation":
            if instruction.program_id == SystemAddresses.SYSTEM_PROGRAM:
                raise PolicyError("a trade must not move SOL with a transfer")
            continue
        lamports = _system_transfer_lamports(instruction)
        if lamports != 0:
            raise PolicyError("self-transfer simulation must move 0 lamports")
        if len(instruction.accounts) < 2:
            raise PolicyError("self-transfer is missing accounts")
        source = instruction.accounts[0].pubkey
        dest = instruction.accounts[1].pubkey
        if source != fee_payer or dest != fee_payer:
            raise PolicyError("self-transfer must be the fee payer to itself")


def _with_compute_budget(
    instructions: list[Instruction], cu_limit: int, micro_lamports: int
) -> list[Instruction]:
    budget = [set_compute_unit_limit(cu_limit)]
    if micro_lamports > 0:
        budget.append(set_compute_unit_price(micro_lamports))
    return budget + instructions


def _request_id(transaction: str) -> str:
    return hashlib.sha256(transaction.encode("ascii")).hexdigest()


def _blank_state() -> dict[str, Any]:
    return {"open_mint": None, "built": [], "accepted": [], "pending_id": None}


def load_state(path: Path) -> dict[str, Any]:
    """Read the local one-position ledger, or an empty one."""
    if not path.exists():
        return _blank_state()
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise PolicyError("state file is not a mapping")
    raw.setdefault("open_mint", None)
    raw.setdefault("built", [])
    raw.setdefault("accepted", [])
    raw.setdefault("pending_id", None)
    return raw


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Write the ledger. The directory is created if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def _remember_build(state: dict[str, Any], request_id: str) -> None:
    built = state.setdefault("built", [])
    if request_id in built or request_id in state.get("accepted", []):
        raise PolicyError("this exact transaction was already built")
    if state.get("pending_id"):
        raise PolicyError("a request is already waiting for its simulation")
    built.append(request_id)
    state["pending_id"] = request_id


class _Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = load_state(path)

    def save(self) -> None:
        save_state(self.path, self.data)


def _trade_gates(
    limits: ExecutorLimits,
    *,
    kind: str,
    signal_id: str | None,
    spend_lamports: int,
    fee_lamports: int,
    rent_lamports: int,
    fx: FxQuote | None,
    balance: BalanceSnapshot | None,
    now: datetime,
    state: dict[str, Any],
    mint: str | None,
) -> float:
    if limits.submit_enabled:
        raise PolicyError("submit is disabled")
    if limits.stop:
        raise PolicyError("stop flag is set")
    _require_signal(signal_id)
    _require_deadline(limits, now)
    checked_fx = _require_fx(limits, fx, now)
    checked_balance = _require_balance(limits, balance, now)
    if state.get("pending_id"):
        raise PolicyError("a request is already waiting for its simulation")
    if kind == "buy":
        if state.get("open_mint"):
            raise PolicyError("one position is already open")
    elif mint is None or state.get("open_mint") != mint:
        raise PolicyError("sell and close require the single open position")
    if limits.live and kind in {"buy", "sell"}:
        raise PolicyError(
            "live mode has no price feed wired; rebuild from an explicit "
            "fixture only while live is false"
        )
    return _require_room(
        limits,
        spend_lamports=spend_lamports,
        fee_lamports=fee_lamports,
        rent_lamports=rent_lamports,
        balance=checked_balance,
        fx=checked_fx,
    )


def _envelope(
    kind: str,
    payer: Pubkey,
    instructions: list[Instruction],
    recent_blockhash: Hash,
) -> PhantomSendEnvelope:
    assert_instructions_allowed(kind, instructions, payer)
    envelope = build_phantom_send_envelope(payer, instructions, recent_blockhash)
    if envelope.confirmed:
        raise PolicyError("envelope confirmed flag must stay false")
    return envelope


def _document(
    *,
    kind: str,
    label: str,
    envelope: PhantomSendEnvelope,
    blockhash: Hash,
    blockhash_source: str,
    now: datetime,
    limits: ExecutorLimits,
    signal_id: str | None,
    spend_lamports: int,
    fee_lamports: int,
    rent_lamports: int,
    spend_nok: float | None,
    fx: FxQuote | None,
    mint: str | None,
) -> dict[str, Any]:
    return {
        "schema": "phantom-executor/1",
        "kind": kind,
        "label": label,
        "confirmed": False,
        "submit": False,
        "network_id": NETWORK_ID,
        "fee_payer": envelope.fee_payer,
        "transaction": envelope.transaction,
        "blockhash": str(blockhash),
        "blockhash_source": blockhash_source,
        "built_at": _as_oslo(now).isoformat(),
        "max_age_seconds": limits.blockhash_max_age_seconds,
        "request_id": _request_id(envelope.transaction),
        "signal_id": signal_id,
        "mint": mint,
        "spend_lamports": spend_lamports,
        "fee_ceiling_lamports": fee_lamports,
        "rent_lamports": rent_lamports,
        "reserve_lamports": limits.reserve_lamports,
        "capital_cap_nok": limits.capital_cap_nok,
        "fx_nok_per_sol": None if fx is None else fx.nok_per_sol,
        "fx_source": None if fx is None else fx.source,
        "spend_nok": spend_nok,
        "max_retries": limits.max_retries,
        "notes": (
            "Parent may pass transaction to solana_send with confirmed omitted. "
            "confirmed true signs and broadcasts and is outside this tool."
        ),
    }


def _payer(limits: ExecutorLimits) -> Pubkey:
    return Pubkey.from_string(limits.fee_payer)


def build_buy_request(
    limits: ExecutorLimits,
    *,
    quote_lamports: int,
    minimum_tokens_out: int,
    blockhash: Hash,
    blockhash_source: str,
    signal_id: str,
    fx: FxQuote,
    balance: BalanceSnapshot,
    now: datetime,
    state_path: Path,
) -> dict[str, Any]:
    """Unsigned pump.fun buy_v2 for the fixture coin."""
    if minimum_tokens_out <= 0 or quote_lamports <= 0:
        raise PolicyError("buy amounts must be positive")
    if blockhash_source == "fixture" and limits.live:
        raise PolicyError("live mode refuses a fixture blockhash")
    ledger = _Ledger(state_path)
    fee = _fee_ceiling(limits, limits.buy_cu)
    rent = token_account_rent_lamports()
    spend_nok = _trade_gates(
        limits,
        kind="buy",
        signal_id=signal_id,
        spend_lamports=quote_lamports,
        fee_lamports=fee,
        rent_lamports=rent,
        fx=fx,
        balance=balance,
        now=now,
        state=ledger.data,
        mint=FIXTURE_MINT,
    )
    payer = _payer(limits)
    builder, provider = _pump()
    built = asyncio.run(
        builder.build_buy_instruction(
            fixture_token(), payer, quote_lamports, minimum_tokens_out, provider
        )
    )
    instructions = _with_compute_budget(
        built, limits.buy_cu, limits.priority_fee_microlamports
    )
    envelope = _envelope("buy", payer, instructions, blockhash)
    document = _document(
        kind="buy",
        label="FIXTURE_BUY_NOT_A_LIVE_TRADE",
        envelope=envelope,
        blockhash=blockhash,
        blockhash_source=blockhash_source,
        now=now,
        limits=limits,
        signal_id=signal_id,
        spend_lamports=quote_lamports,
        fee_lamports=fee,
        rent_lamports=rent,
        spend_nok=spend_nok,
        fx=fx,
        mint=FIXTURE_MINT,
    )
    _remember_build(ledger.data, document["request_id"])
    ledger.save()
    return document


def build_sell_request(
    limits: ExecutorLimits,
    *,
    token_amount_in: int,
    minimum_quote_out: int,
    blockhash: Hash,
    blockhash_source: str,
    signal_id: str,
    fx: FxQuote,
    balance: BalanceSnapshot,
    now: datetime,
    state_path: Path,
) -> dict[str, Any]:
    """Unsigned pump.fun sell_v2 for the open fixture position."""
    if token_amount_in <= 0 or minimum_quote_out < 0:
        raise PolicyError("sell token amount must be positive")
    ledger = _Ledger(state_path)
    fee = _fee_ceiling(limits, limits.sell_cu)
    spend_nok = _trade_gates(
        limits,
        kind="sell",
        signal_id=signal_id,
        spend_lamports=0,
        fee_lamports=fee,
        rent_lamports=0,
        fx=fx,
        balance=balance,
        now=now,
        state=ledger.data,
        mint=FIXTURE_MINT,
    )
    payer = _payer(limits)
    builder, provider = _pump()
    built = asyncio.run(
        builder.build_sell_instruction(
            fixture_token(), payer, token_amount_in, minimum_quote_out, provider
        )
    )
    instructions = _with_compute_budget(
        built, limits.sell_cu, limits.priority_fee_microlamports
    )
    envelope = _envelope("sell", payer, instructions, blockhash)
    document = _document(
        kind="sell",
        label="FIXTURE_SELL_NOT_A_LIVE_TRADE",
        envelope=envelope,
        blockhash=blockhash,
        blockhash_source=blockhash_source,
        now=now,
        limits=limits,
        signal_id=signal_id,
        spend_lamports=0,
        fee_lamports=fee,
        rent_lamports=0,
        spend_nok=spend_nok,
        fx=fx,
        mint=FIXTURE_MINT,
    )
    _remember_build(ledger.data, document["request_id"])
    ledger.save()
    return document


def build_close_request(
    limits: ExecutorLimits,
    *,
    blockhash: Hash,
    blockhash_source: str,
    signal_id: str,
    fx: FxQuote,
    balance: BalanceSnapshot,
    now: datetime,
    state_path: Path,
) -> dict[str, Any]:
    """Unsigned close of the fixture coin's token account. No burn."""
    ledger = _Ledger(state_path)
    fee = _fee_ceiling(limits, limits.close_cu)
    _trade_gates(
        limits,
        kind="close",
        signal_id=signal_id,
        spend_lamports=0,
        fee_lamports=fee,
        rent_lamports=0,
        fx=fx,
        balance=balance,
        now=now,
        state=ledger.data,
        mint=FIXTURE_MINT,
    )
    payer = _payer(limits)
    mint = Pubkey.from_string(FIXTURE_MINT)
    account = get_associated_token_address(
        payer, mint, SystemAddresses.TOKEN_2022_PROGRAM
    )
    built = [
        close_account(
            CloseAccountParams(
                account=account,
                dest=payer,
                owner=payer,
                program_id=SystemAddresses.TOKEN_2022_PROGRAM,
            )
        )
    ]
    instructions = _with_compute_budget(
        built, limits.close_cu, limits.priority_fee_microlamports
    )
    envelope = _envelope("close", payer, instructions, blockhash)
    document = _document(
        kind="close",
        label="FIXTURE_CLOSE_NOT_A_LIVE_TRADE",
        envelope=envelope,
        blockhash=blockhash,
        blockhash_source=blockhash_source,
        now=now,
        limits=limits,
        signal_id=signal_id,
        spend_lamports=0,
        fee_lamports=fee,
        rent_lamports=0,
        spend_nok=0.0,
        fx=fx,
        mint=FIXTURE_MINT,
    )
    _remember_build(ledger.data, document["request_id"])
    ledger.save()
    return document


def build_self_transfer_request(
    limits: ExecutorLimits,
    *,
    blockhash: Hash,
    blockhash_source: str,
    now: datetime,
) -> dict[str, Any]:
    """0-lamport transfer to the fee payer. Not a token trade and not a send."""
    if limits.submit_enabled:
        raise PolicyError("submit is disabled")
    payer = _payer(limits)
    instructions = [
        transfer(TransferParams(from_pubkey=payer, to_pubkey=payer, lamports=0))
    ]
    envelope = _envelope("self_transfer_simulation", payer, instructions, blockhash)
    return _document(
        kind="self_transfer_simulation",
        label="SELF_TRANSFER_SIMULATION_NOT_A_TRADE",
        envelope=envelope,
        blockhash=blockhash,
        blockhash_source=blockhash_source,
        now=now,
        limits=limits,
        signal_id=None,
        spend_lamports=0,
        fee_lamports=BASE_SIGNATURE_LAMPORTS,
        rent_lamports=0,
        spend_nok=0.0,
        fx=None,
        mint=None,
    )


def write_request(path: Path, document: dict[str, Any]) -> None:
    """Write one inspectable request. The file contains no secret."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")


def accept_external_result(
    request: dict[str, Any],
    result: dict[str, Any],
    *,
    now: datetime,
    state_path: Path,
    max_age_seconds: int,
) -> dict[str, Any]:
    """Record a matching simulation. A broadcast or a stale hash is refused."""
    if request.get("confirmed") is not False or request.get("submit") is not False:
        raise PolicyError("request is not a simulation envelope")
    if result.get("schema") != "phantom-executor-result/1":
        raise PolicyError("result schema is not phantom-executor-result/1")
    if result.get("request_id") != request.get("request_id"):
        raise PolicyError("result is for a different request")
    if result.get("transaction") != request.get("transaction"):
        raise PolicyError("result transaction bytes differ; rebuild")
    if result.get("confirmed") is not False or result.get("submitted") is not False:
        raise PolicyError("a broadcast result is refused")
    if result.get("simulated") is not True:
        raise PolicyError("result is not a simulation")
    if result.get("blockhash") != request.get("blockhash"):
        raise PolicyError("blockhash changed; rebuild and simulate again")
    if result.get("err") not in (None, ""):
        raise PolicyError(f"simulation returned an error: {result.get('err')}")
    observed = _as_oslo(datetime.fromisoformat(str(result["observed_at"])))
    built = _as_oslo(datetime.fromisoformat(str(request["built_at"])))
    stale = (
        observed - built > timedelta(seconds=max_age_seconds)
        or _as_oslo(now) - built > timedelta(seconds=max_age_seconds)
    )
    ledger = _Ledger(state_path)
    if stale:
        if ledger.data.get("pending_id") == request.get("request_id"):
            ledger.data["pending_id"] = None
            ledger.save()
        raise PolicyError("blockhash is stale; rebuild and simulate again")
    request_id = str(request["request_id"])
    if request_id in ledger.data["accepted"]:
        raise PolicyError("this simulation was already accepted")
    attempt = int(result.get("attempt", 1))
    if attempt > int(request.get("max_retries", 1)):
        raise PolicyError("retry limit exceeded")
    ledger.data["accepted"].append(request_id)
    ledger.data["pending_id"] = None
    kind = request.get("kind")
    if kind == "buy":
        ledger.data["open_mint"] = request.get("mint")
    elif kind in {"sell", "close"}:
        ledger.data["open_mint"] = None
    ledger.save()
    return {"accepted": True, "request_id": request_id, "submitted": False}


def read_only_rpc(
    url: str,
    method: str,
    params: list[object],
    transport: Callable[[str, str, list[object]], object],
) -> object:
    """Call one read method. `sendTransaction` and other URLs are refused."""
    if method not in READ_ONLY_RPC_METHODS:
        raise PolicyError(f"RPC method {method} is not allowed")
    if url != OFFICIAL_RPC:
        raise PolicyError("only https://api.mainnet-beta.solana.com is allowed")
    return transport(url, method, params)


def urllib_transport(url: str, method: str, params: list[object]) -> object:
    """POST one JSON-RPC read. Used only when the CLI is given `--read-rpc`."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if "error" in payload:
        raise PolicyError(str(payload["error"]))
    return payload["result"]


def blockhash_from_rpc(
    transport: Callable[[str, str, list[object]], object] = urllib_transport,
) -> tuple[Hash, str]:
    """Read `getLatestBlockhash` from the public RPC. Does not send."""
    result = read_only_rpc(
        OFFICIAL_RPC,
        "getLatestBlockhash",
        [{"commitment": "processed"}],
        transport,
    )
    if not isinstance(result, dict):
        raise PolicyError("blockhash response is not an object")
    value = result["value"]["blockhash"]
    return Hash.from_string(str(value)), "rpc_getLatestBlockhash"


def balance_from_rpc(
    fee_payer: str,
    transport: Callable[[str, str, list[object]], object] = urllib_transport,
) -> BalanceSnapshot:
    """Read `getBalance` for the fee payer. Does not send."""
    result = read_only_rpc(
        OFFICIAL_RPC, "getBalance", [fee_payer, {"commitment": "processed"}], transport
    )
    if not isinstance(result, dict):
        raise PolicyError("balance response is not an object")
    return BalanceSnapshot(
        lamports=int(result["value"]),
        observed_at=datetime.now(UTC),
        source="rpc_getBalance",
    )
