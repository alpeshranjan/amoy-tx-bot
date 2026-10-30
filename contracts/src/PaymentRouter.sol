// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Permit.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";

/// @notice Relayer-only USDC transfer. The user signs a permit; the relayer pays gas.
contract PaymentRouter is Ownable, Pausable {
    IERC20 public immutable token;
    address public immutable relayer;
    uint256 public immutable maxTransfer;

    event Payment(address indexed from, address indexed to, uint256 amount);

    error NotRelayer();
    error ZeroRecipient();
    error AmountOutOfRange();

    constructor(address token_, address relayer_, uint256 maxTransfer_, address owner_) Ownable(owner_) {
        token = IERC20(token_);
        relayer = relayer_;
        maxTransfer = maxTransfer_;
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    function transferWithPermit(
        address from,
        address to,
        uint256 amount,
        uint256 deadline,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external {
        require(block.timestamp <= deadline);
        if (msg.sender != relayer) revert NotRelayer();
        _requireNotPaused();
        if (to == address(0)) revert ZeroRecipient();
        if (amount == 0 || amount > maxTransfer) revert AmountOutOfRange();

        // A broadcast permit is public. If someone submits it to the token first,
        // permit reverts and transferFrom still spends the allowance.
        try IERC20Permit(address(token)).permit(from, address(this), amount, deadline, v, r, s) {} catch {}

        token.transferFrom(from, to, amount);
        emit Payment(from, to, amount);
    }
}
