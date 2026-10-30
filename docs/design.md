# Design

## Parsing

A message is untrusted text. The model is asked for JSON matching a fixed schema: `action`, `amount` (string or null), `token`, `recipient`, and `reject_reason`. There is no confidence score.

Amounts are parsed with `decimal.Decimal` from that string. The string must be a positive decimal with at most 6 places and no exponent. `5.5` is 5500000 base units. `1e2` is rejected.

Before a confirm card, the bot also rejects “send all”, “some”, two amounts, two recipients, any token other than USDC, and a self-transfer. A missing token defaults to USDC.

The model’s recipient is accepted only when it matches a `mention` or a `text_mention` in that message. A name the model invented does not select a wallet. If there is an amount and neither entity, the bot inserts the transfer as `awaiting_recipient` and shows a `request_users` button. The 32-bit `request_id` is stored on that row, and the shared user id is applied only to that row.

Card-creation checks are advisory. Expiry and the hourly transfer limit are enforced when the handler moves `pending` to `queued`. The per-minute parse limit is an in-memory window on the single process.

The worker re-checks the on-chain 100 USDC cap, the sender’s USDC balance, the day’s submitted and mined total plus this amount (500 USDC), and the relayer’s POL. It then `eth_call`s the exact transaction. A revert fails the intent and does not allocate a nonce.

## Keys

Each Telegram user has one wallet. `wallets.user_id` is unique. The first `/start` creates it and enqueues one starter mint. Later `/start` calls do not create another key or another starter mint.

The private key is encrypted with AES-256-GCM under `KEY_ENCRYPTION_KEY`. Each wallet has its own nonce. The user id is the associated data, so a ciphertext cannot be moved to another row. The key is decrypted only to sign a permit, then dropped. Losing `KEY_ENCRYPTION_KEY` makes those keys unreadable. There is no second copy.

The relayer key pays gas and holds `MINTER_ROLE`. It is the only bot-held key that needs POL. The deployer key is used by `forge script` and owns the router. The running bot does not have it.

At startup the process checks the chain id (80002 or 31337), `router.relayer()`, `router.token()`, and the relayer’s minter role.

User wallets are not funded with POL. Faucet POL is scarce, and a drip per wallet would still leave every send dependent on it.

## Transactions

`relayer_txs.kind` is `transfer` or `mint`. A cancel is not a new nonce. It is another `relayer_broadcasts` row at the same nonce with `cancel` set, sending 0 POL from the relayer to itself.

The next nonce is the highest committed nonce plus one, or `base_nonce` on an empty table. `base_nonce` is set by `python -m bot init-base-nonce` and cannot be changed.

The worker signs, `eth_call`s, and commits the hash, nonce, both fees, and raw transaction before `send_raw_transaction`. A crash after that commit rebroadcasts the same bytes. The same nonce can land once.

Every 15 seconds the latest raw transaction is rebroadcast. `already known` is success. There is no mempool present-or-missing branch.

While the permit deadline is in the future, a transaction still unmined after 2 minutes is replaced at the same nonce. Both `maxFeePerGas` and `maxPriorityFeePerGas` rise by at least 10%, and both are stored. Bumps stop at `MAX_FEE_PER_GAS` and `MAX_PRIORITY_FEE_PER_GAS`. The permit deadline is one hour, so replacements reuse the same permit. After the deadline the worker sends the 0 POL cancel instead of bumping that calldata.

Node errors:

- `already known` — success
- `nonce too low` — resolve that nonce from receipts
- `replacement transaction underpriced` — a larger bump
- `insufficient funds` — pause the worker and alert

A required bump past the fee ceiling also pauses the worker. None of these drop the nonce.

A transfer is final only when the relayer’s on-chain nonce has passed its nonce. The worker then checks every stored hash at that nonce and links the one that was mined. After a replacement, the original hash can still be the one included. If the mined broadcast is a cancel, or the receipt reverted, the transfer is failed and cannot land later.

At startup, every committed nonce below the chain count must have a receipt on one of its stored hashes. If none does, or a nonce in that range is missing from the table, the process refuses to start. The permit nonce is read from `nonces(owner)` at signing time. A reverted transfer does not consume it, so there is no local counter.

## Router

`transferWithPermit` requires `block.timestamp <= deadline` before anything else, so a permit that was already submitted to the token cannot move funds after the deadline. `permit` is wrapped in try/catch. `transferFrom` enforces the allowance. The token address is an immutable on the router, not an argument. The caller must be the relayer. The router, not the token, is the contract that pauses.

## Cuts

No fiat, no mainnet, no second token, no per-wallet POL, no smart accounts, no group chats, and no edited-message handling.
