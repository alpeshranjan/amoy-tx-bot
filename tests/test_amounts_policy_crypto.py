import uuid

import pytest

import requests

from bot.amounts import parse_amount
from bot.chain import classify_rpc, is_retryable_rpc, revert_text, starting_priority_fee
from bot.crypto import decrypt_key, encrypt_key
from bot.parser import fallback_intent
from bot.policy import EntityRef, Intent, PolicyError, decide_send, precheck_text


def test_parse_amount_accepts_plain_decimals():
    assert parse_amount("5") == 5_000_000
    assert parse_amount("5.5") == 5_500_000
    assert parse_amount("0.000001") == 1


@pytest.mark.parametrize("raw", ["", "0", "-1", "1e2", "1E2", "5.0000001", "1.1234567", "five", "1.", ".5"])
def test_parse_amount_rejects_bad_strings(raw):
    with pytest.raises(ValueError):
        parse_amount(raw)


def test_fallback_and_mention_decision():
    intent = fallback_intent("send 5 USDC to @maya")
    decision = decide_send(
        "send 5 USDC to @maya",
        intent,
        [EntityRef("mention", "@maya", username="maya")],
    )
    assert decision.amount == 5_000_000
    assert decision.username == "maya"
    assert decision.mode == "user"


def test_text_mention_is_a_recipient():
    intent = Intent("send", "2", "USDC", "Maya", None)
    decision = decide_send(
        "send 2 USDC to Maya",
        intent,
        [EntityRef("text_mention", "Maya", telegram_id=42, first_name="Maya")],
    )
    assert decision.telegram_id == 42


def test_invented_recipient_without_entity_opens_picker():
    intent = Intent("send", "2", "USDC", "maya", None)
    decision = decide_send("send 2 USDC to maya", intent, [])
    assert decision.mode == "picker"


def test_username_digits_are_not_a_second_amount():
    assert precheck_text("send 5 USDC to @maya2") is None
    intent = fallback_intent("send 5 USDC to @maya2")
    decision = decide_send(
        "send 5 USDC to @maya2",
        intent,
        [EntityRef("mention", "@maya2", username="maya2")],
    )
    assert decision.username == "maya2"
    assert decision.amount == 5_000_000


def test_all_is_vague_only_when_no_amount_is_present():
    assert precheck_text("send all USDC to @maya")
    assert precheck_text("send some USDC to @maya")
    assert precheck_text("send 5 USDC to @maya for all the help") is None


def test_rejects_ambiguous_sends():
    assert precheck_text("send all USDC to @maya")
    assert precheck_text("send some USDC to @maya")
    assert precheck_text("send 5 USDC to @maya and 3 to @leo")
    assert precheck_text("send 5 USDC to @maya and @leo")
    with pytest.raises(PolicyError):
        decide_send(
            "send 5 USDC to @leo",
            Intent("send", "5", "USDC", "maya", None),
            [EntityRef("mention", "@leo", username="leo")],
        )
    with pytest.raises(PolicyError):
        decide_send("send 5 DAI to @maya", Intent("send", "5", "DAI", "maya", None), [EntityRef("mention", "@maya", username="maya")])


def test_encryption_round_trip_and_binding():
    user_id = uuid.uuid4()
    key = "ab" * 32
    secret = bytes(range(32))
    ciphertext, nonce = encrypt_key(key, user_id, secret)
    assert decrypt_key(key, user_id, ciphertext, nonce) == secret
    with pytest.raises(Exception):
        decrypt_key(key, uuid.uuid4(), ciphertext, nonce)
    tampered = bytearray(ciphertext)
    tampered[0] ^= 0xFF
    with pytest.raises(Exception):
        decrypt_key(key, user_id, bytes(tampered), nonce)


def test_amoy_priority_fee_starts_at_25_gwei():
    ceiling = 50 * 10**9
    assert starting_priority_fee(80002, 1, ceiling) == 25 * 10**9
    assert starting_priority_fee(80002, 30 * 10**9, ceiling) == 30 * 10**9
    assert starting_priority_fee(31337, 30 * 10**9, ceiling) == 2 * 10**9
    with pytest.raises(Exception):
        starting_priority_fee(80002, 1, 10**9)


def test_revert_text_drops_rpc_urls():
    text = revert_text(
        RuntimeError("execution reverted: AmountOutOfRange https://polygon-amoy.g.alchemy.com/v2/secret")
    )
    assert "secret" not in text
    assert "http" not in text
    assert "AmountOutOfRange" in text


def test_connection_errors_are_retried_and_reverts_are_not():
    from web3.exceptions import ContractLogicError, TooManyRequests, Web3RPCError

    assert is_retryable_rpc(requests.exceptions.ConnectionError("https://example.invalid/v2/secret"))
    assert is_retryable_rpc(TooManyRequests("slow down"))
    assert is_retryable_rpc(Web3RPCError("429 too many requests"))
    assert is_retryable_rpc(Web3RPCError("gas required exceeds allowance")) is False
    assert is_retryable_rpc(ContractLogicError("execution reverted")) is False


def test_rpc_error_map():
    assert classify_rpc(RuntimeError("already known")) == "already_known"
    assert classify_rpc(RuntimeError("nonce too low")) == "nonce_too_low"
    assert classify_rpc(RuntimeError("replacement transaction underpriced")) == "underpriced"
    assert classify_rpc(RuntimeError("insufficient funds for gas * price + value")) == "insufficient_funds"
