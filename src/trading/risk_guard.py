"""Hard risk gates for the local keystore bot: reserve floor and daily caps.

These run before any buy. They do not send transactions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

# Conservative extras on top of trade.buy_amount: priority fee + base fee +
# first ATA rent (~0.002) rounded up. Not a quote — just a safety buffer.
DEFAULT_FEE_RENT_BUFFER_SOL = 0.003


@dataclass
class RiskLimits:
    """Kill-switch thresholds loaded from bot YAML `risk:`."""

    min_sol_reserve: float = 0.02
    daily_max_spend_sol: float = 0.1
    max_losing_trades: int = 3
    fee_rent_buffer_sol: float = DEFAULT_FEE_RENT_BUFFER_SOL
    state_file: str | None = None


@dataclass
class RiskState:
    """Persisted day counters for spend and losing trades."""

    day: str
    spent_sol: float = 0.0
    losing_trades: int = 0
    halted: bool = False
    halt_reason: str | None = None


def _today() -> str:
    return date.today().isoformat()


def load_state(path: Path) -> RiskState:
    """Load today's counters; reset when the calendar day changes."""
    if not path.exists():
        return RiskState(day=_today())
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return RiskState(day=_today())
    if raw.get("day") != _today():
        return RiskState(day=_today())
    return RiskState(
        day=raw["day"],
        spent_sol=float(raw.get("spent_sol", 0.0)),
        losing_trades=int(raw.get("losing_trades", 0)),
        halted=bool(raw.get("halted", False)),
        halt_reason=raw.get("halt_reason"),
    )


def save_state(path: Path, state: RiskState) -> None:
    """Write counters atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "day": state.day,
                "spent_sol": state.spent_sol,
                "losing_trades": state.losing_trades,
                "halted": state.halted,
                "halt_reason": state.halt_reason,
            },
            indent=2,
        )
        + "\n"
    )
    tmp.replace(path)


class RiskGuard:
    """Decides whether a proposed buy is allowed under the kill-switches."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self.path = Path(limits.state_file) if limits.state_file else None
        self.state = load_state(self.path) if self.path else RiskState(day=_today())

    def _persist(self) -> None:
        if self.path is not None:
            save_state(self.path, self.state)

    def halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason
        logger.warning(f"RISK HALT: {reason}")
        self._persist()

    def allow_buy(self, *, balance_sol: float, buy_amount_sol: float) -> str | None:
        """Return a skip reason, or None when the buy may proceed."""
        if self.state.day != _today():
            self.state = RiskState(day=_today())
            self._persist()
        if self.state.halted:
            return f"halted: {self.state.halt_reason or 'risk guard'}"
        if self.state.losing_trades >= self.limits.max_losing_trades:
            self.halt(
                f"max_losing_trades={self.limits.max_losing_trades} reached today"
            )
            return self.state.halt_reason
        projected_spend = self.state.spent_sol + buy_amount_sol
        if projected_spend > self.limits.daily_max_spend_sol:
            self.halt(
                f"daily_max_spend_sol={self.limits.daily_max_spend_sol} would be "
                f"exceeded (spent={self.state.spent_sol:.6f} + buy={buy_amount_sol:.6f})"
            )
            return self.state.halt_reason
        after = (
            balance_sol
            - buy_amount_sol
            - self.limits.fee_rent_buffer_sol
        )
        if after < self.limits.min_sol_reserve:
            return (
                f"reserve floor: balance {balance_sol:.6f} - buy {buy_amount_sol:.6f} "
                f"- fees/rent {self.limits.fee_rent_buffer_sol:.6f} = {after:.6f} "
                f"< min_sol_reserve {self.limits.min_sol_reserve:.6f}"
            )
        return None

    def record_buy_attempt(self, buy_amount_sol: float) -> None:
        """Count spend when a live buy is actually submitted."""
        self.state.spent_sol += buy_amount_sol
        self._persist()

    def record_loss(self) -> None:
        """Count a closed losing trade toward the daily cap."""
        self.state.losing_trades += 1
        if self.state.losing_trades >= self.limits.max_losing_trades:
            self.halt(
                f"max_losing_trades={self.limits.max_losing_trades} reached today"
            )
        else:
            self._persist()
