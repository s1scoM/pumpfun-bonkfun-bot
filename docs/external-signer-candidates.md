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
