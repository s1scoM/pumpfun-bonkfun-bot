"""Print the offline paper review. Moves no funds and opens no socket.

Usage:
    uv run tools/paper_readiness.py
"""

import runpy
from pathlib import Path


def main() -> None:
    """Run the paper-readiness verifier."""
    script = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "regression"
        / "verify_paper_readiness.py"
    )
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
