"""Verify the copytrade exit: sell when the copied wallet sells.

Offline machine checks, no network, no funds, no keys. The real
`PositionManager._handle_tp_sl_exit` / `_monitor_position_until_exit` run
against a stub price feed, a stub seller and a scripted watcher, and the real
`SourceSellWatcher` runs against a scripted RPC.

  1. parse_wallet_sell: a partial sale, a full sale, via any venue, counts; a
     buy, a failed tx, a tx the wallet did not sign, another mint do not.
  2. The watcher starts `until` their buy signature, is not blocked by a
     transaction the node never serves, survives RPC failures, and finds a
     sale by balance drop alone (and ignores a balance never seen > 0).
  3. Copy exit config: a position with a source wallet has no take profit, no
     trailing stop, the max-hold override, and keeps its stop loss; without a
     source wallet (or with the feature off) it keeps the normal exits.
  4. The monitor sells the WHOLE position with reason copy_exit as soon as the
     watcher reports their sale: also with the price feed down, and with the
     take profit that used to fire at +30% no longer firing.
  5. A failing exit sell is retried more than 3 times with backoff, then lands.
  6. The source wallet and their buy signature survive the positions file.

Usage:
    PYTHONPATH=src python tests/regression/verify_copy_exit.py
"""

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

import trading.signal_entries as se  # noqa: E402
from interfaces.core import Platform, TokenInfo, TradeFailureReason  # noqa: E402
from platforms import PlatformImplementations  # noqa: E402
from trading import trade_log  # noqa: E402
from trading.base import TradeResult  # noqa: E402
from trading.position import ExitReason  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import decode_token_info, encode_token_info  # noqa: E402

WALLET = "Cv5GgkpXvtXcsrMcKCJvx46BpMjiM61mpPU1j8RbbbVL"
OTHER = "6qaQP9vbbeaew8b8Ni1C74rEKZenZaX1jspbCjJSxJkg"
MINT = "DXPLiLoo4bHVD32BJcHJRK4Wt1nznNLuUWwsQb94pump"
MINT2 = "9kpJGaWGx6FjRE4vqotma9eCaAWevgzSAvQvahkrpump"
BUY_SIG = "4MivJ9LiW4xrfWGj8v2AwMuHNee4dr1pxu3XXYzwvpWagu5duuALWtZfBZC5mbnXdYiMH2x9fP3fpP6tAXNhWyqe"
ENTRY = 1.0e-6
QTY = 1_000_000.0
RESULTS: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n      {detail}" if detail else ""))


def tx(*, signer=WALLET, err=None, mint=MINT, pre=0.0, post=0.0, pre_sol=5.0, post_sol=5.0):
    def bal(owner, amt):
        return [{"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amt}}]
    return {
        "blockTime": 1794255828,
        "transaction": {"message": {"accountKeys": [
            {"pubkey": signer, "signer": True}, {"pubkey": "X" * 32, "signer": False}]}},
        "meta": {"err": err, "preBalances": [int(pre_sol * 1e9), 0], "postBalances": [int(post_sol * 1e9), 0],
                 "preTokenBalances": bal(WALLET, pre), "postTokenBalances": bal(WALLET, post)},
    }


def check_parse() -> None:
    sale = se.parse_wallet_sell(WALLET, MINT, tx(pre=100.0, post=0.0, post_sol=7.0))
    check("full sale is a sale", bool(sale) and sale["tokens"] == 100.0 and sale["sol_delta"] == 2.0, str(sale))
    check("partial sale is a sale", bool(se.parse_wallet_sell(WALLET, MINT, tx(pre=100.0, post=60.0))))
    check("a buy is not", se.parse_wallet_sell(WALLET, MINT, tx(pre=0.0, post=50.0)) is None)
    check("a failed tx is not", se.parse_wallet_sell(WALLET, MINT, tx(pre=100.0, post=0.0, err={"x": 1})) is None)
    check("a tx the wallet did not sign is not",
          se.parse_wallet_sell(WALLET, MINT, tx(signer=OTHER, pre=100.0, post=0.0)) is None)
    check("another mint is not", se.parse_wallet_sell(WALLET, MINT2, tx(pre=100.0, post=0.0)) is None)


class ScriptedRpc:
    """Stands in for SourceSellWatcher._rpc."""

    def __init__(self, sigs_by_round, txs, balances):
        self.sigs_by_round, self.txs, self.balances = list(sigs_by_round), txs, list(balances)
        self.calls: list[tuple[str, list]] = []
        self.round = 0

    async def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "getSignaturesForAddress":
            self.round += 1
            if self.sigs_by_round:
                nxt = self.sigs_by_round.pop(0)
                return nxt  # may be None: an RPC failure
            return []
        if method == "getTransaction":
            return self.txs.get(params[0])
        if method == "getMultipleAccounts":
            assert len(params[0]) == 2 and params[1]["encoding"] == "jsonParsed"
            b = self.balances.pop(0) if self.balances else self.balances_last
            self.balances_last = b
            if b is None:
                return None
            acct = {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(b)}}}}}
            return {"value": [None, None] if b == 0 else [None, acct]}
        raise AssertionError(method)


def watcher(rpc, since_sig=BUY_SIG, **kw):
    w = se.SourceSellWatcher("http://stub", WALLET, MINT, since_sig=since_sig, since_ts=None,
                             poll_seconds=0.01, label="R4", **kw)
    w._rpc = rpc
    return w


async def check_watcher() -> None:
    sig = lambda s: {"signature": s, "err": None, "blockTime": 1}  # noqa: E731
    rpc = ScriptedRpc(
        [None, [sig("unserved")], [sig("buyish"), sig("unserved")], [sig("sell1"), sig("buyish"), sig("unserved")]],
        {"buyish": tx(pre=0.0, post=10.0), "sell1": tx(pre=10.0, post=4.0)}, [0])
    w = watcher(rpc)
    got = await asyncio.wait_for(w.wait(), 5)
    first = next(c for c in rpc.calls if c[0] == "getSignaturesForAddress")
    check("watcher asks for signatures until their buy signature", first[1][1].get("until") == BUY_SIG, str(first[1][1]))
    check("watcher survives a failed poll and an unservable tx, finds the partial sale",
          got["via"] == "transaction" and got["sig"] == "sell1", str(got))
    # advancing the cursor: after a complete round the next `until` is the newest sig
    # unservable txs: skipped after MAX_FETCH_TRIES, not blocking
    rpc2 = ScriptedRpc([[sig("unserved")]] * 8 + [[sig("sell2"), sig("unserved")]],
                       {"sell2": tx(pre=10.0, post=0.0)}, [0])
    w2 = watcher(rpc2)
    got2 = await asyncio.wait_for(w2.wait(), 5)
    check("an unservable tx does not block a later sale and is given up on", got2["sig"] == "sell2" and w2.skipped == 1,
          f"skipped={w2.skipped}")
    # balance path
    rpc3 = ScriptedRpc([], {}, [None, 0, 500, 800, 800, 300])
    w3 = watcher(rpc3)
    got3 = await asyncio.wait_for(w3.wait(), 5)
    check("balance seen then dropping is a sale (0/unreadable before it are ignored)",
          got3["via"] == "balance" and w3.peak_raw == 800, str(got3))
    rpc4 = ScriptedRpc([], {}, [600, 0])
    w4 = watcher(rpc4)
    got4 = await asyncio.wait_for(w4.wait(), 5)
    check("account gone after being seen is a sale", got4["via"] == "balance")
    rpc5 = ScriptedRpc([], {}, [0])
    w5 = watcher(rpc5)
    try:
        await asyncio.wait_for(w5.wait(), 0.3)
        check("never-seen balance is not a sale", False)
    except TimeoutError:
        check("never-seen balance is not a sale", not w5.sold)


# ------------------------------------------------------------------ manager


class Curve:
    def __init__(self, prices):
        self.prices, self.calls = list(prices), 0

    async def calculate_price(self, _pool):
        self.calls += 1
        if self.prices == "down":
            raise ValueError("rpc down")
        return self.prices[min(self.calls - 1, len(self.prices) - 1)]


class Seller:
    def __init__(self, fail_first=0):
        self.fail_first, self.calls = fail_first, []

    async def execute(self, token_info, token_amount, token_price, token_amount_raw=None):
        self.calls.append((token_amount, token_price, time.monotonic()))
        if len(self.calls) <= self.fail_first:
            return TradeResult(success=False, platform=token_info.platform, error_message="429",
                               failure_reason=TradeFailureReason.REVERTED)
        return TradeResult(success=True, platform=token_info.platform, tx_signature="stub",
                           amount=token_amount, price=token_price)


class FakeWatcher:
    """Reports their sale `after` seconds after it is started."""

    after = 0.2
    instances: list = []

    def __init__(self, rpc_endpoint, wallet, mint, *, since_sig, since_ts, poll_seconds, label=""):
        self.args = (rpc_endpoint, wallet, mint, since_sig, poll_seconds)
        self.label, self.sale = label, None
        FakeWatcher.instances.append(self)

    @property
    def sold(self):
        return self.sale is not None

    async def wait(self):
        await asyncio.sleep(FakeWatcher.after)
        self.sale = {"tokens": 1.0, "sol_delta": 1.0, "ts": time.time(), "sig": "s" * 40, "via": "transaction"}
        return self.sale


def token_info(with_source=True):
    m = Pubkey.from_string("11111111111111111111111111111112")
    add = {"entry": "copytrade", "copytrade": {"source_wallet": WALLET, "source_buy_sig": BUY_SIG,
                                               "source_buy_ts": 1794254106, "label": "kolscan:R4"}} if with_source else {}
    return TokenInfo(name="Stub", symbol="STUB", uri="", mint=m, platform=Platform.PUMP_FUN,
                     bonding_curve=m, user=m, creator=m, additional_data=add)


def manager(prices, seller, cfg=None, backoff=0.0):
    t = object.__new__(PositionManager)
    t.price_check_interval = 0.02
    t.max_exit_sell_attempts = 3
    t.take_profit_percentage, t.stop_loss_percentage = 0.30, 0.50
    t.trailing_stop_percentage, t.max_hold_time = 0.40, 1800
    t.platform_implementations = PlatformImplementations(
        address_provider=None, instruction_builder=None, curve_manager=Curve(prices), event_parser=None)
    t.seller = seller
    t.solana_client = t.wallet = t.priority_fee_manager = t.risk_guard = None
    t.cleanup_mode = "disabled"
    t.cleanup_with_priority_fee = t.cleanup_force_close_with_burn = False
    if cfg is not None:
        t.copy_exit = se.load_copytrade_config(cfg)
        t._exit_backoff, t._exit_backoff_max = backoff, backoff * 4
        t.max_exit_sell_attempts = max(3, t.copy_exit.exit_sell_attempts or 0)
    trade_log.log_trade = lambda *_a, **_k: None
    return t


CFG = {"exit_on_source_sell": True, "disable_take_profit": True, "disable_trailing_stop": True,
       "max_hold_time_s": 3600, "exit_sell_attempts": 8, "exit_poll_seconds": 2.5}


def buy():
    return TradeResult(success=True, platform=Platform.PUMP_FUN, tx_signature="b", amount=QTY, price=ENTRY)


async def run(t, ti, timeout=5.0):
    se.SourceSellWatcher = FakeWatcher
    FakeWatcher.instances.clear()
    captured = {}
    orig = t._monitor_position_until_exit

    async def spy(token_info_, position, watch=None):
        captured["position"], captured["watch"] = position, watch
        await orig(token_info_, position, watch)
    t._monitor_position_until_exit = spy
    async with asyncio.timeout(timeout):
        await t._handle_tp_sl_exit(ti, buy())
    return captured


async def check_manager() -> None:
    # 3+4: price pumps to +300% (old TP fired at +30%); their sale arrives 0.2 s later
    s = Seller()
    t0 = time.monotonic()
    cap = await run(manager([ENTRY * 4.0], s, CFG), token_info())
    p = cap["position"]
    check("copy position: no TP, no trailing, max hold 3600, SL kept",
          p.take_profit_price is None and p.trailing_stop_price is None and p.max_hold_time == 3600
          and abs(p.stop_loss_price - ENTRY * 0.5) < 1e-18, f"tp={p.take_profit_price} sl={p.stop_loss_price}")
    check("price +300% did NOT trigger a take profit; their sale did (copy_exit, whole position)",
          p.exit_reason is ExitReason.COPY_EXIT and len(s.calls) == 1 and s.calls[0][0] == QTY
          and time.monotonic() - t0 >= FakeWatcher.after - 0.01, f"reason={p.exit_reason} sells={len(s.calls)}")
    w = FakeWatcher.instances[0]
    check("watcher built from the saved source wallet and their buy signature",
          w.args[1] == WALLET and w.args[2] == str(token_info().mint) and w.args[3] == BUY_SIG and w.args[4] == 2.5, str(w.args))
    # price feed down: still exits, floored at the last known/entry price
    s = Seller()
    cap = await run(manager("down", s, CFG), token_info())
    check("their sale exits even with the price feed down", cap["position"].exit_reason is ExitReason.COPY_EXIT
          and len(s.calls) == 1 and s.calls[0][1] == ENTRY, f"sells={s.calls and s.calls[0][:2]}")
    # 5: retries > 3 with backoff
    s = Seller(fail_first=5)
    cap = await run(manager([ENTRY], s, CFG, backoff=0.05), token_info(), timeout=10)
    gaps = [b[2] - a[2] for a, b in zip(s.calls, s.calls[1:])]
    check("failing exit sell is retried past 3 attempts, with growing backoff, then lands",
          len(s.calls) == 6 and not cap["position"].is_active and gaps[3] > gaps[0], f"attempts={len(s.calls)} gaps={[round(g, 2) for g in gaps]}")
    # unrecoverable: attempts exhausted -> stops (resume picks it up next pass)
    s = Seller(fail_first=99)
    cap = await run(manager([ENTRY], s, dict(CFG, exit_sell_attempts=5), backoff=0.01), token_info(), timeout=10)
    check("attempts are bounded by exit_sell_attempts", len(s.calls) == 5 and cap["position"].is_active, f"attempts={len(s.calls)}")
    # no source wallet -> normal exits, TP still on
    FakeWatcher.after = 0.2
    s = Seller()
    cap = await run(manager([ENTRY * 2.0], s, CFG), token_info(with_source=False))
    p = cap["position"]
    check("no saved source wallet: normal exits (TP 30% fires, no watcher)",
          cap["watch"] is None and p.take_profit_price is not None and p.exit_reason is ExitReason.TAKE_PROFIT
          and not FakeWatcher.instances, f"reason={p.exit_reason}")
    # feature off -> unchanged
    s = Seller()
    cap = await run(manager([ENTRY * 2.0], s, None), token_info())
    p = cap["position"]
    check("copy exit off: unchanged behaviour (TP, trailing, 1800 s)",
          cap["watch"] is None and p.exit_reason is ExitReason.TAKE_PROFIT and p.trailing_stop_pct == 0.40
          and p.max_hold_time == 1800)
    # SL still protects
    s = Seller()
    FakeWatcher.after = 5.0
    cap = await run(manager([ENTRY * 0.4], s, CFG), token_info())
    check("stop loss -50% still exits when they have not sold", cap["position"].exit_reason is ExitReason.STOP_LOSS,
          f"reason={cap['position'].exit_reason}")
    FakeWatcher.after = 0.2


def check_persistence() -> None:
    back = decode_token_info(encode_token_info(token_info()))
    c = back.additional_data["copytrade"]
    check("source wallet and buy signature survive the positions file",
          c["source_wallet"] == WALLET and c["source_buy_sig"] == BUY_SIG)


async def main() -> int:
    check_parse()
    await check_watcher()
    await check_manager()
    check_persistence()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
