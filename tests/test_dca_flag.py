"""Feature flags must actually gate their features (regression for ungated DCA)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _dca_block_is_gated(trader_file: str, flag_prefix: str) -> bool:
    src = (ROOT / trader_file).read_text()
    # The line following the DCA anchor comment must check the flag.
    m = re.search(
        r"# DCA follow-up for existing positions\n\s*if (.+):", src
    )
    assert m, f"DCA block not found in {trader_file}"
    return f"{flag_prefix}.USE_DCA_ENTRY" in m.group(1)


def test_pullback_dca_gated_by_flag():
    assert _dca_block_is_gated("traders/crypto_trades/kraken_pullback.py", "PB")


def test_momentum_dca_gated_by_flag():
    assert _dca_block_is_gated("traders/crypto_trades/kraken_momentum.py", "MO")
