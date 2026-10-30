from datetime import datetime

from bot.amounts import format_amount
from bot.models import User, Wallet


def short_address(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def tx_link(chain_id: int, tx_hash: str) -> str:
    if chain_id == 80002:
        return f"https://amoy.polygonscan.com/tx/{tx_hash}"
    return tx_hash


def mined_text(chain_id: int, tx_hash: str) -> str:
    return f"Sent. {tx_link(chain_id, tx_hash)}"


def failed_transfer_text(reason: str) -> str:
    if reason == "cancelled":
        return "The transfer was cancelled before it was mined. Nothing was sent."
    if reason == "reverted":
        return "The transfer failed on chain. Nothing was sent."
    return reason


def paused_text() -> str:
    return "The relayer is paused. Nothing was sent."


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
