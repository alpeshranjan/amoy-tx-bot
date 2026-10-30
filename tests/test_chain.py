import json
import os
import socket
import subprocess
import time

import pytest
from eth_account import Account
from web3 import Web3

from bot.chain import Chain
from bot.config import Settings

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ANVIL = os.path.expanduser("~/.foundry/bin/anvil")
PORT = 18548
DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _wait_port(port: int) -> None:
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("anvil did not start")


def _artifact(name: str) -> tuple[list, str]:
    path = os.path.join(ROOT, "contracts", "out", name)
    data = json.loads(open(path).read())
    return data["abi"], data["bytecode"]["object"]


def _send(w3: Web3, tx: dict, key: str) -> str:
    signed = Account.sign_transaction(tx, key)
    w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(signed.hash)
    assert receipt.status == 1
    return receipt.contractAddress


def _base(w3: Web3, account: str, nonce: int) -> dict:
    return {
        "from": account,
        "nonce": nonce,
        "chainId": 31337,
        "gas": 4_000_000,
        "maxFeePerGas": 2_000_000_000,
        "maxPriorityFeePerGas": 1_000_000_000,
        "type": 2,
    }


@pytest.fixture
def anvil():
    if not os.path.exists(ANVIL):
        pytest.fail(f"Anvil is required at {ANVIL}. Install Foundry and run foundryup.")
    proc = subprocess.Popen(
        [ANVIL, "--port", str(PORT), "--chain-id", "31337", "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port(PORT)
        yield Web3(Web3.HTTPProvider(f"http://127.0.0.1:{PORT}"))
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_chain_boots_mints_and_accepts_a_permit(anvil: Web3):
    deployer = Account.from_key(DEPLOYER_KEY)
    relayer_account = Account.create()
    fund = {
        **_base(anvil, deployer.address, 0),
        "to": relayer_account.address,
        "value": anvil.to_wei(5, "ether"),
        "gas": 21_000,
    }
    _send(anvil, fund, DEPLOYER_KEY)

    usdc_abi, usdc_code = _artifact("MockUSDC.sol/MockUSDC.json")
    router_abi, router_code = _artifact("PaymentRouter.sol/PaymentRouter.json")
    usdc = anvil.eth.contract(abi=usdc_abi, bytecode=usdc_code)
    usdc_tx = usdc.constructor(deployer.address).build_transaction(_base(anvil, deployer.address, 1))
    usdc_address = _send(anvil, usdc_tx, DEPLOYER_KEY)
    usdc = anvil.eth.contract(address=usdc_address, abi=usdc_abi)

    router = anvil.eth.contract(abi=router_abi, bytecode=router_code)
    router_tx = router.constructor(usdc_address, relayer_account.address, 100_000_000, deployer.address).build_transaction(
        _base(anvil, deployer.address, 2)
    )
    router_address = _send(anvil, router_tx, DEPLOYER_KEY)

    role = usdc.functions.MINTER_ROLE().call()
    grant = usdc.functions.grantRole(role, relayer_account.address).build_transaction(_base(anvil, deployer.address, 3))
    _send(anvil, grant, DEPLOYER_KEY)

    settings = Settings(
        key_encryption_key="11" * 32,
        database_url=os.environ["DATABASE_URL"],
        rpc_url=f"http://127.0.0.1:{PORT}",
        chain_id=31337,
        relayer_private_key=relayer_account.key.hex(),
        router_address=router_address,
        usdc_address=usdc_address,
    )
    chain = Chain(settings)
    await chain.assert_boot()
    assert await chain.max_transfer() == 100_000_000

    user = Account.create()
    calldata = chain.encode_mint(user.address, 5_000_000)
    gas = await chain.estimate_gas(usdc_address, calldata)
    max_fee, priority = await chain.initial_fees()
    assert priority == 2 * 10**9
    minted = chain.sign_transaction(
        nonce=0,
        to=usdc_address,
        calldata=calldata,
        gas_limit=gas,
        max_fee=max_fee,
        priority=priority,
        permit_deadline=None,
    )
    await chain.send_raw(minted.raw)
    anvil.eth.wait_for_transaction_receipt(minted.tx_hash)
    assert await chain.usdc_balance(user.address) == 5_000_000
    assert await chain.permit_nonce(user.address) == 0

    deadline = await chain.block_timestamp() + 3600
    v, r, s = chain.sign_permit(user.key, user.address, 1_000_000, 0, deadline)
    permit_tx = usdc.functions.permit(user.address, router_address, 1_000_000, deadline, v, r, s).build_transaction(
        {
            **_base(anvil, relayer_account.address, 1),
            "gas": 200_000,
        }
    )
    _send(anvil, permit_tx, relayer_account.key.hex())
    assert usdc.functions.allowance(user.address, router_address).call() == 1_000_000
    assert await chain.permit_nonce(user.address) == 1
