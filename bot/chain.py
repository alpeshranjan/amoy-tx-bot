import asyncio
import re
from dataclasses import dataclass

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import (
    ContractLogicError,
    PersistentConnectionError,
    ProviderConnectionError,
    RequestTimedOut,
    TimeExhausted,
    TooManyRequests,
    TransactionNotFound,
    Web3RPCError,
)

from bot.abi import ROUTER_ABI, USDC_ABI
from bot.config import TOKEN_NAME, TOKEN_VERSION, Settings
from bot.errors import FeeCeiling, StartupError

AMOY_CHAIN_ID = 80002
AMOY_MIN_PRIORITY_FEE = 25 * 10**9
LOCAL_PRIORITY_FEE = 2 * 10**9
_URL = re.compile(r"https?://\S+")


def classify_rpc(exc: BaseException) -> str:
    message = str(exc).lower()
    if "already known" in message:
        return "already_known"
    if "nonce too low" in message:
        return "nonce_too_low"
    if "replacement transaction underpriced" in message or "underpriced" in message:
        return "underpriced"
    if "insufficient funds" in message:
        return "insufficient_funds"
    return "other"


def is_retryable_rpc(exc: BaseException) -> bool:
    """A lost connection, a timeout, a rate limit, or a JSON-RPC error from the node.

    A contract revert is not. The worker keeps running and tries the tick again.
    """
    if isinstance(exc, ContractLogicError):
        return False
    if isinstance(
        exc,
        (
            Web3RPCError,
            OSError,
            TimeoutError,
            ProviderConnectionError,
            TimeExhausted,
            TooManyRequests,
            RequestTimedOut,
            PersistentConnectionError,
        ),
    ):
        return True
    module = type(exc).__module__
    return module.startswith("requests.") or module.startswith("urllib3.")


def simulation_failure_counts(exc: BaseException) -> bool:
    """A JSON-RPC error other than a rate limit. A dropped connection does not count."""
    if not isinstance(exc, Web3RPCError):
        return False
    message = str(exc).lower()
    return "rate limit" not in message and "too many requests" not in message and "429" not in message


def revert_text(exc: BaseException) -> str:
    message = _URL.sub("[rpc]", str(exc)).replace("execution reverted:", "").strip(" :")
    if not message or message.lower() == "execution reverted":
        return "The transfer would revert."
    return message[:300]


def starting_priority_fee(chain_id: int, suggested: int | None, ceiling: int) -> int:
    if chain_id == AMOY_CHAIN_ID:
        priority = max(int(suggested or 0), AMOY_MIN_PRIORITY_FEE)
    else:
        priority = LOCAL_PRIORITY_FEE
    if priority > ceiling:
        raise FeeCeiling("Starting priority fee is above MAX_PRIORITY_FEE_PER_GAS.")
    return priority


@dataclass(frozen=True)
class SignedRaw:
    raw: bytes
    tx_hash: str
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    calldata: bytes
    to: str
    gas_limit: int
    nonce: int
    permit_deadline: int | None


class Chain:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        self.relayer = Account.from_key(settings.relayer_private_key)
        self.usdc = self.w3.eth.contract(
            address=to_checksum_address(settings.usdc_address), abi=USDC_ABI
        )
        self.router = self.w3.eth.contract(
            address=to_checksum_address(settings.router_address), abi=ROUTER_ABI
        )
        self._lock = asyncio.Lock()

    async def _call(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    async def assert_boot(self) -> None:
        chain_id = await self._call(lambda: self.w3.eth.chain_id)
        if chain_id != self.settings.chain_id:
            raise StartupError(f"RPC chain id {chain_id} does not match CHAIN_ID.")
        relayer = await self._call(lambda: self.router.functions.relayer().call())
        token = await self._call(lambda: self.router.functions.token().call())
        if relayer.lower() != self.relayer.address.lower():
            raise StartupError("router.relayer() does not match the relayer key.")
        if token.lower() != self.settings.usdc_address.lower():
            raise StartupError("router.token() does not match USDC_ADDRESS.")
        role = await self._call(lambda: self.usdc.functions.MINTER_ROLE().call())
        allowed = await self._call(
            lambda: self.usdc.functions.hasRole(role, self.relayer.address).call()
        )
        if not allowed:
            raise StartupError("Relayer is missing MINTER_ROLE.")

    async def transaction_count(self) -> int:
        return await self._call(
            lambda: self.w3.eth.get_transaction_count(self.relayer.address, "latest")
        )

    async def block_timestamp(self) -> int:
        block = await self._call(lambda: self.w3.eth.get_block("latest"))
        return int(block["timestamp"])

    async def receipt(self, tx_hash: str):
        def _get():
            try:
                return self.w3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                return None

        return await self._call(_get)

    async def usdc_balance(self, address: str) -> int:
        return await self._call(lambda: self.usdc.functions.balanceOf(to_checksum_address(address)).call())

    async def pol_balance(self) -> int:
        return await self._call(lambda: self.w3.eth.get_balance(self.relayer.address))

    async def max_transfer(self) -> int:
        return await self._call(lambda: self.router.functions.maxTransfer().call())

    async def permit_nonce(self, owner: str) -> int:
        return await self._call(lambda: self.usdc.functions.nonces(to_checksum_address(owner)).call())

    def sign_permit(
        self, private_key: bytes, owner: str, amount: int, nonce: int, deadline: int
    ) -> tuple[int, bytes, bytes]:
        typed = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Permit": [
                    {"name": "owner", "type": "address"},
                    {"name": "spender", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ],
            },
            "primaryType": "Permit",
            "domain": {
                "name": TOKEN_NAME,
                "version": TOKEN_VERSION,
                "chainId": self.settings.chain_id,
                "verifyingContract": self.usdc.address,
            },
            "message": {
                "owner": to_checksum_address(owner),
                "spender": self.router.address,
                "value": amount,
                "nonce": nonce,
                "deadline": deadline,
            },
        }
        signed = Account.sign_message(encode_typed_data(full_message=typed), private_key)
        return signed.v, signed.r.to_bytes(32, "big"), signed.s.to_bytes(32, "big")

    def encode_transfer(self, owner: str, to: str, amount: int, deadline: int, v: int, r: bytes, s: bytes) -> bytes:
        encoded = self.router.encode_abi(
            "transferWithPermit",
            [to_checksum_address(owner), to_checksum_address(to), amount, deadline, v, r, s],
        )
        return bytes(HexBytes(encoded))

    def encode_mint(self, to: str, amount: int) -> bytes:
        return bytes(HexBytes(self.usdc.encode_abi("mint", [to_checksum_address(to), amount])))

    async def initial_fees(self) -> tuple[int, int]:
        def _fees() -> tuple[int, int]:
            block = self.w3.eth.get_block("latest")
            base = int(block.get("baseFeePerGas") or self.w3.eth.gas_price or 1)
            suggested = None
            if self.settings.chain_id == AMOY_CHAIN_ID:
                suggested = int(self.w3.eth.max_priority_fee)
            priority = starting_priority_fee(
                self.settings.chain_id, suggested, self.settings.max_priority_fee_per_gas
            )
            max_fee = min(base * 2 + priority, self.settings.max_fee_per_gas)
            if max_fee < priority:
                max_fee = priority
            return max_fee, priority

        return await self._call(_fees)

    def bump_fees(self, max_fee: int, priority: int) -> tuple[int, int]:
        new_priority = priority * 110 // 100 + 1
        new_max = max_fee * 110 // 100 + 1
        if new_max < new_priority:
            new_max = new_priority
        if new_priority > self.settings.max_priority_fee_per_gas or new_max > self.settings.max_fee_per_gas:
            from bot.errors import FeeCeiling

            raise FeeCeiling("Fee bump would exceed the configured maximum.")
        return new_max, new_priority

    def sign_transaction(
        self,
        *,
        nonce: int,
        to: str,
        calldata: bytes,
        gas_limit: int,
        max_fee: int,
        priority: int,
        permit_deadline: int | None,
    ) -> SignedRaw:
        tx = {
            "chainId": self.settings.chain_id,
            "nonce": nonce,
            "to": to_checksum_address(to),
            "value": 0,
            "data": calldata,
            "gas": gas_limit,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
            "type": 2,
        }
        signed = Account.sign_transaction(tx, self.relayer.key)
        return SignedRaw(
            raw=bytes(signed.raw_transaction),
            tx_hash=signed.hash.to_0x_hex(),
            max_fee_per_gas=max_fee,
            max_priority_fee_per_gas=priority,
            calldata=calldata,
            to=to_checksum_address(to),
            gas_limit=gas_limit,
            nonce=nonce,
            permit_deadline=permit_deadline,
        )

    async def estimate_gas(self, to: str, calldata: bytes) -> int:
        def _estimate() -> int:
            gas = self.w3.eth.estimate_gas(
                {"from": self.relayer.address, "to": to_checksum_address(to), "data": calldata, "value": 0}
            )
            return int(gas * 12 // 10) + 1000

        return await self._call(_estimate)

    async def eth_call(self, to: str, calldata: bytes, gas_limit: int) -> None:
        def _call() -> None:
            self.w3.eth.call(
                {
                    "from": self.relayer.address,
                    "to": to_checksum_address(to),
                    "data": calldata,
                    "gas": gas_limit,
                    "value": 0,
                }
            )

        await self._call(_call)

    async def send_raw(self, raw: bytes) -> None:
        def _send() -> None:
            self.w3.eth.send_raw_transaction(raw)

        await self._call(_send)
