"""Build an unsigned v0 transaction for an external signer.

Phantom's embedded wallet has no sign-only method. `solana_send` simulates
when `confirmed` is omitted and, with `confirmed` true, both signs and
broadcasts. This function only builds the standard base64 envelope. It does
not sign, send, or choose a blockhash from the network. The live bot still
signs with a keypair in `SolanaClient.build_and_send_transaction`.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from solders.message import MessageV0
from solders.signature import Signature
from solders.transaction import VersionedTransaction

if TYPE_CHECKING:
    from solders.hash import Hash
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

# The alphabet `solana_send` accepts. Standard base64, not base58.
_STANDARD_BASE64 = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")
NETWORK_ID = "solana:mainnet"


@dataclass(frozen=True)
class PhantomSendEnvelope:
    """Arguments a parent can pass to `solana_send` without setting `confirmed`.

    `confirmed` is always false. Flipping it submits the transaction, and that
    step is not this function's job.
    """

    transaction: str
    network_id: str
    confirmed: bool
    fee_payer: str


def build_phantom_send_envelope(
    payer: Pubkey,
    instructions: list[Instruction],
    recent_blockhash: Hash,
) -> PhantomSendEnvelope:
    """Serialize a v0 transaction whose signature slots are empty.

    The fee payer is `payer`. No keypair is read. A zero blockhash, or any
    other hash the caller invented, cannot land on chain.

    Raises:
        ValueError: No instructions, or the bytes are not standard base64.
    """
    if not instructions:
        msg = "at least one instruction is required"
        raise ValueError(msg)
    message = MessageV0.try_compile(payer, instructions, [], recent_blockhash)
    blank = [Signature.default()] * message.header.num_required_signatures
    transaction = VersionedTransaction.populate(message, blank)
    encoded = base64.b64encode(bytes(transaction)).decode("ascii")
    if _STANDARD_BASE64.fullmatch(encoded) is None:
        msg = "envelope is not standard base64"
        raise ValueError(msg)
    if any(signature != Signature.default() for signature in transaction.signatures):
        msg = "envelope carries a signature"
        raise ValueError(msg)
    return PhantomSendEnvelope(
        transaction=encoded,
        network_id=NETWORK_ID,
        confirmed=False,
        fee_payer=str(payer),
    )
