# src/cli.py

import argparse
import importlib
import os
import sys
import asyncio
import logging
from dotenv import load_dotenv
from typing import Optional, Dict, Any

from solders.pubkey import Pubkey
from src.core.client import SolanaClient
from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient
from src.core.wallet import Wallet
from src.core.priority_fee.manager import PriorityFeeManager
from src.trading.trader import PumpTrader
from src.utils.logger import get_logger

logger = get_logger(__name__)

def load_config(config_path: str) -> Dict[str, Any]:
    """Load and validate the full bot configuration."""
    logger.info(f"Attempting to load configuration from: {config_path}")

    # Normalize to a Python import path
    module_path = config_path.replace("/", ".").replace("\\", ".")
    # If user passed a .py filename, strip that exact suffix
    if module_path.endswith(".py"):
        module_path = module_path[:-3]

    logger.info(f"Using import path: {module_path}")

    try:
        cfg_mod = importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        logger.critical(f"FATAL: Config module not found: '{module_path}'.")
        raise

    # 1) Required core values
    config: Dict[str, Any] = {}
    for var in ("SOLANA_NODE_RPC_ENDPOINT", "SOLANA_NODE_WSS_ENDPOINT", "SOLANA_PRIVATE_KEY"):
        val = getattr(cfg_mod, var, os.getenv(var))
        if not val:
            raise ValueError(f"Missing required config var: {var}")
        config[var] = val

    # 2) Pump.fun program ID & event discriminator must be set
    pump_id = getattr(cfg_mod, "PUMP_FUN_PROGRAM_ID", None) or os.getenv("PUMP_FUN_PROGRAM_ID")
    disc    = getattr(cfg_mod, "CREATE_EVENT_DISCRIMINATOR", None) or os.getenv("CREATE_EVENT_DISCRIMINATOR")
    if not pump_id or not disc:
        raise ValueError("PUMP_FUN_PROGRAM_ID and CREATE_EVENT_DISCRIMINATOR must be set in config or .env")
    config["PUMP_FUN_PROGRAM_ID"]        = pump_id
    config["CREATE_EVENT_DISCRIMINATOR"] = disc

    # 3) All other optional settings
    optional_defaults = {
        "BUY_AMOUNT_SOL": 0.01,
        "BUY_SLIPPAGE_BPS": 1500,
        "SELL_SLIPPAGE_BPS": 2500,
        "RAYDIUM_SELL_SLIPPAGE_BPS": 100,
        "MAX_BUY_RETRIES": 3,
        "MAX_SELL_RETRIES": 2,
        "MAX_RAYDIUM_SELL_RETRIES": 3,
        "CONFIRM_TIMEOUT_SECONDS": 60,
        "MONITORING_INTERVAL_SECONDS": 5.0,
        "BUY_COMPUTE_UNIT_LIMIT": 300_000,
        "SELL_COMPUTE_UNIT_LIMIT": 200_000,
        "RAYDIUM_SELL_COMPUTE_UNIT_LIMIT": 600_000,
        "LISTENER_TYPE": "logs",
        "ENABLE_DYNAMIC_FEE": True,
        "ENABLE_FIXED_FEE": False,
        "PRIORITY_FEE_FIXED_AMOUNT_MICROLAMPORTS": 10000,
        "EXTRA_PRIORITY_FEE_MICROLAMPORTS": 5000,
        "HARD_CAP_PRIORITY_FEE_MICROLAMPORTS": 1_500_000,
        "CLEANUP_MODE": "after_sell",
        "CLEANUP_WITH_PRIORITY_FEE": False,
    }

    for var, default in optional_defaults.items():
        raw = getattr(cfg_mod, var, os.getenv(var))
        if raw is None:
            config[var] = default
        else:
            try:
                config[var] = type(default)(raw)
            except Exception:
                logger.warning(f"Config warning: invalid type for {var}, using default {default}")
                config[var] = default

    logger.info("Configuration loaded successfully.")
    return config


async def main():
    load_dotenv()  # early .env load

    parser = argparse.ArgumentParser(description="Pump.fun Trading Bot")
    parser.add_argument("--config", required=True,
                        help="Python module path (e.g., src.config_hybrid_strategy)")
    parser.add_argument("--yolo", action="store_true", help="Enable YOLO mode")
    parser.add_argument("--marry", action="store_true", help="Enable Marry mode")
    parser.add_argument("--amount", type=float, help="Override BUY_AMOUNT_SOL")
    parser.add_argument("--rug-check-creator", action="store_true", help="Enable creator rug check")
    parser.add_argument("--no-rug-check-creator", action="store_true", help="Disable creator rug check")
    parser.add_argument("--rug-max-creator-hold-pct", type=float, help="Override RUG_MAX_CREATOR_HOLD_PCT")
    parser.add_argument("--match", type=str, help="Filter tokens by name/symbol substring")
    parser.add_argument("--bro", type=str, help="Filter tokens by creator address")
    args = parser.parse_args()

    logger.info("--- Starting main() ---")
    try:
        cfg = load_config(args.config)
    except Exception as e:
        logger.critical(f"Config load failed: {e}")
        sys.exit(1)

    # Apply CLI overrides
    if args.amount is not None:
        cfg["BUY_AMOUNT_SOL"] = args.amount; logger.info(f"CLI Override: BUY_AMOUNT_SOL = {args.amount}")
    if args.rug_check_creator:
        cfg["RUG_CHECK_CREATOR_ENABLED"] = True; logger.info("CLI Override: RUG_CHECK_CREATOR_ENABLED = True")
    if args.no_rug_check_creator:
        cfg["RUG_CHECK_CREATOR_ENABLED"] = False; logger.info("CLI Override: RUG_CHECK_CREATOR_ENABLED = False")
    if args.rug_max_creator_hold_pct is not None:
        cfg["RUG_MAX_CREATOR_HOLD_PCT"] = args.rug_max_creator_hold_pct; logger.info(
            f"CLI Override: RUG_MAX_CREATOR_HOLD_PCT = {args.rug_max_creator_hold_pct}%"
        )
    cfg["FILTER_MATCH_STRING"] = args.match or None
    cfg["FILTER_BRO_CREATORS"]  = [args.bro] if args.bro else None

    # Initialize core components
    try:
        client    = SolanaClient(cfg["SOLANA_NODE_RPC_ENDPOINT"])
        raw_async = SolanaPyAsyncClient(cfg["SOLANA_NODE_RPC_ENDPOINT"])
        wallet    = Wallet(cfg["SOLANA_PRIVATE_KEY"])
        fee_mgr   = PriorityFeeManager(
            client        = raw_async,
            enable_dynamic_fee            = cfg["ENABLE_DYNAMIC_FEE"],
            enable_fixed_fee              = cfg["ENABLE_FIXED_FEE"],
            fixed_fee                     = cfg["PRIORITY_FEE_FIXED_AMOUNT_MICROLAMPORTS"],
            extra_fee                     = cfg["EXTRA_PRIORITY_FEE_MICROLAMPORTS"],
            hard_cap                      = cfg["HARD_CAP_PRIORITY_FEE_MICROLAMPORTS"],
        )
        trader    = PumpTrader(client, wallet, fee_mgr, cfg)
        logger.info("PumpTrader Initialized.")
    except Exception as e:
        logger.critical(f"Initialization error: {e}", exc_info=True)
        if "client" in locals():    await client.close()
        if "raw_async" in locals(): await raw_async.close()
        sys.exit(1)

    # Run the trader
    try:
        await trader.start(
            listener_type     = cfg["LISTENER_TYPE"],
            wss_endpoint      = cfg["SOLANA_NODE_WSS_ENDPOINT"],
            match_string      = cfg.get("FILTER_MATCH_STRING"),
            creator_address   = (cfg.get("FILTER_BRO_CREATORS")[0]
                                 if cfg.get("FILTER_BRO_CREATORS") else None),
            yolo_mode         = args.yolo,
            marry_mode        = args.marry,
        )
    except asyncio.CancelledError:
        logger.info("Trading cancelled.")
    except Exception as e:
        logger.critical(f"FATAL: Unhandled error: {e}", exc_info=True)
    finally:
        logger.info("Cleaning up…")
        await client.close()
        await raw_async.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(main())
