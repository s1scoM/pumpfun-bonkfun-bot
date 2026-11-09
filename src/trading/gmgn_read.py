# ruff: noqa: TRY003
"""Optional GMGN read fixture. Disabled unless the executor config says so.

This does not create an API key, read `GMGN_PRIVATE_KEY`, import the gmgn CLI,
or call `openapi.gmgn.ai`. Trade routes are not implemented.
"""

from __future__ import annotations

from typing import Any

from trading.phantom_executor import PolicyError


class GmgnReadOnly:
    """A switch that stays off. Fixtures are explicit dictionaries."""

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled

    def token_security(self, address: str) -> dict[str, Any]:
        """Refuse a live lookup. `address` is unused while the switch is off."""
        del address
        if not self.enabled:
            raise PolicyError("gmgn read is disabled and no key is configured")
        raise PolicyError(
            "gmgn read does not call the network; pass a labeled fixture"
        )

    def token_security_fixture(self, document: dict[str, Any]) -> dict[str, Any]:
        """Return a caller-supplied fixture once the switch is on."""
        if not self.enabled:
            raise PolicyError("gmgn read is disabled and no key is configured")
        if document.get("label") != "GMGN_FIXTURE_NOT_A_LIVE_RESPONSE":
            raise PolicyError("gmgn fixture is missing its label")
        return {
            "label": document["label"],
            "address": document.get("address"),
            "source": "caller_fixture",
            "live_call": False,
        }
