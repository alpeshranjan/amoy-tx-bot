// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {Script, console} from "forge-std/Script.sol";
import {MockUSDC} from "../src/MockUSDC.sol";
import {PaymentRouter} from "../src/PaymentRouter.sol";

contract Deploy is Script {
    uint256 internal constant MAX_TRANSFER = 100_000_000;

    function run() external {
        uint256 deployerKey = vm.envUint("DEPLOYER_PRIVATE_KEY");
        address relayer = vm.addr(vm.envUint("RELAYER_PRIVATE_KEY"));
        address deployer = vm.addr(deployerKey);

        vm.startBroadcast(deployerKey);
        MockUSDC usdc = new MockUSDC(deployer);
        PaymentRouter router = new PaymentRouter(address(usdc), relayer, MAX_TRANSFER, deployer);
        usdc.grantRole(usdc.MINTER_ROLE(), relayer);
        vm.stopBroadcast();

        string memory obj = "deploy";
        vm.serializeUint(obj, "chainId", block.chainid);
        vm.serializeAddress(obj, "token", address(usdc));
        vm.serializeAddress(obj, "router", address(router));
        vm.serializeAddress(obj, "relayer", relayer);
        string memory json = vm.serializeUint(obj, "maxTransfer", MAX_TRANSFER);
        if (!vm.exists("deployments")) vm.createDir("deployments", true);
        string memory path = string.concat("deployments/", vm.toString(block.chainid), ".json");
        vm.writeJson(json, path);
        console.log("token", address(usdc));
        console.log("router", address(router));
        console.log("relayer", relayer);
        console.log("wrote", path);
    }
}
