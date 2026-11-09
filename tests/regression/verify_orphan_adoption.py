"""Verify the start-up scan for coins held without a position record.

Offline: a fake client serves token accounts, signatures and trade deltas; no
network, no funds, no keys.

  1. parse_token_account reads mint and raw amount.
  2. A held coin the store does not know is rebuilt as a position: amount =
     what is held, cost = what the wallet's buys paid (fees included), entry
     time = the first buy's block time, buy signature kept.
  3. Coins in the store, wrapped SOL, empty accounts, dust (below the value
     floor), non-pump coins and coins with no buy in the history are left alone.
  4. Several buys of one coin are summed into one cost basis.
  5. PositionManager.adopt writes the record first (a restart resumes it) and
     opens the position with the reconstructed entry time.

Usage:
    PYTHONPATH=src python tests/regression/verify_orphan_adoption.py
"""

import asyncio
import struct
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

import trading.orphans as orphans  # noqa: E402
from core.trade_deltas import TradeDeltas  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402

WALLET = Pubkey.from_string("6qaQP9vbbeaew8b8Ni1C74rEKZenZaX1jspbCjJSxJkg")
R = []


def check(label, ok, detail=""):
    R.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n      {detail}" if detail else ""))


def mint(n):
    return Pubkey.from_bytes(bytes([n]) * 32)


def acct(m, amount):
    return bytes(m) + bytes(WALLET) + struct.pack("<Q", amount) + b"\0" * 93


HELD = {
    mint(1): 601_298_699_991,   # orphan, one buy
    mint(2): 100_000_000_000,   # in the store
    mint(3): 5_000_000,         # dust
    mint(4): 90_000_000_000,    # not a pump coin
    mint(5): 80_000_000_000,    # no buy in history
    mint(6): 300_000_000_000,   # two buys
    mint(7): 0,                 # empty
}
PRICE = {mint(1): 3e-8, mint(3): 1e-10, mint(5): 3e-8, mint(6): 1e-7}
BUYS = {  # account address -> [(sig, blockTime, tokens_raw, quote_raw)]
    "acct1": [("sig1", 1794254420, 601_298_699_991, -49_484_566)],
    "acct6": [("sig6b", 1794254500, 100_000_000_000, -20_000_000), ("sig6a", 1794254400, 200_000_000_000, -30_000_000)],
    "acct3": [("sig3", 1794254000, 5_000_000, -1_000_000)],
}
ADDR = {mint(n): f"acct{n}" for n in range(1, 8)}


class FakeClient:
    async def get_token_accounts(self, owner, program):
        if program != orphans.TOKEN_2022_PROGRAM:
            return [(Pubkey.from_string("So11111111111111111111111111111111111111112"), acct(Pubkey.from_string("So11111111111111111111111111111111111111112"), 10**9))]
        return [(ADDR[m], acct(m, a)) for m, a in HELD.items()]

    async def post_rpc(self, body):
        account = body["params"][0]
        rows = BUYS.get(account, [])
        return {"result": [{"signature": s, "blockTime": t, "err": None} for s, t, *_ in rows]}

    async def get_trade_deltas(self, sig, wallet, m, quote):
        for rows in BUYS.values():
            for s, _t, tok, q in rows:
                if s == sig:
                    return TradeDeltas(tokens_raw=tok, quote_raw=q, network_fee_lamports=23000)
        return None

    async def close(self):
        pass


async def fake_resolve(self, mint_str, summary=None):
    m = Pubkey.from_string(mint_str)
    if m == mint(4):
        raise ValueError("no pump.fun bonding curve")
    return TokenInfo(name="N", symbol=f"C{mint_str[:2]}", uri="", mint=m, platform=Platform.PUMP_FUN,
                     additional_data={"onchain_price_sol": PRICE.get(m, 3e-8)})


def main():
    p = orphans.parse_token_account(acct(mint(1), 42))
    check("parse_token_account reads mint and amount", p == (mint(1), 42))

    orphans.MomentumScanner.resolve = fake_resolve
    orphans.MomentumScanner.close = lambda self: asyncio.sleep(0)
    found = asyncio.run(orphans.find_orphans(FakeClient(), WALLET, Platform.PUMP_FUN, {str(mint(2))}))
    got = {str(o.token_info.mint): o for o in found}
    check("only the unknown, real, non-dust, bought coins are adopted",
          set(got) == {str(mint(1)), str(mint(6))}, str(sorted(k[:6] for k in got)))
    o = got[str(mint(1))]
    b = o.buy_result
    check("amount = held, cost = paid + fee, entry = first buy time, signature kept",
          b.amount_raw == 601_298_699_991 and abs(b.price - (49_484_566 + 23000) / 1e9 / 601298.699991) < 1e-15
          and b.tx_signature == "sig1",
          f"price={b.price:.4g} entry={o.entry_time}")
    from datetime import UTC, datetime
    check("entry time is the buy's block time (UTC)",
          o.entry_time == datetime.fromtimestamp(1794254420, UTC).replace(tzinfo=None))
    b6 = got[str(mint(6))].buy_result
    check("two buys are summed into one cost basis and the first signature/time kept",
          b6.deltas.tokens_raw == 300_000_000_000 and b6.deltas.quote_raw == -50_000_000
          and b6.deltas.network_fee_lamports == 46000 and b6.tx_signature == "sig6a",
          str(b6.deltas))

    # adopt
    with tempfile.TemporaryDirectory() as d:
        store = PositionStore(Path(d) / "pos.json")
        mgr = object.__new__(PositionManager)
        mgr.store = store
        mgr.wallet = SimpleNamespace(pubkey=WALLET)
        opened = []
        mgr.open = lambda ti, buy, entry_time=None: opened.append((ti, buy, entry_time))
        mgr.adopt(o)
        stored = PositionStore(Path(d) / "pos.json").load()
        check("adopt writes the record (a restart resumes it) and opens the position",
              len(stored) == 1 and str(stored[0].token_info.mint) == str(mint(1)) and stored[0].wallet == str(WALLET)
              and stored[0].buy_result.amount_raw == 601_298_699_991 and len(opened) == 1
              and opened[0][2] == o.entry_time, f"stored={len(stored)} opened={len(opened)}")
    print(f"\n{sum(R)}/{len(R)} checks passed")
    return 0 if all(R) else 1


if __name__ == "__main__":
    sys.exit(main())
