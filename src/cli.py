# src/cli.py

import argparse
import importlib
import os
import sys
import asyncio
import logging # Import logging
from dotenv import load_dotenv
from typing import Optional

# Early environment variables loading
print("Attempting to load environment variables...")
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)
print(".env variables loaded (if file exists).")

if project_root not in sys.path:
    sys.path.insert(0, project_root)
    print(f"Added project root to sys.path: {project_root}")

try:
    from src.core.client import SolanaClient
    from solana.rpc.async_api import AsyncClient as SolanaPyAsyncClient
    from src.core.wallet import Wallet
    from src.core.priority_fee.manager import PriorityFeeManager
    from src.trading.trader import PumpTrader
    from src.utils.logger import get_logger
except ImportError as e:
    print(f"[Critical Import Error] Failed to import core modules: {e}")
    print("Please ensure all dependencies are installed and paths are set up.")
    sys.exit(1)

logger = get_logger(__name__)

# --- Environment Variable Checks ---
rpc_check = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
wss_check = os.getenv("SOLANA_NODE_WSS_ENDPOINT")
key_check = os.getenv("SOLANA_PRIVATE_KEY")
bird_check = os.getenv("BIRDEYE_API_KEY")
logger.debug(f"Post-load env check: RPC={rpc_check is not None}, WSS={wss_check is not None}, PRIV_KEY={key_check is not None}, BIRD={bird_check is not None}")


# --- load_config function (Keep as is) ---
def load_config(config_path: str):
    """Loads configuration from a Python module path."""
    logger.info(f"Attempting to load configuration from: {config_path}")
    import_path = config_path
    if import_path.endswith(".py"): import_path = import_path[:-3]
    if not import_path.startswith('src.'):
         if os.path.exists(os.path.join(project_root, 'src', f"{import_path}.py")): import_path = f"src.{import_path}"
         else: import_path = config_path.replace(os.path.sep, '.'); import_path = f"src.{import_path}" if not import_path.startswith('src.') else import_path
    logger.info(f"Using import path: {import_path}")
    try:
        config_module = importlib.import_module(import_path)
        logger.info(f"Successfully loaded configuration module: {import_path}")
        required_vars = ['SOLANA_NODE_RPC_ENDPOINT', 'SOLANA_NODE_WSS_ENDPOINT', 'SOLANA_PRIVATE_KEY']
        config = {}; missing_vars = []
        for var in required_vars:
            value = getattr(config_module, var, os.getenv(var)); config[var.lower()] = value
            if value is None: missing_vars.append(var)
        if missing_vars: raise ValueError(f"Missing required config variables: {', '.join(missing_vars)}")
        optional_vars_defaults = {
            'BUY_AMOUNT': 0.01, 'BUY_SLIPPAGE_BPS': 1500, 'SELL_SLIPPAGE_BPS': 2500,
            'SELL_PROFIT_THRESHOLD_PCT': 100.0, 'SELL_STOPLOSS_THRESHOLD_PCT': 30.0,
            'MAX_TOKEN_AGE_SECONDS': 45, 'WAIT_TIME_AFTER_CREATION_SECONDS': 3,
            'WAIT_TIME_AFTER_BUY_SECONDS': 10, 'WAIT_TIME_BEFORE_NEW_TOKEN_SECONDS': 5,
            'MAX_RETRIES': 3, 'MAX_BUY_RETRIES': 3, 'CONFIRM_TIMEOUT_SECONDS': 60,
            'LISTENER_TYPE': 'logs', 'ENABLE_DYNAMIC_FEE': True, 'ENABLE_FIXED_FEE': False,
            'PRIORITY_FEE_FIXED_AMOUNT': 10000, 'EXTRA_PRIORITY_FEE': 0,
            'HARD_CAP_PRIOR_FEE': 1000000, 'RUG_CHECK_CREATOR': False,
            'RUG_MAX_CREATOR_HOLD_PCT': 20.0, 'RUG_CHECK_PRICE_DROP': False, 'RUG_PRICE_DROP_PCT': 40.0,
            'RUG_CHECK_LIQUIDITY_DROP': False, 'RUG_LIQUIDITY_DROP_PCT': 50.0, 'CLEANUP_MODE': 'after_sell',
            'CLEANUP_FORCE_CLOSE': False, 'CLEANUP_PRIORITY_FEE': True,
            'SOL_THRESHOLD_FOR_RAYDIUM': 300.0
        }
        for var, default in optional_vars_defaults.items():
            config_val = getattr(config_module, var, os.getenv(var))
            try:
                if isinstance(default, bool): config[var.lower()] = bool(config_val) if config_val is not None else default
                elif isinstance(default, int): config[var.lower()] = int(config_val) if config_val is not None else default
                elif isinstance(default, float): config[var.lower()] = float(config_val) if config_val is not None else default
                else: config[var.lower()] = str(config_val) if config_val is not None else default
            except (ValueError, TypeError) as type_e:
                 logger.warning(f"Config Warning: Could not convert {var} ('{config_val}') to type {type(default)}. Using default {default}. Error: {type_e}")
                 config[var.lower()] = default
        logger.info("Configuration loaded successfully.")
        return config
    except ModuleNotFoundError: logger.error(f"Config module not found: '{import_path}'."); raise
    except ValueError as e: logger.error(f"Config error in '{import_path}': {e}"); raise
    except Exception as e: logger.error(f"Unexpected error loading config {import_path}: {e}", exc_info=True); raise

async def main():
    args = parser.parse_args()
    logger.info("--- Starting main() ---")
    try:
        config = load_config(args.config)
    except Exception:
        logger.critical("Exiting due to config load failure.")
        sys.exit(1)

    logger.info("Configuration loaded, initializing components...")
    trader_client: Optional[SolanaClient] = None
    fee_manager_client: Optional[SolanaPyAsyncClient] = None
    trader: Optional[PumpTrader] = None
    wallet: Optional[Wallet] = None # Define wallet variable
    priority_fee_manager: Optional[PriorityFeeManager] = None # Define fee manager variable


    try:
        logger.debug("Initializing SolanaClient wrapper...")
        trader_client = SolanaClient(config['solana_node_rpc_endpoint'])

        logger.debug("Initializing SolanaPy AsyncClient for Fee Manager...")
        fee_manager_client = SolanaPyAsyncClient(config['solana_node_rpc_endpoint'])

        logger.debug("Initializing Wallet...")
        # Use the private key from config to initialize Wallet
        wallet = Wallet(config['solana_private_key'])
        logger.info(f"Wallet Pubkey: {wallet.pubkey}")

        logger.debug("Initializing PriorityFeeManager...")
        priority_fee_manager = PriorityFeeManager(
            client=fee_manager_client, # Pass the raw client
            enable_dynamic_fee=bool(config['enable_dynamic_fee']),
            enable_fixed_fee=bool(config['enable_fixed_fee']),
            fixed_fee=int(config['priority_fee_fixed_amount']),
            extra_fee=int(config['extra_priority_fee']), # Corrected from float if needed
            hard_cap=int(config['hard_cap_prior_fee'])
        )
        logger.info("Components (Wallet, Client, FeeManager) Initialized.")

        # --- Adjust PumpTrader Initialization ---
        logger.debug("Initializing PumpTrader...")
        buy_slip_decimal = float(config['buy_slippage_bps']) / 10000.0
        sell_slip_decimal = float(config['sell_slippage_bps']) / 10000.0
        sell_profit_decimal = float(config['sell_profit_threshold_pct']) / 100.0
        sell_stoploss_decimal = float(config['sell_stoploss_threshold_pct']) / 100.0
        rug_creator_pct = float(args.rug_max_creator_hold_pct) if args.rug_max_creator_hold_pct is not None else float(config['rug_max_creator_hold_pct'])
        rug_creator_pct_decimal = rug_creator_pct / 100.0
        rug_price_drop_decimal = float(config['rug_price_drop_pct']) / 100.0
        rug_liq_drop_decimal = float(config['rug_liquidity_drop_pct']) / 100.0

        # *** FIX: Pass the correct OBJECTS and config values to PumpTrader ***
        trader = PumpTrader(
            # Core Objects
            client=trader_client,
            priority_fee_manager=priority_fee_manager, # Pass the initialized object
            wallet=wallet,                             # Pass the initialized object
            # Endpoints & Config
            wss_endpoint=config['solana_node_wss_endpoint'],
            # Trading Params (Passed directly)
            buy_amount=float(args.amount) if args.amount is not None else float(config['buy_amount']),
            buy_slippage=buy_slip_decimal,
            sell_slippage=sell_slip_decimal,
            sell_profit_threshold=sell_profit_decimal,
            sell_stoploss_threshold=sell_stoploss_decimal,
            max_token_age=float(config['max_token_age_seconds']),
            wait_time_after_creation=float(config['wait_time_after_creation_seconds']),
            wait_time_after_buy=float(config['wait_time_after_buy_seconds']),
            wait_time_before_new_token=float(config['wait_time_before_new_token_seconds']),
            max_retries=int(config['max_retries']),
            max_buy_retries=int(config['max_buy_retries']),
            confirm_timeout=int(config['confirm_timeout_seconds']),
            # Other Config
            listener_type=str(config['listener_type']),
            # Rug Checks
            rug_check_creator=args.rug_check_creator or bool(config['rug_check_creator']),
            rug_max_creator_hold_pct=rug_creator_pct_decimal,
            rug_check_price_drop=bool(config['rug_check_price_drop']),
            rug_price_drop_pct=rug_price_drop_decimal,
            rug_check_liquidity_drop=bool(config['rug_check_liquidity_drop']),
            rug_liquidity_drop_pct=rug_liq_drop_decimal,
            # Cleanup Config
            cleanup_mode=str(config['cleanup_mode']),
            cleanup_force_close=bool(config['cleanup_force_close']),
            cleanup_priority_fee=bool(config['cleanup_priority_fee']),
            # Raydium Config
            sol_threshold_for_raydium=float(config['sol_threshold_for_raydium'])
        )
        logger.info("PumpTrader Initialized.")
        # --- End PumpTrader Initialization Adjustment ---

    except KeyError as e:
        logger.critical(f"FATAL: Missing config key during initialization: {e}")
        if fee_manager_client: await fee_manager_client.close()
        if trader_client: await trader_client.close()
        sys.exit(1)
    except Exception as e:
        logger.critical(f"FATAL: Error instantiating components: {e}", exc_info=True)
        # Ensure clients are closed even if trader init fails
        if fee_manager_client: await fee_manager_client.close()
        if trader_client: await trader_client.close()
        sys.exit(1)

    # --- Start the Trader ---
    trader_task: Optional[asyncio.Task] = None
    try:
        if trader and hasattr(trader, 'start'):
             logger.info("Starting trader task...")
             trader_task = asyncio.create_task(
                 trader.start(
                    match_string=args.match,
                    bro_address=args.bro,
                    marry_mode=args.marry,
                    yolo_mode=args.yolo
                ),
                name="PumpTrader_Start"
             )
             await trader_task # Wait for the main trader task
        elif trader is None:
             logger.critical("FATAL: Trader object was not initialized successfully.")
        else:
             logger.critical("FATAL: Trader object does not have a 'start' method.")

    except asyncio.CancelledError:
         logger.info("Main trader task was cancelled.")
    except Exception as e:
        logger.critical(f"FATAL: Critical error running trader: {e}", exc_info=True)
    finally:
        # --- Final Cleanup ---
        logger.info("Attempting final cleanup...")
        if trader_task and not trader_task.done():
             logger.info("Cancelling main trader task...")
             trader_task.cancel()
             try: await asyncio.wait_for(trader_task, timeout=5.0)
             except asyncio.TimeoutError: logger.warning("Timeout waiting for main trader task cancellation.")
             except asyncio.CancelledError: pass
             except Exception as e_gather: logger.error(f"Error during trader task gather/cancellation: {e_gather}")

        # Close clients - Close wrapper first
        if trader_client:
             logger.debug("Closing SolanaClient wrapper...")
             await trader_client.close()

        # Close raw client IF it wasn't closed by the wrapper (less likely now)
        if fee_manager_client:
            is_closed = getattr(fee_manager_client, '_closed', None) # httpx client uses _closed
            if is_closed is False: # Explicitly check if not closed
                logger.warning("Raw fee manager client was not closed by wrapper. Attempting direct close.")
                close_method = getattr(fee_manager_client, 'aclose', getattr(fee_manager_client, 'close', None))
                if close_method and asyncio.iscoroutinefunction(close_method):
                    try: await close_method(); logger.info("Raw fee manager async client closed.")
                    except Exception as e_close: logger.error(f"Error closing raw fee manager async client: {e_close}")
                elif close_method:
                     try: close_method(); logger.info("Raw fee manager async client closed (sync).")
                     except Exception as e_close: logger.error(f"Error closing raw fee manager async client (sync): {e_close}")


    logger.info("--- main() finished ---")

# --- Argument parser setup (Keep as is) ---
parser = argparse.ArgumentParser(description="Pump.fun Trading Bot")
parser.add_argument("--config", required=True, help="Python module path to config (e.g., src.config_default)")
parser.add_argument("--yolo", action="store_true", help="Enable YOLO mode (process multiple tokens concurrently)")
parser.add_argument("--marry", action="store_true", help="Enable Marry mode (only process first detected token)")
parser.add_argument("--amount", type=float, help="Override buy amount in SOL (e.g., 0.01)")
parser.add_argument("--rug-check-creator", action="store_true", help="Enable creator holding percentage rug check")
parser.add_argument("--rug-max-creator-hold-pct", type=float, help="Override creator hold %% threshold (e.g., 20.0 for 20%%)")
parser.add_argument("--match", type=str, help="Filter tokens by name/symbol containing this string (case-insensitive)")
parser.add_argument("--bro", type=str, help="Filter tokens created by this specific wallet address")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    logger.info("Starting script execution...")

    if '--config' not in sys.argv:
         print("\nERROR: the following arguments are required: --config", file=sys.stderr)
         parser.print_help(sys.stderr)
         sys.exit(1)

    try:
        try: asyncio.run(main())
        except RuntimeError as e:
             if "cannot run loop while another loop is running" in str(e):
                  logger.warning("Detected potential nested asyncio loop. Applying nest_asyncio patch.")
                  try: import nest_asyncio; nest_asyncio.apply(); logger.info("nest_asyncio patch applied."); asyncio.run(main())
                  except ImportError: logger.error("nest_asyncio not installed. Cannot patch nested loop.")
                  except Exception as patch_e: logger.error(f"Error applying nest_asyncio patch: {patch_e}")
                  if 'nest_asyncio' not in sys.modules: raise e
             else: raise e
    except KeyboardInterrupt: logger.info("Process interrupted by user (KeyboardInterrupt).")
    except Exception as e: logger.critical(f"Unhandled exception at top level: {e}", exc_info=True)
    finally: logger.info("Script execution finished.")