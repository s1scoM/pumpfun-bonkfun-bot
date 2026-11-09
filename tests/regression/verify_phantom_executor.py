# ruff: noqa: C901, PLR0912, PLR0915, PLR2004, TRY003
"""Buy, sell and close envelopes are real pump.fun instructions and stay unsigned.

The amounts, mint and blockhash are fixtures. No socket, no keypair, no
`sendTransaction`. A simulation result is accepted only when it matches the
request, is not a broadcast, and is younger than the blockhash budget.

Usage:
    uv run tests/regression/verify_phantom_executor.py
"""

from __future__ import annotations

import base64
import socket
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _refuse_connect(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("NETWORK_DISABLED")


def _refuse_socket(
    _self: socket.socket, *_args: object, **_kwargs: object
) -> None:
    _refuse_connect()


socket.socket.connect = _refuse_socket  # type: ignore[method-assign]
socket.create_connection = _refuse_connect  # type: ignore[assignment]

import yaml  # noqa: E402
from _fakes import _check  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from config_loader import disabled_in_yaml  # noqa: E402
from core.pubkeys import SystemAddresses  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402
from trading.gmgn_read import GmgnReadOnly  # noqa: E402
from trading.phantom_executor import (  # noqa: E402
    FIXTURE_MINT,
    OFFICIAL_RPC,
    BalanceSnapshot,
    ExecutorLimits,
    FxQuote,
    PolicyError,
    accept_external_result,
    assert_instructions_allowed,
    balance_from_rpc,
    build_buy_request,
    build_close_request,
    build_self_transfer_request,
    build_sell_request,
    load_executor_config,
    read_only_rpc,
)

_NOW = datetime.fromisoformat("2026-11-09T12:00:00+01:00")
_CONFIG = PROJECT_ROOT / "bots" / "phantom-executor.yaml"
_SNIPER = PROJECT_ROOT / "bots" / "bot-sniper-1-geyser.yaml"
_CLI = PROJECT_ROOT / "tools" / "phantom_executor.py"
_MODULE = PROJECT_ROOT / "src" / "trading" / "phantom_executor.py"


def _limits(**overrides: object) -> ExecutorLimits:
    values: dict[str, object] = {
        "fee_payer": "8rRjKEhJYoYJmG3AVz64xq1cj9LUpqr77j9My5T26BDj",
        "stop": False,
        "live": False,
        "submit_enabled": False,
    }
    values.update(overrides)
    return ExecutorLimits(**values)  # type: ignore[arg-type]


def _fx(rate: float = 820.0) -> FxQuote:
    return FxQuote(nok_per_sol=rate, observed_at=_NOW, source="fixture")


def _balance(lamports: int = 223_224_000) -> BalanceSnapshot:
    return BalanceSnapshot(lamports=lamports, observed_at=_NOW, source="fixture")


def _pump_instruction(document: dict) -> tuple[Pubkey, bytes]:
    raw = VersionedTransaction.from_bytes(base64.b64decode(document["transaction"]))
    found = None
    for compiled in raw.message.instructions:
        program = raw.message.account_keys[compiled.program_id_index]
        if program == PumpFunAddresses.PROGRAM:
            found = (program, bytes(compiled.data))
    if found is None:
        raise AssertionError("no pump.fun instruction")
    return found


def _result(document: dict, **overrides: object) -> dict:
    body = {
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
    body.update(overrides)
    return body


def _buy(tmp: Path, **overrides: object) -> dict:
    arguments = {
        "limits": _limits(),
        "quote_lamports": 1_000,
        "minimum_tokens_out": 1,
        "blockhash": Hash.default(),
        "blockhash_source": "fixture",
        "signal_id": "fixture-signal",
        "fx": _fx(),
        "balance": _balance(),
        "now": _NOW,
        "state_path": tmp / "state.json",
    }
    arguments.update(overrides)
    return build_buy_request(**arguments)  # type: ignore[arg-type]


def main() -> None:
    """Run the executor checks and exit non-zero when one fails."""
    print("PHANTOM EXECUTOR (unsigned fixtures, network disabled)")
    problems: list[str] = []
    tmp = PROJECT_ROOT / "phantom-requests" / "verifier"
    if tmp.exists():
        for child in sorted(tmp.glob("**/*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()
    tmp.mkdir(parents=True, exist_ok=True)

    sniper = yaml.safe_load(_SNIPER.read_text())
    if not _check(
        "shipped sniper stays enabled and is refused here",
        sniper.get("enabled") is True
        and sniper.get("private_key") == "${SOLANA_PRIVATE_KEY}",
        "bots/bot-sniper-1-geyser.yaml",
    ):
        problems.append("sniper")
    try:
        load_executor_config(_SNIPER)
        refused = False
    except PolicyError:
        refused = True
    if not _check("raw-key config cannot load", refused, _SNIPER.name):
        problems.append("sniper load")

    loaded = load_executor_config(_CONFIG)
    if not _check(
        "dedicated config is stopped and cannot submit",
        disabled_in_yaml(str(_CONFIG))
        and loaded.stop
        and loaded.submit_enabled is False
        and loaded.live is False
        and loaded.gmgn_read is False
        and loaded.capital_cap_nok == 200
        and loaded.reserve_lamports == 8_224_000
        and loaded.max_spend_lamports == 215_000_000,
        _CONFIG.name,
    ):
        problems.append("config")

    source = _MODULE.read_text() + _CLI.read_text()
    if not _check(
        "executor source has no keypair and does not start the bot",
        "Keypair(" not in source
        and "bot_runner" not in source
        and "send_transaction" not in source
        and "sendTransaction(" not in source,
        "phantom_executor",
    ):
        problems.append("source")

    for label, kwargs in (
        ("stop flag", {"limits": _limits(stop=True)}),
        ("missing signal", {"signal_id": "  "}),
        ("missing fx", {"fx": None}),
        (
            "NOK cap",
            {"fx": _fx(1_000_000), "quote_lamports": 1_000_000},
        ),
        (
            "fees rent and reserve",
            {"quote_lamports": 215_000_000, "limits": _limits(max_quote_lamports=215_000_000)},
        ),
    ):
        try:
            _buy(tmp / label, **kwargs)
            failed = False
        except PolicyError:
            failed = True
        if not _check(f"buy refuses {label}", failed, label):
            problems.append(label)

    buy = _buy(tmp / "buy")
    program, data = _pump_instruction(buy)
    builder_buy = data[:8]
    if not _check(
        "buy is an unsigned pump.fun instruction for the fixture mint",
        buy["confirmed"] is False
        and buy["submit"] is False
        and buy["mint"] == FIXTURE_MINT
        and buy["label"] == "FIXTURE_BUY_NOT_A_LIVE_TRADE"
        and program == PumpFunAddresses.PROGRAM
        and buy["transaction"].startswith("AQAA")
        and VersionedTransaction.from_bytes(
            base64.b64decode(buy["transaction"])
        ).signatures[0]
        == Signature.default(),
        f"spend {buy['spend_lamports']} lamports",
    ):
        problems.append("buy shape")

    try:
        _buy(tmp / "buy")
        duplicate = False
    except PolicyError:
        duplicate = True
    if not _check("second buy waits instead of duplicating", duplicate, "pending"):
        problems.append("duplicate buy")

    try:
        accept_external_result(
            buy,
            _result(buy, submitted=True),
            now=_NOW,
            state_path=tmp / "buy" / "state.json",
            max_age_seconds=60,
        )
        broadcast = False
    except PolicyError:
        broadcast = True
    if not _check("broadcast result is refused", broadcast, "submitted"):
        problems.append("broadcast")

    decision = accept_external_result(
        buy,
        _result(buy),
        now=_NOW,
        state_path=tmp / "buy" / "state.json",
        max_age_seconds=60,
    )
    if not _check(
        "matching simulation is recorded and not submitted",
        decision["accepted"] is True and decision["submitted"] is False,
        decision["request_id"][:12],
    ):
        problems.append("accept")
    try:
        accept_external_result(
            buy,
            _result(buy),
            now=_NOW,
            state_path=tmp / "buy" / "state.json",
            max_age_seconds=60,
        )
        replay = False
    except PolicyError:
        replay = True
    if not _check("the same simulation cannot be accepted twice", replay, "replay"):
        problems.append("replay")

    try:
        accept_external_result(
            buy,
            _result(buy, observed_at="2026-11-09T12:05:00+01:00"),
            now=_NOW + timedelta(minutes=5),
            state_path=tmp / "stale" / "state.json",
            max_age_seconds=60,
        )
        stale = False
    except PolicyError as exc:
        stale = "rebuild" in str(exc)
    if not _check("stale blockhash asks for a rebuild", stale, "age"):
        problems.append("stale")

    sell = build_sell_request(
        _limits(),
        token_amount_in=1,
        minimum_quote_out=1,
        blockhash=Hash.default(),
        blockhash_source="fixture",
        signal_id="fixture-signal",
        fx=_fx(),
        balance=_balance(),
        now=_NOW,
        state_path=tmp / "buy" / "state.json",
    )
    _sell_program, sell_data = _pump_instruction(sell)
    if not _check(
        "sell is a different unsigned pump.fun instruction",
        sell["kind"] == "sell"
        and sell["spend_lamports"] == 0
        and sell_data[:8] != builder_buy
        and sell["confirmed"] is False,
        "sell_v2",
    ):
        problems.append("sell")
    accept_external_result(
        sell,
        _result(sell),
        now=_NOW,
        state_path=tmp / "buy" / "state.json",
        max_age_seconds=60,
    )

    opened = _buy(tmp / "close")
    accept_external_result(
        opened,
        _result(opened),
        now=_NOW,
        state_path=tmp / "close" / "state.json",
        max_age_seconds=60,
    )
    close = build_close_request(
        _limits(),
        blockhash=Hash.default(),
        blockhash_source="fixture",
        signal_id="fixture-signal",
        fx=_fx(),
        balance=_balance(),
        now=_NOW,
        state_path=tmp / "close" / "state.json",
    )
    decoded = VersionedTransaction.from_bytes(base64.b64decode(close["transaction"]))
    closed = False
    for compiled in decoded.message.instructions:
        program = decoded.message.account_keys[compiled.program_id_index]
        if program == SystemAddresses.TOKEN_2022_PROGRAM and bytes(compiled.data)[:1] == bytes([9]):
            closed = True
    if not _check(
        "close is an unsigned Token-2022 CloseAccount",
        close["kind"] == "close" and closed and close["confirmed"] is False,
        "opcode 9",
    ):
        problems.append("close")

    payer = Pubkey.from_string("8rRjKEhJYoYJmG3AVz64xq1cj9LUpqr77j9My5T26BDj")
    try:
        assert_instructions_allowed(
            "buy",
            [
                Instruction(
                    program_id=SystemAddresses.TOKEN_PROGRAM,
                    data=bytes([4]),
                    accounts=[AccountMeta(payer, is_signer=True, is_writable=False)],
                )
            ],
            payer,
        )
        approved = False
    except PolicyError:
        approved = True
    if not _check("token approve is refused", approved, "opcode 4"):
        problems.append("approve")

    self_transfer = build_self_transfer_request(
        _limits(stop=True),
        blockhash=Hash.default(),
        blockhash_source="fixture",
        now=_NOW,
    )
    if not _check(
        "self-transfer is zero lamports and not a trade",
        self_transfer["kind"] == "self_transfer_simulation"
        and self_transfer["spend_lamports"] == 0
        and self_transfer["label"] == "SELF_TRANSFER_SIMULATION_NOT_A_TRADE"
        and self_transfer["confirmed"] is False,
        "stop still allows this diagnostic",
    ):
        problems.append("self")

    def _transport(_url: str, method: str, _params: list) -> dict:
        if method == "sendTransaction":
            raise AssertionError("send was attempted")
        return {"value": {"blockhash": "11111111111111111111111111111111"}}

    try:
        read_only_rpc(OFFICIAL_RPC, "sendTransaction", [], _transport)
        blocked = False
    except PolicyError:
        blocked = True
    if not _check("sendTransaction is not a read", blocked, "method"):
        problems.append("send")
    try:
        read_only_rpc("https://example.invalid", "getBalance", [], _transport)
        other = False
    except PolicyError:
        other = True
    if not _check("a non-public RPC URL is refused", other, "url"):
        problems.append("url")
    preview = read_only_rpc(
        OFFICIAL_RPC, "getLatestBlockhash", [{"commitment": "processed"}], _transport
    )
    balance = balance_from_rpc(
        "8rRjKEhJYoYJmG3AVz64xq1cj9LUpqr77j9My5T26BDj",
        lambda _url, _method, _params: {"value": 223_224_000},
    )
    if not _check(
        "read-only balance and blockhash use the fake transport",
        preview["value"]["blockhash"].startswith("1")
        and balance.lamports == 223_224_000
        and balance.source == "rpc_getBalance",
        "fake rpc",
    ):
        problems.append("rpc fake")

    gmgn = GmgnReadOnly()
    try:
        gmgn.token_security(FIXTURE_MINT)
        gmgn_off = False
    except PolicyError:
        gmgn_off = True
    sample = GmgnReadOnly(enabled=True).token_security_fixture(
        {
            "label": "GMGN_FIXTURE_NOT_A_LIVE_RESPONSE",
            "address": FIXTURE_MINT,
        }
    )
    if not _check(
        "gmgn read stays off and a fixture does not call out",
        gmgn_off and sample["live_call"] is False,
        "disabled",
    ):
        problems.append("gmgn")

    try:
        _buy(
            tmp / "live",
            limits=_limits(
                live=True,
                deadline_oslo=datetime.fromisoformat("2026-11-09T18:00:00+01:00"),
            ),
        )
        live_quote = False
    except PolicyError as exc:
        live_quote = "price feed" in str(exc) or "fixture" in str(exc)
    if not _check("live mode refuses the fixture quote", live_quote, "live"):
        problems.append("live")

    check = subprocess.run(  # noqa: S603
        [sys.executable, str(_CLI), "check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if not _check(
        "check command stays on the safe config",
        check.returncode == 0
        and "submit=False" in check.stdout
        and "stop=True" in check.stdout
        and "solana_send is not called" in check.stdout,
        check.stdout.strip().splitlines()[-1] if check.stdout else check.stderr,
    ):
        problems.append("cli check")
    missing_fx = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(_CLI),
            "request",
            "buy",
            "--fixture",
            "--state",
            str(tmp / "cli-state.json"),
            "--out",
            str(tmp / "no-fx.json"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if not _check(
        "buy without FX fails closed",
        missing_fx.returncode == 1 and "fx" in missing_fx.stderr.lower(),
        missing_fx.stderr.strip(),
    ):
        problems.append("cli fx")

    if problems:
        print(f"FAILED {len(problems)} check(s)")
        sys.exit(1)
    print("HARNESS PASS")
    print("live_submit: False")
    print("unattended_snipe: False")


if __name__ == "__main__":
    main()
