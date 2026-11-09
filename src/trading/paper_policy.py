"""Offline review of whether a bot config may advance to a live wallet.

The functions below read numbers the caller already has. They do not open a
socket, sign, read a private key, or change what `bot_runner` will do. A
passing cost check is arithmetic on a labeled example, not permission to trade.
"""

from __future__ import annotations

from dataclasses import dataclass

PASS = "PASS"  # noqa: S105 - review status word, not a credential
FAIL = "FAIL"
UNTESTED = "UNTESTED"

LAMPORTS_PER_SOL = 1_000_000_000
MICROLAMPORTS_PER_LAMPORT = 1_000_000
# One signature on a Solana transaction. Not read from a node.
BASE_SIGNATURE_LAMPORTS = 5_000
# Rent-exempt minimum is (account overhead + data length) times this many
# lamports. The parameters have been frozen for years; this path does not
# read the rent sysvar, so a live reread stays UNTESTED.
_RENT_EXEMPT_LAMPORTS_PER_BYTE = 6_960
_ACCOUNT_OVERHEAD_BYTES = 128
_TOKEN_ACCOUNT_DATA_BYTES = 165

EXAMPLE_LABEL = "EXAMPLE_FIXTURE_NOT_LIVE"


@dataclass(frozen=True)
class Finding:
    """One labeled review result."""

    code: str
    status: str
    detail: str


@dataclass(frozen=True)
class CostSketch:
    """Lamports a single open position can lock or spend before a sell returns."""

    quote_ceiling_lamports: int
    priority_buy_lamports: int
    priority_sell_lamports: int
    base_fee_lamports: int
    rent_lamports: int
    failed_buy_lamports: int
    reserved_lamports: int

    @property
    def quote_ceiling_sol(self) -> float:
        """Highest quote the buy instruction may spend, in whole SOL."""
        return self.quote_ceiling_lamports / LAMPORTS_PER_SOL

    @property
    def reserved_sol(self) -> float:
        """Fees plus token-account rent set aside for one position."""
        return self.reserved_lamports / LAMPORTS_PER_SOL


def token_account_rent_lamports() -> int:
    """Rent-exempt lamports for a 165-byte token account, from the frozen formula."""
    return (_ACCOUNT_OVERHEAD_BYTES + _TOKEN_ACCOUNT_DATA_BYTES) * (
        _RENT_EXEMPT_LAMPORTS_PER_BYTE
    )


def priority_fee_lamports(price_microlamports: int, cu_limit: int) -> int:
    """Priority fee in lamports: microlamports per CU times the CU limit.

    Matches `set_compute_unit_price`: the price is microlamports, and
    1 lamport is 1_000_000 microlamports. The chain charges this only up to
    the units the transaction actually consumes; the figure here is the
    ceiling the bot requests, which is what a reserve has to cover.
    """
    if price_microlamports < 0 or cu_limit < 0:
        msg = "priority fee inputs must be non-negative"
        raise ValueError(msg)
    return price_microlamports * cu_limit // MICROLAMPORTS_PER_LAMPORT


def quote_ceiling_lamports(
    buy_amount_sol: float, slippage: float, *, spends_exact_in: bool
) -> int:
    """Most quote a buy may spend, in lamports.

    An exact-in venue spends `buy_amount`. pump.fun `buy_v2` spends up to
    `buy_amount * (1 + slippage)` because the token count is fixed and the
    quote amount is a ceiling. `extreme_fast_mode` still uses that ceiling.
    """
    if buy_amount_sol < 0 or not 0 <= slippage <= 1:
        msg = "buy amount and slippage must be in range"
        raise ValueError(msg)
    sol = buy_amount_sol if spends_exact_in else buy_amount_sol * (1 + slippage)
    return round(sol * LAMPORTS_PER_SOL)


@dataclass(frozen=True)
class TradeSize:
    """The numbers that set one buy's quote ceiling and priority fee."""

    buy_amount_sol: float
    slippage: float
    spends_exact_in: bool
    priority_fee_microlamports: int
    buy_cu: int
    sell_cu: int


def sketch_one_position(size: TradeSize) -> CostSketch:
    """Fee reserve for one buy, one sell, one reverted extra buy, and one ATA."""
    buy_fee = priority_fee_lamports(size.priority_fee_microlamports, size.buy_cu)
    sell_fee = priority_fee_lamports(size.priority_fee_microlamports, size.sell_cu)
    failed = BASE_SIGNATURE_LAMPORTS + buy_fee
    rent = token_account_rent_lamports()
    base = BASE_SIGNATURE_LAMPORTS * 2
    reserved = buy_fee + sell_fee + base + rent + failed
    return CostSketch(
        quote_ceiling_lamports=quote_ceiling_lamports(
            size.buy_amount_sol, size.slippage, spends_exact_in=size.spends_exact_in
        ),
        priority_buy_lamports=buy_fee,
        priority_sell_lamports=sell_fee,
        base_fee_lamports=base,
        rent_lamports=rent,
        failed_buy_lamports=failed,
        reserved_lamports=reserved,
    )


def illustrative_cap_sol(
    capital_cap_nok: float, balance_sol: float, nok_note_for_balance: float
) -> float:
    """SOL equivalent of a NOK cap using a caller-supplied stale note.

    `nok_note_for_balance` is whatever NOK figure was written down next to
    `balance_sol`. It is not a market price.
    """
    if capital_cap_nok <= 0 or balance_sol <= 0 or nok_note_for_balance <= 0:
        msg = "cap, balance and NOK note must be positive"
        raise ValueError(msg)
    return capital_cap_nok * balance_sol / nok_note_for_balance


@dataclass(frozen=True)
class PaperCase:
    """One labeled example. Network and signing flags must stay off."""

    label: str
    network_enabled: bool
    signing_enabled: bool
    leverage: bool
    signer: str
    bot_has_external_signer: bool
    bot_has_capital_cap: bool
    bot_has_fee_reserve: bool
    bot_has_session_deadline: bool
    bot_has_rug_check: bool
    unsold_position_has_deadline: bool
    shipped_bot_can_start: bool
    balance_sol: float
    balance_is_live_read: bool
    nok_note_for_balance: float
    capital_cap_nok: float
    nok_note_is_live: bool
    buy_amount_sol: float
    buy_slippage: float
    spends_exact_in: bool
    priority_fee_microlamports: int
    buy_cu: int
    sell_cu: int
    max_open_positions: int
    codebase_is_spot: bool
    profit_target_implemented: bool


def review(case: PaperCase) -> tuple[Finding, ...]:
    """Classify `case`. FAIL means the bot must not advance on that point."""
    findings: list[Finding] = []

    def add(code: str, status: str, detail: str) -> None:
        findings.append(Finding(code, status, detail))

    if case.label == EXAMPLE_LABEL:
        add("example_label", PASS, EXAMPLE_LABEL)
    else:
        add("example_label", FAIL, f"fixture label is {case.label!r}")

    add(
        "network_disabled",
        PASS if not case.network_enabled else FAIL,
        "paper run does not open a socket"
        if not case.network_enabled
        else "network flag is on",
    )
    add(
        "signing_disabled",
        PASS if not case.signing_enabled else FAIL,
        "paper run does not sign" if not case.signing_enabled else "signing flag is on",
    )
    add(
        "no_leverage",
        PASS if case.codebase_is_spot and not case.leverage else FAIL,
        "spot only; leverage is not a bot setting",
    )
    if case.signer == "phantom_external" and case.bot_has_external_signer:
        signer_status = PASS
        signer_detail = "bot can hand an unsigned transaction to an external signer"
    else:
        signer_status = FAIL
        signer_detail = (
            f"signer={case.signer}; bot_has_external_signer="
            f"{case.bot_has_external_signer}. Live path is "
            "Wallet(base58 64-byte keypair) or an encrypted keystore"
        )
    add("phantom_external_signer", signer_status, signer_detail)
    add(
        "bot_capital_cap",
        PASS if case.bot_has_capital_cap else FAIL,
        "no capital-cap field; trade.buy_amount is the only size",
    )
    add(
        "bot_fee_reserve",
        PASS if case.bot_has_fee_reserve else FAIL,
        "no fee-reserve field; priority fee is a per-CU price with a hard cap",
    )
    add(
        "bot_session_deadline",
        PASS if case.bot_has_session_deadline else FAIL,
        "no session deadline; token_wait_timeout only bounds waiting for a coin",
    )
    add(
        "rug_or_sellability_precheck",
        PASS if case.bot_has_rug_check else FAIL,
        "filters are name, creator, age, quote mint and transfer fee; "
        "no honeypot, liquidity or pre-buy sell simulation",
    )
    add(
        "unsold_position_deadline",
        PASS if case.unsold_position_has_deadline else FAIL,
        "after max_exit_sell_attempts the position stays open with no further deadline",
    )
    add(
        "shipped_config_stays_off",
        FAIL if case.shipped_bot_can_start else PASS,
        "bots/bot-sniper-1-geyser.yaml is enabled: true and listens with geyser",
    )

    sketch = sketch_one_position(
        TradeSize(
            buy_amount_sol=case.buy_amount_sol,
            slippage=case.buy_slippage,
            spends_exact_in=case.spends_exact_in,
            priority_fee_microlamports=case.priority_fee_microlamports,
            buy_cu=case.buy_cu,
            sell_cu=case.sell_cu,
        )
    )
    cap_sol = illustrative_cap_sol(
        case.capital_cap_nok, case.balance_sol, case.nok_note_for_balance
    )
    binding_sol = min(case.balance_sol, cap_sol)
    need_lamports = (
        sketch.quote_ceiling_lamports * case.max_open_positions
        + sketch.reserved_lamports * case.max_open_positions
    )
    need_sol = need_lamports / LAMPORTS_PER_SOL
    fits = need_sol <= binding_sol and case.max_open_positions >= 1
    add(
        "hypothetical_cost_fits_stated_balance",
        PASS if fits else FAIL,
        (
            f"one position quote ceiling {sketch.quote_ceiling_sol:.6f} SOL, "
            f"reserve {sketch.reserved_sol:.6f} SOL "
            f"(priority buy {sketch.priority_buy_lamports} + sell "
            f"{sketch.priority_sell_lamports} + base {sketch.base_fee_lamports} + "
            f"rent {sketch.rent_lamports} + extra failed buy "
            f"{sketch.failed_buy_lamports} lamports); "
            f"{case.max_open_positions} position(s) need {need_sol:.6f} SOL; "
            f"stated balance {case.balance_sol} SOL; illustrative {case.capital_cap_nok:.0f} NOK "
            f"cap is {cap_sol:.6f} SOL at the stale note. Not a live quote"
        ),
    )
    add(
        "live_balance",
        PASS if case.balance_is_live_read else UNTESTED,
        "stated 0.223224 SOL was not read from a node or from Phantom",
    )
    add(
        "nok_valuation",
        PASS if case.nok_note_is_live else UNTESTED,
        "183 NOK note and 200 NOK cap were not repriced",
    )
    add(
        "provider_invoice",
        UNTESTED,
        "RPC and Geyser prices were not queried; the enabled bot requires both",
    )
    add(
        "chain_parameters_reread",
        UNTESTED,
        "rent sysvar and consumed CU were not re-read; reserve uses the frozen "
        "rent formula and the bot's CU limits",
    )
    add(
        "profit_target",
        PASS if case.profit_target_implemented else FAIL,
        "nothing in the bot aims at 1000 NOK or any profit target beyond the exit strategy",
    )
    return tuple(findings)


# These must be PASS before the example may advance. UNTESTED stays blocking:
# a stale balance is not evidence the wallet can pay.
_BLOCKING = (
    "example_label",
    "network_disabled",
    "signing_disabled",
    "no_leverage",
    "phantom_external_signer",
    "bot_capital_cap",
    "bot_fee_reserve",
    "bot_session_deadline",
    "rug_or_sellability_precheck",
    "unsold_position_deadline",
    "shipped_config_stays_off",
    "live_balance",
    "nok_valuation",
    "provider_invoice",
    "profit_target",
)


def suitable_to_advance(findings: tuple[Finding, ...]) -> bool:
    """True only when every blocking finding passed."""
    by_code = {finding.code: finding.status for finding in findings}
    return all(by_code.get(code) == PASS for code in _BLOCKING)
