# src/cli.py

import argparse
import importlib
import os
import sys
import asyncio
# --- Load .env *FIRST* ---
from dotenv import load_dotenv
print("Attempting to load environment variables...")
load_dotenv()
print(".env variables loaded (if file exists).")
# --- End Load .env ---

# Add src directory to Python path
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)
    print(f"Added project root to sys.path: {project_root}")

# --- Now import project modules ---
from typing import Optional # Import Optional for type hints
from src.core.client import SolanaClient
from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient
from src.core.wallet import Wallet
from src.core.priority_fee.manager import PriorityFeeManager
from src.trading.trader import PumpTrader
from src.utils.logger import get_logger

logger = get_logger(__name__)

# --- Check Env Vars ---
rpc_check = os.getenv("SOLANA_NODE_RPC_ENDPOINT"); wss_check = os.getenv("SOLANA_NODE_WSS_ENDPOINT"); key_check = os.getenv("SOLANA_PRIVATE_KEY"); bird_check = os.getenv("BIRDEYE_API_KEY");
logger.debug(f"Post-load env check: RPC={rpc_check is not None}, WSS={wss_check is not None}, PRIV_KEY={key_check is not None}, BIRD={bird_check is not None}")


def load_config(config_path: str):
    """Loads configuration from a Python module path."""
    logger.info(f"Attempting to load configuration from: {config_path}")
    if config_path.endswith(".py"): config_path = config_path[:-3]
    import_path = config_path.replace(os.path.sep, '.')
    normalized_path = os.path.normpath(config_path)
    if not import_path.startswith('src.') and 'src' in config_path and normalized_path.startswith('src'):
         import_path = normalized_path.replace(os.path.sep, '.')
         logger.debug(f"Adjusted import path to: {import_path}")
    logger.info(f"Derived import path: {import_path}")
    try:
        config_module = importlib.import_module(import_path)
        logger.info(f"Successfully loaded configuration module: {import_path}")
        required_vars = ['SOLANA_NODE_RPC_ENDPOINT', 'SOLANA_NODE_WSS_ENDPOINT', 'SOLANA_PRIVATE_KEY']
        config = {}
        missing_vars = []
        for var in required_vars: value = getattr(config_module, var, os.getenv(var)); config[var.lower()] = value;
        optional_vars_defaults = {
             'BUY_AMOUNT': 0.01, 'BUY_SLIPPAGE_BPS': 2500, 'SELL_SLIPPAGE_BPS': 2500,
             'SELL_PROFIT_THRESHOLD_PCT': 100.0, 'SELL_STOPLOSS_THRESHOLD_PCT': 50.0,
             'MAX_TOKEN_AGE_SECONDS': 60, 'WAIT_TIME_AFTER_CREATION_SECONDS': 5,
             'WAIT_TIME_AFTER_BUY_SECONDS': 10, 'WAIT_TIME_BEFORE_NEW_TOKEN_SECONDS': 5,
             'MAX_RETRIES': 3, 'MAX_BUY_RETRIES': 3, 'CONFIRM_TIMEOUT_SECONDS': 60,
             'LISTENER_TYPE': 'logs', 'ENABLE_DYNAMIC_FEE': True, 'ENABLE_FIXED_FEE': False,
             'PRIORITY_FEE_FIXED_AMOUNT': 10000, 'EXTRA_PRIORITY_FEE': 0,
             'HARD_CAP_PRIOR_FEE': 1000000, 'RUG_CHECK_CREATOR': False,
             'RUG_MAX_CREATOR_HOLD_PCT': 30.0, 'RUG_CHECK_PRICE_DROP': False, 'RUG_PRICE_DROP_PCT': 50.0,
             'RUG_CHECK_LIQUIDITY_DROP': False, 'RUG_LIQUIDITY_DROP_PCT': 60.0, 'CLEANUP_MODE': 'disabled',
             'CLEANUP_FORCE_CLOSE': False, 'CLEANUP_PRIORITY_FEE': True
        }
        for var, default in optional_vars_defaults.items(): config[var.lower()] = getattr(config_module, var, os.getenv(var, default))
        still_missing = [var for var in required_vars if config.get(var.lower()) is None]
        if still_missing: raise ValueError(f"Missing required config variables: {', '.join(still_missing)}")
        elif missing_vars: logger.warning(f"Required variables {[v for v in missing_vars if config.get(v.lower())]} loaded from env.")
        logger.info(f"Configuration loaded successfully.")
        return config
    except ModuleNotFoundError: logger.error(f"Config module not found: '{import_path}'."); raise
    except ValueError as e: logger.error(f"Config error in '{import_path}': {e}"); raise
    except Exception as e: logger.error(f"Unexpected error loading config {import_path}: {e}", exc_info=True); raise


async def main():
    args = parser.parse_args()
    logger.info("--- Starting main() ---")
    try: config = load_config(args.config)
    except Exception: logger.error("Exiting due to config load failure."); return

    logger.info("Configuration loaded, initializing components...")
    async_client: Optional[SolanaPyAsyncClient] = None
    client: Optional[SolanaClient] = None

    try:
        logger.debug("Initializing SolanaClient...")
        client = SolanaClient(config['solana_node_rpc_endpoint'])

        logger.debug("Initializing SolanaPy AsyncClient...")
        async_client = SolanaPyAsyncClient(config['solana_node_rpc_endpoint'])

        logger.debug("Initializing Wallet...")
        wallet = Wallet(config['solana_private_key'])
        logger.info(f"Wallet Pubkey: {wallet.pubkey}")

        logger.debug("Initializing PriorityFeeManager...")
        priority_fee_manager = PriorityFeeManager(
            client=async_client, # Pass the SolanaPyAsyncClient instance
            enable_dynamic_fee=config['enable_dynamic_fee'],
            enable_fixed_fee=config['enable_fixed_fee'],
            fixed_fee=config['priority_fee_fixed_amount'],
            extra_fee=config['extra_priority_fee'],
            hard_cap=config['hard_cap_prior_fee']
        )
        logger.info("Components Wallet, Client, FeeManager Initialized.")

        logger.debug("Initializing PumpTrader...")
        # Convert values
        buy_slip_decimal = config['buy_slippage_bps'] / 10000.0
        sell_slip_decimal = config['sell_slippage_bps'] / 10000.0
        sell_profit_decimal = config['sell_profit_threshold_pct'] / 100.0
        sell_stoploss_decimal = config['sell_stoploss_threshold_pct'] / 100.0
        rug_creator_pct_value = (args.rug_max_creator_hold_pct if args.rug_max_creator_hold_pct is not None else config['rug_max_creator_hold_pct'])
        rug_creator_pct_decimal = float(rug_creator_pct_value) / 100.0
        rug_price_drop_decimal = config['rug_price_drop_pct'] / 100.0
        rug_liq_drop_decimal = config['rug_liquidity_drop_pct'] / 100.0

        trader = PumpTrader(
             client=client,
             # --- rpc_endpoint REMOVED ---
             wss_endpoint=config['solana_node_wss_endpoint'],
             private_key=config['solana_private_key'],
             buy_amount=args.amount if args.amount is not None else config['buy_amount'],
             buy_slippage=buy_slip_decimal,
             sell_slippage=sell_slip_decimal,
             sell_profit_threshold=sell_profit_decimal,
             sell_stoploss_threshold=sell_stoploss_decimal,
             max_token_age=config['max_token_age_seconds'],
             wait_time_after_creation=config['wait_time_after_creation_seconds'],
             wait_time_after_buy=config['wait_time_after_buy_seconds'],
             wait_time_before_new_token=config['wait_time_before_new_token_seconds'],
             max_retries=config['max_retries'],
             max_buy_retries=config['max_buy_retries'],
             confirm_timeout=config['confirm_timeout_seconds'],
             listener_type=config['listener_type'],
             enable_dynamic_fee=config['enable_dynamic_fee'],
             enable_fixed_fee=config['enable_fixed_fee'],
             fixed_fee=config['priority_fee_fixed_amount'],
             extra_fee=config['extra_priority_fee'],
             cap=config['hard_cap_prior_fee'],
             rug_check_creator=args.rug_check_creator or config['rug_check_creator'],
             rug_max_creator_hold_pct=rug_creator_pct_decimal,
             rug_check_price_drop=config['rug_check_price_drop'],
             rug_price_drop_pct=rug_price_drop_decimal,
             rug_check_liquidity_drop=config['rug_check_liquidity_drop'],
             rug_liquidity_drop_pct=rug_liq_drop_decimal,
             cleanup_mode=config['cleanup_mode'],
             cleanup_force_close=config['cleanup_force_close'],
             cleanup_priority_fee=config['cleanup_priority_fee']
        )
        logger.info("PumpTrader Initialized.")

    except KeyError as e: logger.critical(f"FATAL: Missing config key: {e}"); await async_client.close() if async_client else None; return
    except Exception as e: logger.critical(f"FATAL: Error instantiating components: {e}", exc_info=True); await async_client.close() if async_client else None; return

    # Run trader.start
    logger.info("Starting trader...")
    try:
        await trader.start(
            match_string=args.match,
            bro_address=args.bro,
            marry_mode=args.marry,
            yolo_mode=args.yolo
        )
    except Exception as e:
         logger.critical(f"FATAL: Critical error running trader: {e}", exc_info=True)
    finally:
         if async_client and hasattr(async_client, 'close') and hasattr(async_client,'is_closed') and not async_client.is_closed():
             logger.info("Closing async Solana client connection in main finally block...")
             await async_client.close()

    logger.info("--- main() finished ---")


# Define parser globally
parser = argparse.ArgumentParser(description="Pump.fun Trading Bot")
parser.add_argument("--config", required=True, help="Python module path to config (e.g., src.config_default)")
parser.add_argument("--yolo", action="store_true", help="Enable YOLO mode")
parser.add_argument("--marry", action="store_true", help="Enable Marry mode")
parser.add_argument("--amount", type=float, help="Override buy amount")
parser.add_argument("--rug-check-creator", action="store_true", help="Enable creator rug check")
parser.add_argument("--rug-max-creator-hold-pct", type=float, help="Override creator hold % (e.g. 30.0)")
parser.add_argument("--match", type=str, help="Token name/symbol filter")
parser.add_argument("--bro", type=str, help="Creator address filter")
# Add other arguments...

if __name__ == "__main__":
     logger.info(f"Starting script execution...")
     if len(sys.argv) <= 1: parser.print_help(sys.stderr); sys.exit(1)
     try: asyncio.run(main())
     except RuntimeError: import nest_asyncio; nest_asyncio.apply(); asyncio.run(main())
     except KeyboardInterrupt: logger.info("Process interrupted by user.")
     finally: logger.info(f"Script execution finished.")