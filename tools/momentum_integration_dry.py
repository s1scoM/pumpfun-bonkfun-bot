"""Integration dry run: real UniversalTrader with entry_mode momentum, dry_run on,
a throwaway keypair (no funds, nothing signed or sent), temp risk/positions files."""
import asyncio, copy, sys
import base58, yaml
from solders.keypair import Keypair
import bot_runner
from interfaces.core import Platform
from trading.universal_trader import UniversalTrader

cfg = yaml.safe_load(open("bots/momo-dryrun.yaml"))
cfg.pop("keystore", None)
cfg["private_key"] = base58.b58encode(bytes(Keypair())).decode()
cfg["rpc_endpoint"] = "https://solana-rpc.publicnode.com"
cfg["wss_endpoint"] = "wss://solana-rpc.publicnode.com"
cfg["dry_run"] = True
cfg["trade"]["entry_mode"] = "momentum"
cfg["filters"]["momentum"]["traded_file"] = "/workspace/momentum-dev/integ-traded.json"
cfg["risk"]["state_file"] = "/workspace/momentum-dev/integ-risk.json"
cfg["timing"]["token_wait_timeout"] = int(sys.argv[1]) if len(sys.argv) > 1 else 300
settings = bot_runner.build_trader_settings(cfg, Platform.PUMP_FUN, positions_file="/workspace/momentum-dev/integ-positions.json")
assert settings.dry_run and settings.entry_mode == "momentum"
async def run():
    t = UniversalTrader(settings)
    t.risk_guard.allow_buy = lambda **kw: None  # throwaway wallet has 0 SOL; reach the DRY_RUN branch
    await t.start()
asyncio.run(run())
