// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {MockUSDC} from "../src/MockUSDC.sol";
import {PaymentRouter} from "../src/PaymentRouter.sol";

contract PaymentRouterTest is Test {
    uint256 internal constant MAX_TRANSFER = 100_000_000;
    bytes32 internal constant PERMIT_TYPEHASH =
        keccak256("Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)");

    uint256 internal ownerKey = 0xA11CE;
    uint256 internal relayerKey = 0xBEEF;
    uint256 internal aliceKey = 0xA11;
    uint256 internal bobKey = 0xB0B;

    address internal owner = vm.addr(ownerKey);
    address internal relayer = vm.addr(relayerKey);
    address internal alice = vm.addr(aliceKey);
    address internal bob = vm.addr(bobKey);

    MockUSDC internal usdc;
    PaymentRouter internal router;

    function setUp() public {
        usdc = new MockUSDC(owner);
        router = new PaymentRouter(address(usdc), relayer, MAX_TRANSFER, owner);
        bytes32 minter = usdc.MINTER_ROLE();
        vm.prank(owner);
        usdc.grantRole(minter, relayer);
        vm.prank(relayer);
        usdc.mint(alice, 1_000_000_000);
    }

    function test_transferWithPermit() public {
        uint256 amount = 5_000_000;
        uint256 deadline = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _sign(aliceKey, amount, deadline);

        vm.expectEmit(true, true, false, true);
        emit PaymentRouter.Payment(alice, bob, amount);
        vm.prank(relayer);
        router.transferWithPermit(alice, bob, amount, deadline, v, r, s);

        assertEq(usdc.balanceOf(bob), amount);
        assertEq(usdc.balanceOf(alice), 1_000_000_000 - amount);
    }

    function test_directPermitStillTransfersBeforeDeadline() public {
        uint256 amount = 5_000_000;
        uint256 deadline = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _sign(aliceKey, amount, deadline);

        vm.prank(alice);
        usdc.permit(alice, address(router), amount, deadline, v, r, s);

        vm.prank(relayer);
        router.transferWithPermit(alice, bob, amount, deadline, v, r, s);
        assertEq(usdc.balanceOf(bob), amount);
    }

    function test_directPermitRouterCallAfterDeadlineReverts() public {
        uint256 amount = 5_000_000;
        uint256 deadline = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _sign(aliceKey, amount, deadline);

        vm.prank(alice);
        usdc.permit(alice, address(router), amount, deadline, v, r, s);
        assertEq(usdc.allowance(alice, address(router)), amount);

        vm.warp(deadline + 1);
        vm.prank(relayer);
        vm.expectRevert();
        router.transferWithPermit(alice, bob, amount, deadline, v, r, s);
        assertEq(usdc.balanceOf(bob), 0);
    }

    function test_capRevert() public {
        uint256 deadline = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _sign(aliceKey, MAX_TRANSFER + 1, deadline);
        vm.prank(relayer);
        vm.expectRevert(PaymentRouter.AmountOutOfRange.selector);
        router.transferWithPermit(alice, bob, MAX_TRANSFER + 1, deadline, v, r, s);
    }

    function test_nonRelayerReverts() public {
        uint256 deadline = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _sign(aliceKey, 1, deadline);
        vm.prank(alice);
        vm.expectRevert(PaymentRouter.NotRelayer.selector);
        router.transferWithPermit(alice, bob, 1, deadline, v, r, s);
    }

    function test_routerPause() public {
        vm.prank(owner);
        router.pause();
        uint256 deadline = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _sign(aliceKey, 1, deadline);
        vm.prank(relayer);
        vm.expectRevert();
        router.transferWithPermit(alice, bob, 1, deadline, v, r, s);
    }

    function test_zeroRecipient() public {
        uint256 deadline = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _sign(aliceKey, 1, deadline);
        vm.prank(relayer);
        vm.expectRevert(PaymentRouter.ZeroRecipient.selector);
        router.transferWithPermit(alice, address(0), 1, deadline, v, r, s);
    }

    function _sign(uint256 key, uint256 value, uint256 deadline)
        internal
        view
        returns (uint8 v, bytes32 r, bytes32 s)
    {
        address owner_ = vm.addr(key);
        bytes32 structHash = keccak256(
            abi.encode(PERMIT_TYPEHASH, owner_, address(router), value, usdc.nonces(owner_), deadline)
        );
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", usdc.DOMAIN_SEPARATOR(), structHash));
        (v, r, s) = vm.sign(key, digest);
    }
}
