import re
from decimal import Decimal

SCALE = Decimal(10) ** 6
_AMOUNT = re.compile(r"[0-9]+(\.[0-9]{1,6})?")


def parse_amount(raw: str) -> int:
    """Parse a USDC string into 6-decimal base units. Never uses float."""
    if not isinstance(raw, str) or not _AMOUNT.fullmatch(raw):
        raise ValueError("Amount must be a positive number with at most 6 decimal places.")
    value = Decimal(raw)
    if value <= 0:
        raise ValueError("Amount must be positive.")
    return int(value * SCALE)


def format_amount(base_units: int) -> str:
    return f"{Decimal(base_units) / SCALE:.6f}"
