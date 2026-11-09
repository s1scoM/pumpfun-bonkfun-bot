"""Integration dry run: real UniversalTrader with entry_mode MODE, dry_run on,
a throwaway keypair (no funds, nothing signed or sent), temp state files.
Usage: PYTHONPATH=src python tools/signal_integration_dry.py MODE TIMEOUT [QUEUE_FILE]"""
import asyncio, sys
import base58, yaml
from solders.keypair import Keypair
import bot_runner
from interfaces.core import Platform
from trading.universal_trader import UniversalTrader

mode, timeout = sys.argv[1], int(sys.argv[2])
cfg = yaml.safe_load(open("bots/momo-dryrun.yaml"))
cfg.pop("keystore", None)
cfg["private_key"] = base58.b58encode(bytes(Keypair())).decode()
cfg["rpc_endpoint"] = "https://solana-rpc.publicnode.com"
cfg["wss_endpoint"] = "wss://solana-rpc.publicnode.com"
cfg["dry_run"] = True
cfg["trade"]["entry_mode"] = mode
sec = cfg["filters"].setdefault(mode, {})
sec["traded_file"] = f"/workspace/signals-dev/integ-{mode}-traded.json"
if mode == "xsignal":
    sec["queue_file"] = sys.argv[3]
    sec["state_file"] = "/workspace/signals-dev/integ-xsignal-state.json"
cfg["risk"]["state_file"] = "/workspace/signals-dev/integ-risk.json"
cfg["timing"]["token_wait_timeout"] = timeout
settings = bot_runner.build_trader_settings(cfg, Platform.PUMP_FUN, positions_file="/workspace/signals-dev/integ-positions.json")
assert settings.dry_run and settings.entry_mode == mode


async def run():
    t = UniversalTrader(settings)
    t.risk_guard.allow_buy = lambda **kw: None  # throwaway wallet has 0 SOL; reach the DRY_RUN branch
    await t.start()

asyncio.run(run())
