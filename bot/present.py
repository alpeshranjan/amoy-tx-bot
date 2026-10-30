from datetime import datetime

from bot.amounts import format_amount
from bot.models import User, Wallet


def short_address(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def tx_link(chain_id: int, tx_hash: str) -> str:
    if chain_id == 80002:
        return f"https://amoy.polygonscan.com/tx/{tx_hash}"
    return tx_hash


def confirm_text(amount: int, user: User, wallet: Wallet) -> str:
    who = f"@{user.username}" if user.username else (user.first_name or "this person")
    lines = [f"Send {format_amount(amount)} USDC to {who} ({short_address(wallet.address)})?"]
    if user.first_name:
        lines.append(user.first_name)
    if user.username and user.username_observed_at is not None:
        seen = user.username_observed_at.strftime("%d %b %Y")
        lines.append(f"@{user.username}, last seen on this username {seen}")
    return "\n".join(lines)


def observed_label(when: datetime | None) -> str:
    if when is None:
        return "never"
    return when.strftime("%d %b %Y")
