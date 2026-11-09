"""The three reviewed repos are not taken on as dependencies.

sol-trade-sdk signs with a Keypair, eliza reads SOLANA_PRIVATE_KEY, and
OpenAlice is an AGPL broker orchestrator. This check only reads our tree: no
clone, no install, no network.

Usage:
    uv run tests/regression/verify_external_signer_candidates.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402
from _fakes import _check  # noqa: E402

_DOC = PROJECT_ROOT / "docs" / "external-signer-candidates.md"
_PYPROJECT = PROJECT_ROOT / "pyproject.toml"
_CONFIG = PROJECT_ROOT / "bots" / "phantom-executor.yaml"
_EXECUTOR = PROJECT_ROOT / "src" / "trading" / "phantom_executor.py"
_COMMITS = (
    "c717741d9956f248fd4dd74b4523576bfb74d367",
    "67e25f5bfaae98261d52f2298ffa3f22b1f3f065",
    "03284431c68d71ae8461fa181dc77dd59f1e4a60",
)
_NAMES = ("sol-trade-sdk", "eliza", "OpenAlice", "openalice")


def main() -> None:
    """Exit non-zero if a reviewed repo was added or the executor was armed."""
    print("EXTERNAL SIGNER CANDIDATES (not installed)")
    problems: list[str] = []
    doc = _DOC.read_text()
    pyproject = _PYPROJECT.read_text().lower()
    if not _check(
        "reviewed commits are pinned",
        all(commit in doc for commit in _COMMITS),
        _DOC.name,
    ):
        problems.append("commits")
    if not _check(
        "pyproject does not depend on the three repos",
        all(name.lower() not in pyproject for name in _NAMES),
        "pyproject.toml",
    ):
        problems.append("deps")
    config = yaml.safe_load(_CONFIG.read_text())
    if not _check(
        "phantom executor stays stopped",
        config.get("enabled") is False
        and config.get("stop") is True
        and config.get("submit") is False
        and "private_key" not in config,
        _CONFIG.name,
    ):
        problems.append("config")
    source = _EXECUTOR.read_text()
    if not _check(
        "executor still has no keypair",
        "Keypair(" not in source and "submit_enabled=False" in source,
        _EXECUTOR.name,
    ):
        problems.append("executor")
    if problems:
        print(f"FAILED {len(problems)} check(s)")
        sys.exit(1)
    print("HARNESS PASS")
    print("perps_403_fixed: False")


if __name__ == "__main__":
    main()
