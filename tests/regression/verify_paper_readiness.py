"""Offline paper review: Phantom cannot sign for this bot, and the bot has no capital kill switch.

The shipped trader builds a `solders` keypair from `private_key` or a keystore
and submits the signed transaction itself. A public address, including the
stated Phantom embedded-agent address, is 32 bytes and is rejected. Nothing
here opens a socket or signs. Product findings that say FAIL are the expected
review result; the harness passes only when it reports those findings.

  A. A 32-byte public key is not a wallet, and `build_and_send_transaction`
     requires a `Keypair`.
  B. `bots/paper-example.yaml` is disabled, has no key, and fails config load.
  C. `bots/bot-sniper-1-geyser.yaml` is enabled and names a private-key env var.
  D. The paper cost sketch fits the stated 0.223224 SOL / 183 NOK note, and
     the advance decision is still no: no external signer, no capital cap, no
     fee reserve, no session deadline, no rug precheck, no deadline after a
     failed sell, and the live balance was not read.
  E. A fixture position hits stop loss and max hold; `time_based` sleeps
     `wait_after_buy` and then gives up.

Usage:
    uv run tests/regression/verify_paper_readiness.py
"""

from __future__ import annotations

import ast
import inspect
import socket
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Block connects before any project import. File reads still work.
_CONNECT_BLOCKED = "NETWORK_DISABLED"


def _refuse_connect(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError(_CONNECT_BLOCKED)


def _refuse_socket_connect(
    _self: socket.socket, *_args: object, **_kwargs: object
) -> None:
    _refuse_connect()


socket.socket.connect = _refuse_socket_connect  # type: ignore[method-assign]
socket.create_connection = _refuse_connect  # type: ignore[assignment]

import yaml  # noqa: E402
from _fakes import _check  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from config_loader import REQUIRED_FIELDS, load_bot_config  # noqa: E402
from core.client import SolanaClient  # noqa: E402
from core.wallet import Wallet  # noqa: E402
from trading.paper_policy import (  # noqa: E402
    EXAMPLE_LABEL,
    FAIL,
    PASS,
    UNTESTED,
    PaperCase,
    review,
    suitable_to_advance,
)
from trading.position import ExitReason, Position  # noqa: E402

# Public address the owner stated for the Phantom embedded agent. Not a key.
STATED_PHANTOM_ADDRESS = "8rRjKEhJYoYJmG3AVz64xq1cj9LUpqr77j9My5T26BDj"
STATED_BALANCE_SOL = 0.223224
STATED_NOK_NOTE = 183.0
CAPITAL_CAP_NOK = 200.0

_PAPER_YAML = PROJECT_ROOT / "bots" / "paper-example.yaml"
_GEYSER_YAML = PROJECT_ROOT / "bots" / "bot-sniper-1-geyser.yaml"
_POLICY_PATH = PROJECT_ROOT / "src" / "trading" / "paper_policy.py"
_TIME_EXIT_PATH = PROJECT_ROOT / "src" / "trading" / "position_manager.py"

_EXPECTED = {
    "example_label": PASS,
    "network_disabled": PASS,
    "signing_disabled": PASS,
    "no_leverage": PASS,
    "phantom_external_signer": FAIL,
    "bot_capital_cap": FAIL,
    "bot_fee_reserve": FAIL,
    "bot_session_deadline": FAIL,
    "rug_or_sellability_precheck": FAIL,
    "unsold_position_deadline": FAIL,
    "shipped_config_stays_off": FAIL,
    "hypothetical_cost_fits_stated_balance": PASS,
    "live_balance": UNTESTED,
    "nok_valuation": UNTESTED,
    "provider_invoice": UNTESTED,
    "chain_parameters_reread": UNTESTED,
    "profit_target": FAIL,
}


_PUBLIC_KEY_BYTES = 32


def _wallet_rejects(public_key: str) -> bool:
    """True when `Wallet` refuses a public key for being 32 bytes, not 64."""
    try:
        Wallet(public_key)
    except ValueError as exc:
        return "64" in str(exc)
    return False


def _source_has_no_network(path: Path) -> bool:
    """True when the module neither connects nor signs."""
    tree = ast.parse(path.read_text())
    banned = {"socket", "Keypair", "send_transaction", "urlopen", "aiohttp"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in banned for alias in node.names):
                return False
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in banned:
                return False
            if any(alias.name in banned for alias in node.names):
                return False
    return "SOLANA_PRIVATE_KEY" not in path.read_text()


def _paper_case() -> PaperCase:
    return PaperCase(
        label=EXAMPLE_LABEL,
        network_enabled=False,
        signing_enabled=False,
        leverage=False,
        signer="phantom_external",
        bot_has_external_signer=False,
        bot_has_capital_cap=False,
        bot_has_fee_reserve=False,
        bot_has_session_deadline=False,
        bot_has_rug_check=False,
        unsold_position_has_deadline=False,
        shipped_bot_can_start=True,
        balance_sol=STATED_BALANCE_SOL,
        balance_is_live_read=False,
        nok_note_for_balance=STATED_NOK_NOTE,
        capital_cap_nok=CAPITAL_CAP_NOK,
        nok_note_is_live=False,
        buy_amount_sol=0.0001,
        buy_slippage=0.3,
        spends_exact_in=False,
        priority_fee_microlamports=1_000_000,
        buy_cu=180_000,
        sell_cu=120_000,
        max_open_positions=1,
        codebase_is_spot=True,
        profit_target_implemented=False,
    )


def _check_signer() -> list[str]:
    problems: list[str] = []
    pubkey = Pubkey.from_string(STATED_PHANTOM_ADDRESS)
    if not _check(
        "stated address is a 32-byte public key",
        len(bytes(pubkey)) == _PUBLIC_KEY_BYTES,
        f"{STATED_PHANTOM_ADDRESS} decodes to {len(bytes(pubkey))} bytes",
    ):
        problems.append("stated address")
    if not _check(
        "Wallet rejects the stated public address",
        _wallet_rejects(STATED_PHANTOM_ADDRESS),
        "Keypair.from_bytes requires 64 bytes",
    ):
        problems.append("stated address rejected")
    params = inspect.signature(SolanaClient.build_and_send_transaction).parameters
    annotation = params["signer_keypair"].annotation
    send_source = inspect.getsource(SolanaClient.build_and_send_transaction)
    if not _check(
        "build_and_send_transaction takes a Keypair and signs locally",
        annotation is Keypair
        and "VersionedTransaction(message, [signer_keypair])" in send_source,
        f"signer_keypair annotation is {getattr(annotation, '__name__', annotation)}",
    ):
        problems.append("signer annotation")
    if not _check(
        "config schema requires private_key and has no external signer",
        "private_key" in REQUIRED_FIELDS and "external_signer" not in REQUIRED_FIELDS,
        ", ".join(REQUIRED_FIELDS),
    ):
        problems.append("required fields")
    return problems


def _check_configs() -> list[str]:
    problems: list[str] = []
    paper = yaml.safe_load(_PAPER_YAML.read_text())
    geyser = yaml.safe_load(_GEYSER_YAML.read_text())
    paper_text = _PAPER_YAML.read_text()
    if not _check(
        "paper example stays disabled",
        paper.get("enabled") is False
        and paper.get("paper", {}).get("label") == EXAMPLE_LABEL
        and paper.get("paper", {}).get("network") == "disabled"
        and paper.get("paper", {}).get("signing") == "disabled"
        and "private_key" not in paper
        and "keystore" not in paper
        and "${" not in paper_text,
        "bots/paper-example.yaml",
    ):
        problems.append("paper yaml")
    try:
        load_bot_config(str(_PAPER_YAML))
        loaded = True
    except ValueError as exc:
        loaded = False
        message = str(exc)
    else:
        message = "config loaded"
    if not _check(
        "paper example cannot load into a trader",
        not loaded and "private_key" in message,
        message,
    ):
        problems.append("paper load")
    if not _check(
        "shipped geyser bot is enabled and key-backed",
        geyser.get("enabled") is True
        and geyser.get("private_key") == "${SOLANA_PRIVATE_KEY}"
        and geyser.get("filters", {}).get("listener_type") == "geyser"
        and geyser.get("trade", {}).get("exit_strategy") == "time_based"
        and geyser.get("filters", {}).get("yolo_mode") is False,
        "bots/bot-sniper-1-geyser.yaml",
    ):
        problems.append("shipped yaml")
    return problems


def _check_policy() -> list[str]:
    problems: list[str] = []
    findings = review(_paper_case())
    by_code = {finding.code: finding for finding in findings}
    if set(by_code) != set(_EXPECTED):
        problems.append(f"finding codes {sorted(by_code)} != {sorted(_EXPECTED)}")
        print(f"  [FAIL] finding codes differ: {sorted(by_code)}")
    print("PRODUCT")
    for code, expected in _EXPECTED.items():
        found = by_code.get(code)
        status = found.status if found else "MISSING"
        detail = found.detail if found else "missing"
        print(f"  [{status}] {code}: {detail}")
        if status != expected:
            problems.append(code)
            print(f"    pin expected {expected}")
    advance = suitable_to_advance(findings)
    print(f"suitable_to_advance: {advance}")
    if advance:
        problems.append("suitable_to_advance")
    return problems


def _check_exits() -> list[str]:
    problems: list[str] = []
    mint = Pubkey.from_string(STATED_PHANTOM_ADDRESS)
    position = Position(
        mint=mint,
        symbol="EXAMPLE",
        entry_price=1.0,
        quantity=20.0,
        entry_time=datetime(2020, 1, 1),
        take_profit_price=1.5,
        stop_loss_price=0.8,
        max_hold_time=5,
    )
    hit, reason = position.should_exit(0.7)
    if not _check(
        "fixture stop loss",
        hit and reason is ExitReason.STOP_LOSS,
        f"{hit} {reason}",
    ):
        problems.append("stop loss")
    held, hold_reason = position.should_exit(1.1)
    if not _check(
        "fixture max hold",
        held and hold_reason is ExitReason.MAX_HOLD_TIME,
        f"{held} {hold_reason}",
    ):
        problems.append("max hold")
    source = _TIME_EXIT_PATH.read_text()
    if not _check(
        "time_based exit sleeps wait_after_buy then gives up",
        "self.wait_time_after_buy" in source
        and "Giving up on selling" in source
        and "Position stays open" in source,
        "position_manager._handle_time_based_exit",
    ):
        problems.append("time exit")
    if not _check(
        "paper policy module does not sign or connect",
        _source_has_no_network(_POLICY_PATH),
        str(_POLICY_PATH.relative_to(PROJECT_ROOT)),
    ):
        problems.append("policy imports")
    return problems


def main() -> None:
    """Run the paper review and exit non-zero when a pin moves."""
    print("PAPER READINESS (network and signing disabled)")
    print(f"fixture: {EXAMPLE_LABEL}")
    problems = [
        *_check_signer(),
        *_check_configs(),
        *_check_policy(),
        *_check_exits(),
    ]
    if problems:
        print(f"FAILED {len(problems)} check(s)")
        sys.exit(1)
    print("HARNESS PASS")


if __name__ == "__main__":
    main()
