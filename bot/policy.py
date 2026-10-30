import re
from dataclasses import dataclass
from typing import Literal

from bot.amounts import parse_amount
from bot.errors import PolicyError

_SENDISH = re.compile(r"\b(send|pay|transfer)\b", re.I)
_VAGUE = re.compile(r"\b(all|everything|some)\b", re.I)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_AT = re.compile(r"@[A-Za-z0-9_]+")


@dataclass(frozen=True)
class EntityRef:
    kind: str
    text: str
    username: str | None = None
    telegram_id: int | None = None
    first_name: str | None = None


@dataclass(frozen=True)
class Intent:
    action: str
    amount: str | None
    token: str | None
    recipient: str | None
    reject_reason: str | None


@dataclass(frozen=True)
class SendDecision:
    amount: int
    mode: Literal["user", "picker"]
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None


def precheck_text(text: str) -> str | None:
    if _SENDISH.search(text) and _VAGUE.search(text):
        return "Say an exact USDC amount. “All” and “some” are not accepted."
    if len(_NUMBER.findall(text)) > 1:
        return "Name one amount."
    if len(_AT.findall(text)) > 1:
        return "Name one recipient."
    return None


def _match(recipient: str, entities: list[EntityRef]) -> EntityRef | None:
    want = recipient.lstrip("@").casefold()
    for entity in entities:
        if entity.kind == "mention" and entity.username and entity.username.casefold() == want:
            return entity
        if entity.kind == "text_mention" and entity.text.casefold() == recipient.casefold():
            return entity
    return None


def decide_send(text: str, intent: Intent, entities: list[EntityRef]) -> SendDecision:
    reason = precheck_text(text)
    if reason:
        raise PolicyError(reason)
    if intent.reject_reason:
        raise PolicyError(intent.reject_reason)
    if intent.action != "send":
        raise PolicyError("That is not a transfer.")
    mentions = [entity for entity in entities if entity.kind in {"mention", "text_mention"}]
    if len(mentions) > 1:
        raise PolicyError("Name one recipient.")
    token = (intent.token or "USDC").upper()
    if token != "USDC":
        raise PolicyError("Only USDC is supported.")
    if not intent.amount:
        raise PolicyError("Say how much USDC to send.")
    try:
        amount = parse_amount(intent.amount)
    except ValueError as exc:
        raise PolicyError(str(exc)) from exc
    if intent.recipient:
        matched = _match(intent.recipient, mentions)
        if matched is not None:
            if matched.kind == "mention":
                return SendDecision(
                    amount=amount,
                    mode="user",
                    username=matched.username,
                    first_name=matched.first_name,
                )
            return SendDecision(
                amount=amount,
                mode="user",
                telegram_id=matched.telegram_id,
                first_name=matched.first_name,
            )
        if mentions:
            raise PolicyError("That recipient is not the person named in the message.")
        return SendDecision(amount=amount, mode="picker")
    if mentions:
        raise PolicyError("Say who to pay with @name, or use the contact button.")
    return SendDecision(amount=amount, mode="picker")
