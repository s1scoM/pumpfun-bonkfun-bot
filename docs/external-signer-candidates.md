# External signer candidates

Reviewed as shallow checkouts. None is imported, installed or wired into
`pump_bot`. None speaks to Phantom MCP, and none can see another wallet's
perpetual positions.

| Repo | Commit | License | Role |
| --- | --- | --- | --- |
| [0xfnzero/sol-trade-sdk](https://github.com/0xfnzero/sol-trade-sdk) | `c717741d9956f248fd4dd74b4523576bfb74d367` | MIT | Solana spot transaction builder |
| [elizaOS/eliza](https://github.com/elizaOS/eliza) | `67e25f5bfaae98261d52f2298ffa3f22b1f3f065` | MIT | Agent runtime and wallet plugins |
| [TraderAlice/OpenAlice](https://github.com/TraderAlice/OpenAlice) | `03284431c68d71ae8461fa181dc77dd59f1e4a60` | AGPL-3.0 | Local trading orchestrator |

## sol-trade-sdk 11.0.1

Spot only: Pump.fun, PumpSwap, LaunchLab / Bonk / StonkFun, Raydium AMM v4,
Raydium CPMM and Meteora DAMM v2. No perpetual venue.

`build_transaction_with_version` in
`src/trading/common/transaction_builder.rs` takes `Arc<Keypair>` and calls
`try_sign_message` before it returns the `VersionedTransaction`. The hot path
does not return an unsigned envelope for an external signer. Using it here
would add a Rust toolchain and a key the embedded wallet does not export.

## eliza

`plugins/plugin-wallet` signs Solana with `LocalSolanaSigner`, which wraps a
`Keypair` read from `SOLANA_PRIVATE_KEY` or `WALLET_PRIVATE_KEY`. The same
plugin also routes Hyperliquid perps, bridges and LP. That is agent
orchestration plus a custodial local key, not an unsigned builder for
`solana_send`.

## OpenAlice

Research and broker orchestration. Installable engines are `ccxt`, `alpaca`,
`ibkr`, `leverup` and `longbridge`; `mock` is the only engine built in.
Integration tests name Bybit, OKX and Hyperliquid paper perps, plus Alpaca and
IBKR. Broker credentials stay in the local app. It does not build Solana
instructions for a public fee payer. The AGPL-3.0 license also rules out
copying it into this MIT bot.

## What stays

`bots/phantom-executor.yaml` remains `enabled: false`, `stop: true` and
`submit: false`. A Phantom `perps_*` HTTP 403, and a separate mobile wallet,
are outside these repositories.

## Unverified wallet advice

Advice to import a seed, create a burner wallet, unlock a browser extension for
eight hours, or turn on auto-approve for Jupiter, Drift or Axiom was not
followed and is not implemented here. No reviewed repository documents a scoped
Solana session key for the Phantom MCP embedded wallet.

Phantom's own docs separate the two wallets. An embedded wallet is created with
Google or Apple and is managed by Phantom. An extension wallet is a
self-custody wallet with its own recovery phrase. Auto-confirm exists only for
the injected extension provider; the docs say calling it on an embedded
provider throws. The embedded path documents `signAndSendTransaction`, which
signs and broadcasts together. It does not document `signTransaction` alone.
MCP `solana_send` still simulates when `confirmed` is omitted and signs and
broadcasts only when `confirmed` is true.

The agent address `8rRjKEhJYoYJmG3AVz64xq1cj9LUpqr77j9My5T26BDj` still held
223224000 lamports on the last public `getBalance` read. That read does not
support a claim that social login creates a new empty address every session.
A mobile wallet is a different account. Drift is a perpetual venue. A session
delegation to Drift, Jupiter or Axiom would be a new signature we do not
create, and it would not answer a Phantom MCP `perps_*` 403.

## Who can send

Three different actions are easy to mix together. They are not the same.

A per-trade approval is the supported spot path. The assistant prepares an
unsigned request. Phantom `solana_send` with `confirmed` omitted only
simulates. A later call with `confirmed: true` signs and broadcasts that one
transaction, and only after the user accepts that preview. Buy and sell are
separate approvals. A blockhash that has expired cannot reuse the old yes.

An assistant-executed send after a preview is the same second call. It is
possible only in a session that already has Phantom, and only when the user
sets `confirmed: true` for that preview. This cloud agent has no Phantom
session, so it cannot make that call. The approval is not stored and cannot
be reused for the next transaction.

Unattended automation is not available on the embedded MCP wallet.
Auto-confirm is documented for the browser extension only, and the docs say
it throws for an embedded provider. None of the reviewed repositories provide
a scoped session key, a spending limit, or a revocation call for this wallet.
Holding a raw key would let a process sign alone. That path is refused.
Jupiter's own wallet documents auto-approvals for Jupiter Wallet, which is a
different product from this embedded agent wallet. No Drift session-key page
was found in Drift's docs search, and Drift is a perpetual venue, so it does
not change the `perps_*` 403.
