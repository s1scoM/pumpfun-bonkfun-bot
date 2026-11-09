"""An unsigned v0 envelope can be built from a public key alone.

Phantom `solana_send` takes standard base64. With `confirmed` omitted it
simulates; with `confirmed` true it signs and broadcasts. The embedded wallet
does not hand back a signature for the bot to submit itself. This check builds
one offline fixture and proves the live send path is unchanged:

  A. A 0-lamport self-transfer for the stated public address, with the all-zero
     blockhash, encodes as standard base64 and decodes back to an empty signature.
  B. The fee payer is that address. No keypair is created.
  C. `confirmed` stays false.
  D. `SolanaClient.build_and_send_transaction` still signs with a keypair and
     does not call the envelope helper.

Nothing is sent. The zero blockhash cannot land.

Usage:
    uv run tests/regression/verify_unsigned_phantom_envelope.py
"""

from __future__ import annotations

import ast
import base64
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import _check  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.system_program import TransferParams, transfer  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from core.unsigned_tx import build_phantom_send_envelope  # noqa: E402

# Public address only. The fixture moves nothing and uses a dead blockhash.
STATED_PHANTOM_ADDRESS = "8rRjKEhJYoYJmG3AVz64xq1cj9LUpqr77j9My5T26BDj"
_CLIENT = PROJECT_ROOT / "src" / "core" / "client.py"
_HELPER = PROJECT_ROOT / "src" / "core" / "unsigned_tx.py"


def _helper_imports_no_keypair() -> bool:
    tree = ast.parse(_HELPER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if "keypair" in node.module:
                return False
            if any(alias.name == "Keypair" for alias in node.names):
                return False
    return "Keypair(" not in _HELPER.read_text()


def main() -> None:
    """Build one fixture envelope and exit non-zero if the proof fails."""
    print("UNSIGNED PHANTOM ENVELOPE (not submitted)")
    problems: list[str] = []
    payer = Pubkey.from_string(STATED_PHANTOM_ADDRESS)
    envelope = build_phantom_send_envelope(
        payer,
        [transfer(TransferParams(from_pubkey=payer, to_pubkey=payer, lamports=0))],
        Hash.default(),
    )
    decoded = VersionedTransaction.from_bytes(base64.b64decode(envelope.transaction))
    blank = all(sig == Signature.default() for sig in decoded.signatures)
    if not _check(
        "fixture encodes and has an empty signature",
        envelope.transaction.startswith("AQAA")
        and blank
        and decoded.verify_with_results() == [False],
        f"{len(envelope.transaction)} base64 chars, signature empty",
    ):
        problems.append("envelope")
    if not _check(
        "fee payer is the public address and confirmed stays false",
        envelope.fee_payer == STATED_PHANTOM_ADDRESS
        and envelope.confirmed is False
        and envelope.network_id == "solana:mainnet"
        and decoded.message.account_keys[0] == payer,
        envelope.fee_payer,
    ):
        problems.append("payer")
    if not _check(
        "helper does not construct a keypair",
        _helper_imports_no_keypair(),
        "src/core/unsigned_tx.py",
    ):
        problems.append("helper")
    client = _CLIENT.read_text()
    if not _check(
        "live send path still signs locally and ignores the helper",
        "VersionedTransaction(message, [signer_keypair])" in client
        and "build_phantom_send_envelope" not in client,
        "src/core/client.py",
    ):
        problems.append("live path")
    if problems:
        print(f"FAILED {len(problems)} check(s)")
        sys.exit(1)
    print("HARNESS PASS")
    print("suitable_for_unattended_snipe: False")


if __name__ == "__main__":
    main()
